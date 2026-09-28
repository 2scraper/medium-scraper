#!/usr/bin/env python3
"""medium-scraper — Selenium edition

A parity engine. It must agree with playwright_scraper.py and
puppeteer_scraper.py on exit codes, run status, and whether a run crashes or
spends money — the shared modules (`product_parser`, `page_flow`,
`output_writer`, `proxy_pool`, `captcha_solver`) are what keep it honest, and
this file holds only "how to ask Selenium".

Read playwright_scraper.py's docstring for what is different about this SITE.
Three things are different about this ENGINE:

* **It drives the installed Chrome, and there is nothing to choose.**
  chromedriver has no bundled browser, so there is no browser-channel flag
  here. On this site that is an advantage rather than a cost: what Medium's
  Cloudflare refuses is an outdated or headless-marked User-Agent, and an
  installed Chrome is neither.

* **It cannot authenticate a proxy, and it cannot use an authenticated CDP
  endpoint.** `--proxy-server` takes an address with nowhere to put a
  password, and chromedriver's `debuggerAddress` takes a bare `host:port`.
  Both are reported loudly rather than silently half-working. Use the
  Playwright or pyppeteer engine for either.

* **`--concurrency` above 1 is not implemented here.** The worker pool lives
  in playwright_scraper.py. This engine walks the same archive days one at a
  time and produces identical rows, exit codes and metadata — it just takes
  longer. Said out loud rather than run as one worker quietly, which would
  look like the flag did something.

Measured live 2026-09-16: 53 rows from `/tag/python`, 100% of them carrying a
title and an author.

Examples
--------
    python3 selenium_scraper.py --url "https://medium.com/tag/python"

    python3 selenium_scraper.py \\
        --url "https://medium.com/tag/python/archive/2026/09/10" --pages 3

    python3 selenium_scraper.py --url "https://medium.com/@quincylarson"
"""

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlsplit

from selenium import webdriver
from selenium.common.exceptions import (TimeoutException, WebDriverException)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            INJECT_TOKEN_JS)
from product_parser import (parse_posts, SELECTORS, PAGE_CAP,
                            detect_bot_challenge, listing_kind, normalize_url,
                            page_url, paginates_by_url, redirected_away,
                            served_by_medium, source_of, unsupported_reason)
from output_writer import dedupe_by_key, finish_run
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, split_credentials,
                        mask, ROTATE_MODES, ProxyError)
import env_config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("selenium_scraper")

# Explicit, because a driver that stops answering otherwise hangs the run:
# "every remote call is bounded" applies to this engine too.
PAGE_LOAD_TIMEOUT = 60
SCRIPT_TIMEOUT = 30

# Kept identical to the Playwright engine's, and the smoke suite asserts it:
# a floor that differed between engines would mean one of them warning about
# a page its twin called healthy.
FIELD_FLOOR = 90


_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced.

    Global, not first-match: an error can repeat an endpoint several times,
    and a masker that handles one occurrence prints the password for the
    rest while looking like it works.
    """
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _chrome_ua(version: str) -> str:
    """A desktop-Chrome UA naming the browser's OWN real version."""
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{version} Safari/537.36")


def _cdp_host_port(endpoint: str) -> str:
    """`host:port` out of a CDP endpoint, refusing one with credentials.

    Selenium cannot use an authenticated remote CDP endpoint at all, and this
    is the one place to say so. Playwright's `connect_over_cdp` and
    Puppeteer's `browserWSEndpoint` take a full `ws://user:pass@host:port`
    and authenticate on the WebSocket upgrade; chromedriver's
    `debuggerAddress` takes a bare `host:port` with nowhere to put a
    password. Silently stripping the credentials would produce a connection
    refusal a long way from its cause.
    """
    parts = urlsplit(endpoint)
    if parts.username or parts.password:
        logger.error(
            "This --cdp-endpoint carries credentials, and Selenium cannot "
            "send them: chromedriver's debuggerAddress is a bare host:port. "
            "The 2Captcha Scraping Browser API endpoint is authenticated, so "
            "it cannot be used from this engine — run playwright_scraper.py "
            "or puppeteer_scraper.py for it. Endpoint: %s",
            _mask_credentials(endpoint))
        sys.exit(2)
    host = parts.hostname or endpoint
    port = f":{parts.port}" if parts.port else ""
    return f"{host}{port}"


@dataclass
class PageOutcome:
    """What one page produced. Same shape as the other two engines'."""
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    state: Optional[str] = None
    # See the Playwright engine for why this is NOT a per-page counter.
    tag_total_posts: Optional[int] = None
    gap: Optional[int] = None
    scroll: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


class _Session:
    """One Chrome driver, relaunchable onto a different exit.

    Same contract as the Playwright engine's _BrowserSession, including the
    rule that a rotation means a genuinely FRESH browser: cookies a bot
    manager issued against one exit, replayed from another, are a stronger
    signal than either address alone.
    """

    def __init__(self, args, pool):
        self.args, self.pool = args, pool
        self.remote = bool(args.cdp_endpoint)
        self.driver = None

    def open(self):
        options = Options()
        if self.remote:
            options.debugger_address = _cdp_host_port(self.args.cdp_endpoint)
            logger.info("Attaching to an existing browser at %s.",
                        options.debugger_address)
            # No UA, no proxy, no fingerprint on this path: the remote browser
            # brings its own, and stacking a second creates a contradiction
            # rather than better cover.
            self.driver = webdriver.Chrome(options=options)
            self._apply_timeouts()
            return self

        if self.args.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        # 1440x900 matches what the captures were taken at. The viewport
        # decides how far the feed has to be scrolled before Medium fetches
        # the next batch, so keeping the window the measured size keeps the
        # numbers in the README meaningful.
        options.add_argument("--window-size=1440,900")
        # Not a fingerprint measure, a correctness one: without it Chrome
        # advertises "HeadlessChrome", which is a giveaway on any site with a
        # bot manager in front of it.
        options.add_argument("--disable-blink-features=AutomationControlled")

        if self.pool:
            scrubbed, credentials = split_credentials(self.pool.current)
            options.add_argument(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(self.pool.current))
            if credentials:
                logger.warning(
                    "This proxy has credentials and SELENIUM CANNOT SEND "
                    "THEM: --proxy-server accepts an address only, and there "
                    "is no Selenium equivalent of pyppeteer's "
                    "page.authenticate. They have been stripped, so requests "
                    "will go out unauthenticated and the exit will most "
                    "likely refuse them. Use playwright_scraper.py or "
                    "puppeteer_scraper.py for an authenticated proxy.")

        # Chrome's performance log, which is how this engine counts refused
        # GraphQL responses — see `_graphql_refused_count`. It has to be
        # asked for at driver creation; there is no way to turn it on later.
        # Without it this engine would report `complete` where its twins
        # report `partial` on the identical run (§6).
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

        self.driver = webdriver.Chrome(options=options)
        self._apply_timeouts()
        _watch_graphql(self)

        version = self.driver.capabilities.get("browserVersion", "")
        if version:
            try:
                self.driver.execute_cdp_cmd(
                    "Network.setUserAgentOverride",
                    {"userAgent": _chrome_ua(version)})
            except WebDriverException as e:
                logger.debug("Could not override the user agent: %s", e)

        if self.args.fingerprint:
            self._apply_fingerprint()
        return self

    def _apply_timeouts(self):
        self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        self.driver.set_script_timeout(SCRIPT_TIMEOUT)

    def _apply_fingerprint(self):
        # THROUGH THE SHARED HELPER, never by reaching into the response
        # shape here. This line dug the UA out of the response itself until a
        # live call to the API showed what it actually returns: the UA is at
        # `userAgent.userAgent` in the chromium format and at `data.ua` in
        # the raw one, and the key this engine asked for exists in NEITHER.
        # So `--fingerprint`
        # silently set no user agent at all and the browser kept its own —
        # which defeats the flag rather than breaking it, because the run
        # then presents a Windows fingerprint's screen, locale and timezone
        # over a local Chromium's UA. That is the identity MISMATCH the flag
        # exists to avoid (§16, where the same defect was live in four
        # sibling repos at once).
        from fingerprint_client import (get_fingerprint, fingerprint_user_agent,
                                        playwright_init_script)
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = fingerprint_user_agent(fp)
        script = playwright_init_script(fp)
        try:
            if ua:
                self.driver.execute_cdp_cmd("Network.setUserAgentOverride",
                                            {"userAgent": ua})
            # The same patch script the Playwright engine installs on its
            # context. Shared deliberately: two engines applying different
            # halves of one fingerprint would be a contradiction of exactly
            # the kind a fingerprint is meant to avoid.
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument", {"source": script})
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"),
                        fp.get("country"))
        except WebDriverException as e:
            logger.warning("Could not apply the fingerprint over CDP (%s) — "
                           "continuing without it.", e)

    def relaunch(self):
        if self.remote:
            return
        self.close()
        self.open()

    def close(self):
        try:
            if self.driver is not None:
                # quit(), not close(): close() ends one window and leaves the
                # driver process running, which on a per-page rotation would
                # leak a chromedriver per page.
                self.driver.quit()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during driver teardown: %s", e)


# ---------------------------------------------------------------------------
# page_flow, bound to Selenium
# ---------------------------------------------------------------------------
# Only "how to ask this driver" lives here. Note the JS dialect: Selenium's
# execute_script runs a function BODY and needs an explicit `return`, unlike
# the `() => expr` both other engines take — which is exactly why page_flow
# names operations instead of passing JavaScript across the boundary (§1).
def _count(session, selector: str) -> int:
    try:
        return len(session.driver.find_elements(By.CSS_SELECTOR, selector))
    except WebDriverException as e:
        logger.debug("count(%s) failed: %s", selector, e)
        return 0


def _sleep(ms: int) -> None:
    time.sleep(ms / 1000.0)


def _content(session) -> Optional[str]:
    try:
        return session.driver.page_source
    except WebDriverException as e:
        logger.debug("page_source unavailable (page navigating?): %s", e)
        return None


def _scroll_to_bottom(session) -> None:
    """Scroll the WINDOW to the end of the document. See page_flow.

    Note the dialect: Selenium's `execute_script` takes a function BODY with
    an explicit `return`, where Playwright and pyppeteer take `() => expr`.
    That difference is exactly why `page_flow` names the OPERATION and each
    engine spells it itself (§1).
    """
    try:
        session.driver.execute_script(
            "window.scrollTo(0, document.body.scrollHeight); return null;")
    except WebDriverException as e:
        logger.debug("scroll failed: %s", e)


def _page_height(session) -> Optional[int]:
    try:
        return session.driver.execute_script(
            "return document.body.scrollHeight;")
    except WebDriverException:
        return None


# Each scroll batch after the first arrives over POST /graphql, and counting
# the refused ones is what separates a feed that ran out (COMPLETE) from one
# whose next batch was refused (PARTIAL). The other two engines get this from
# a response listener, which Selenium has no equivalent of — so it is read
# out of Chrome's performance log instead.
#
# This is NOT a cosmetic difference. If this engine always answered 0, it
# would report `complete` where its twins report `partial` on the identical
# run, and that is precisely the drift the shared modules exist to prevent
# (§6). The log has to be ENABLED at driver creation; see `_Session.open`.
_GRAPHQL_PATH = "/graphql/"


def _watch_graphql(session) -> None:
    """Reset the running count. The log itself is enabled on the driver."""
    session._graphql_refused = 0
    _graphql_refused_count(session)   # drain anything already buffered


def _graphql_refused_count(session) -> int:
    """How many GraphQL responses have come back >= 400 so far this session.

    Chrome's performance log is drained on read — each `get_log` call returns
    only entries since the last one — so this accumulates rather than
    recounting, and callers take a difference across the window they care
    about.
    """
    import json as _json
    total = getattr(session, "_graphql_refused", 0)
    try:
        entries = session.driver.get_log("performance")
    except Exception:  # noqa: BLE001 — an absent log must never break a run
        return total
    for entry in entries:
        try:
            message = _json.loads(entry.get("message", "{}"))["message"]
            if message.get("method") != "Network.responseReceived":
                continue
            response = message["params"]["response"]
            if _GRAPHQL_PATH in response.get("url", "") \
                    and int(response.get("status", 0)) >= 400:
                total += 1
        except Exception:  # noqa: BLE001
            continue
    session._graphql_refused = total
    return total


def _ready_selector(args) -> str:
    return page_flow.ready_selector(args.mode)


def _min_matches(args, html: str = "") -> int:
    return page_flow.min_matches(args.mode, page_flow.tag_total_posts(html))


def _current_url(session) -> str:
    try:
        return session.driver.current_url
    except WebDriverException:
        return ""


def _classify(session, html: str, status=None) -> str:
    # `status` is POSITIONAL and second. Two engines in a sibling repo passed
    # it as a keyword and both crashed on their first fetch (§17); this
    # repo's smoke suite binds every shared-module call in every engine
    # against the callee's real signature for that reason.
    return page_flow.classify(html, status, _current_url(session))


def _parse_for_mode(html: str, url: str, args, page_num: int = 1) -> List:
    return parse_posts(html, url, page=page_num, mode=args.mode)


def handle_captcha_if_present(session, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Same contract and same reconciliation as the Playwright engine, including
    what it CANNOT reach: Medium's refusal is Cloudflare's MANAGED challenge,
    which publishes no sitekey, so there is nothing for a solver to answer.
    `page_flow.STATE_POLICY` marks that state retry-but-do-not-solve and
    nothing is ever charged for it.
    """
    driver = session.driver
    html = _content(session)
    if html is None:
        return False

    already_rendered = _count(session, _ready_selector(args))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, _current_url(session))
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: driver.execute_script(f"return ({js})();"),
        page_url=_current_url(session))
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return False

    if when_blocked and already_rendered > page_flow.MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d cards are already on the page "
                    "— not solving it. Pass --solve-captcha always to solve "
                    "it anyway.", challenge.kind, challenge.source,
                    already_rendered)
        return False

    logger.warning("%s detected via %s (sitekey=%s, action=%s) — attempting "
                   "to solve.", challenge.kind, challenge.source,
                   challenge.sitekey, challenge.action)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be "
                       "solved — continuing with whatever the page holds.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                api_version=args.captcha_api,
                                min_score=args.min_score)
    except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
        logger.error("Solving the challenge failed (%s) — continuing with "
                     "whatever the page holds.", e)
        return False

    driver.execute_script(f"return ({INJECT_TOKEN_JS})(arguments[0]);", token)
    logger.info("Token injected. Reloading page to continue.")
    _sleep(1500)
    try:
        driver.refresh()
    except WebDriverException as e:
        logger.warning("Reload after the solve failed: %s", e)
    return True


def _scroll_the_feed(session, args, html: str, page_num: int) -> dict:
    """Scroll to the bottom until the feed stops growing.

    A BOUNDED SAFETY NET on this site rather than the main event. Medium
    server-renders its payload — a tag feed's first response already carries
    34 stories and a day archive 128 — and the XHR that would extend the feed
    is a POST to `medium.com/_/graphql` which came back 403 on every one of
    the 11 attempts measured, while the HTML itself came back 200. So the
    expected outcome here is "scrolled, nothing arrived", and that is
    reported rather than fought: more rounds against a feed the site will not
    extend only spends time (§8).

    Deliberately no `target`. The only total Medium publishes is
    `tag.postCount` — 257,497 for `python` — which is the size of the tag's
    whole catalogue since 2003, not of this page. Using it to decide when to
    stop scrolling would scroll forever.
    """
    before = _count(session, page_flow.READY_SELECTOR)
    trace = page_flow.scroll_until_settled(
        lambda sel: _count(session, sel),
        lambda: _scroll_to_bottom(session),
        lambda: _page_height(session),
        _sleep,
        selector=page_flow.READY_SELECTOR)
    logger.info("Scrolled batch %d: %d card(s) at first paint, %d after "
                "%d round(s) of scrolling.", page_num, trace["cards_before"],
                trace["cards_after"], trace["rounds"])
    warning = page_flow.feed_not_extended_warning(args.mode, trace["cards_after"],
                                                  trace)
    if warning:
        logger.info("%s", warning)
    return {"first_paint": before, "reached": trace["cards_after"],
            "rounds": trace["rounds"], "settled": True}


def _fetch_one_page(session, args, pool, page_num: int,
                    url: Optional[str]) -> PageOutcome:
    """Fetch (or turn to) one page and parse it.

    `url` is the address for page 1 and None for every page after it: pages
    2..N on this site are not addresses, they are the result of pressing the
    site's own button.
    """
    outcome = PageOutcome(page_num=page_num, url=url or _current_url(session))

    has_pool = bool(pool and len(pool) > 1)
    block_retries = 0 if not page_flow.RETRY_ON_BLOCKED else (
        args.proxy_block_retries if has_pool
        else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    solves_bought = 0
    html, state, load_failed = None, "ok", False

    for block_attempt in range(block_retries + 1):
        load_failed = False

        if url is not None:
            logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
            for attempt in range(1, args.retries + 1):
                try:
                    session.driver.get(url)
                    load_failed = False
                    break
                except (TimeoutException, WebDriverException) as e:
                    load_failed = True
                    if attempt < args.retries:
                        pause = args.retry_delay * (2 ** (attempt - 1))
                        logger.warning("Timeout loading %s (attempt %d/%d: "
                                       "%s) — retrying in %.1fs.", url,
                                       attempt, args.retries,
                                       str(e)[:120], pause)
                        time.sleep(pause)
        else:
            logger.info("Extending the feed to batch %d/%d by scrolling "
                        "(this listing has no batch-%d address).",
                        page_num, args.pages, page_num)
            refused_before = _graphql_refused_count(session)
            turn = page_flow.advance_feed(
                lambda: _scroll_to_bottom(session),
                lambda sel: _count(session, sel),
                lambda: _page_height(session),
                _sleep)
            if turn == page_flow.NO_GROWTH:
                refused = _graphql_refused_count(session) - refused_before
                if refused:
                    # NOT the end of the listing. Reporting a refusal as
                    # "exhausted" would make a throttled run say "complete"
                    # while holding its first batch (§7).
                    outcome.state = "no_turnover"
                    outcome.load_failed = True
                    outcome.final_url = _current_url(session)
                    logger.error(
                        "The feed did not grow within %.0fs and %d GraphQL "
                        "response(s) were refused while waiting. This is NOT "
                        "the end of the listing — the run is reported as "
                        "PARTIAL (exit 6). Raise --delay (it is %.1fs now), "
                        "or spread the load with --proxy-file.",
                        page_flow.NEXT_BATCH_TIMEOUT_MS / 1000, refused,
                        args.delay)
                    return outcome
                # Before calling that the end of the listing, ASK WHAT PAGE
                # WE ARE ON — see the Playwright engine. A feed that stopped
                # growing because Cloudflare replaced the page has not run
                # out, and `exhausted` is a COMPLETE stop reason.
                current = _content(session) or ""
                state_now = _classify(session, current)
                if page_flow.counts_as_blocked(state_now):
                    outcome.state = "blocked_mid_scroll"
                    outcome.blocked_by = (detect_bot_challenge(current)
                                          or "bot-challenge")
                    outcome.final_url = _current_url(session)
                    logger.error(
                        "The feed stopped growing because the page was "
                        "replaced: it is now %s. This is NOT the end of the "
                        "listing — reported as partial, not complete.",
                        state_now)
                    return outcome
                outcome.state = "exhausted"
                outcome.final_url = _current_url(session)
                return outcome

        if load_failed:
            break

        if handle_captcha_if_present(session, args):
            _sleep(1000)

        html = _content(session) or ""
        state = _classify(session, html)

        if page_flow.is_unpainted(state, html):
            wait_timeout = page_flow.content_timeout_ms(args.mode)
            logger.info("Page %d is a shell the site served but has not "
                        "filled in (%d bytes, no cards) — waiting up to "
                        "%.0fs for the grid rather than spending a retry.",
                        page_num, len(html), wait_timeout / 1000)
            page_flow.wait_for_count(
                lambda sel: _count(session, sel), _sleep,
                _ready_selector(args), _min_matches(args, html), wait_timeout)
            html = _content(session) or html
            state = _classify(session, html)

        if (page_flow.should_solve(state)
                and solves_bought < page_flow.SOLVES_PER_PAGE):
            solves_bought += 1
            if handle_captcha_if_present(session, args):
                _sleep(1000)
                html = _content(session) or html
                state = _classify(session, html)
                if state == "content":
                    logger.info("The solve was accepted — page %d is content "
                                "now.", page_num)
                else:
                    logger.warning("The solve was NOT accepted: page %d is "
                                   "still %s. The purchase is spent.",
                                   page_num, state)

        if not page_flow.should_retry(state):
            break

        if block_attempt < block_retries:
            pause = args.retry_delay * (block_attempt + 1)
            if has_pool:
                logger.warning("Page %d came back as %s from %s — retrying "
                               "from another exit in %.1fs (%d/%d).",
                               page_num, state, mask(pool.current), pause,
                               block_attempt + 1, block_retries)
                pool.advance(f"{state} on page {page_num}")
                session.relaunch()
                time.sleep(pause)
            else:
                # No pool, so nowhere else to go. What DOES clear it here is
                # a FRESH browser context: 9 of 27 first attempts were
                # challenged and all 9 were served on the next attempt in a
                # new context, while waiting inside the same context cleared
                # none. `page_flow.RETRY_NEEDS_FRESH_CONTEXT` says so, and a
                # constant no engine consulted would be dead policy dressed
                # up as enforcement (§17).
                #
                # NOT relaunched over --cdp-endpoint: a Scraping Browser
                # profile allows one live connection, so reconnecting risks
                # `profile_locked`.
                fresh = (page_flow.RETRY_NEEDS_FRESH_CONTEXT
                         and not args.cdp_endpoint)
                logger.warning("Page %d came back as %s — waiting %.1fs and "
                               "re-fetching %s (%d/%d).",
                               page_num, state, pause,
                               "in a FRESH browser context, which is what "
                               "clears this site's challenge" if fresh
                               else "through the same access path",
                               block_attempt + 1, block_retries)
                time.sleep(pause)
                if fresh:
                    session.relaunch()
            if url is None:
                logger.warning("Page %d was reached by a button press, so "
                               "there is no address to re-fetch — stopping "
                               "here rather than silently restarting the "
                               "listing at page 1.", page_num)
                break

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        return outcome

    outcome.state = state

    if page_flow.counts_as_blocked(state):
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        try:
            session.driver.save_screenshot(f"{args.out}_page{page_num}_debug.png")
        except WebDriverException as e:
            logger.warning("Could not capture screenshot: %s", e)
        served = served_by_medium(html or "")
        vendor = detect_bot_challenge(html or "", url=_current_url(session))
        logger.error(
            "The site did not serve this request — %d bytes, %s the site's "
            "own asset hosts, saved to %s. This is exit 3, distinct from a "
            "genuinely empty result (exit 4).", len(html or ""),
            "which references" if served else "with no reference to",
            debug_html)
        logger.error("%s", page_flow.block_advice(
            html, headless=bool(getattr(args, "headless", False)),
            has_pool=has_pool))
        outcome.blocked_by = vendor or ("no-response" if not html else "bot-or-not")
        outcome.final_url = _current_url(session)
        return outcome

    if page_flow.should_parse(state):
        selector, threshold = _ready_selector(args), _min_matches(args, html)
        content_timeout = page_flow.content_timeout_ms(args.mode)
        found = page_flow.wait_for_count(
            lambda sel: _count(session, sel), _sleep, selector, threshold,
            content_timeout)
        _sleep(500)
        if found < threshold:
            logger.info("No story cards appeared within %.0fs. If this "
                        "feed genuinely holds nothing, that is the "
                        "expected answer and the run will report 0 rows "
                        "(exit 4).", content_timeout / 1000)
        outcome.scroll = _scroll_the_feed(session, args, html, page_num)
        html = _content(session) or html

    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html or "")
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html or ""))

    products = _parse_for_mode(html or "", _current_url(session), args, page_num)
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    # The size of the tag's WHOLE catalogue, where Medium states one — 257,497
    # for `python`, going back to 2003. Recorded beside what this run read,
    # and never used as a gap: see PageOutcome.tag_total_posts.
    outcome.tag_total_posts = page_flow.tag_total_posts(html or "")
    outcome.gap = page_flow.page_gap(html or "", len(products))
    if outcome.tag_total_posts:
        logger.info("Medium states this tag holds %d stor(ies) in total, "
                    "going back to 2003; this page carried %d. That is the "
                    "catalogue's size rather than this page's, and the "
                    "difference is not a missing read.",
                    outcome.tag_total_posts, len(products))

    if products:
        with_title = sum(1 for p in products if p.title)
        with_author = sum(1 for p in products if p.author)
        worst = min(with_title, with_author)
        share = 100.0 * worst / len(products)
        # Reported every time, not only when it looks wrong, so a consumer
        # gets the number rather than a threshold someone guessed.
        logger.info("Title/author coverage on page %d: %d and %d of %d "
                    "(%.0f%% at worst); the measured floor is %d%%.",
                    page_num, with_title, with_author, len(products), share,
                    FIELD_FLOOR)
        if share < FIELD_FLOOR:
            logger.warning(
                "Only %.0f%% of page %d carries both a title and an author, "
                "against a measured floor of %d%%. Both payloads carry both "
                "on every story they describe — 128 of 128 on a day archive "
                "and 34 of 34 on a tag feed — so this is the read breaking "
                "rather than the feed being unusual. Re-run with "
                "--dump-html. The likeliest cause is that neither payload "
                "parsed and the rows came from the DOM alone, which the "
                "data_source column will say.",
                share, page_num, FIELD_FLOOR)

        # WHICH view built these rows. `obvinit` is the rich one (84 fields,
        # with reading time, word count and language); `apollo` carries claps
        # and responses but no reading time on a tag feed; `jsonld` and `dom`
        # carry neither. Reported rather than warned about, because which one
        # answers is a property of the URL (§9).
        from collections import Counter
        views = Counter(p.data_source for p in products)
        logger.info("Rows by view on page %d: %s.", page_num,
                    ", ".join(f"{k}={v}" for k, v in sorted(views.items())))
    if not products:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        try:
            session.driver.save_screenshot(f"{args.out}_page{page_num}_debug.png")
        except WebDriverException as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw "
                       "to %s.", debug_html)

    outcome.products = products
    outcome.final_url = _current_url(session)
    return outcome


def scrape(args) -> int:
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    stop_reason = "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint "
                       "the remote browser has its own exit.")
        pool = None

    # Two shapes of pagination, and the URL picks which (§7). A day archive
    # is a real independent address; the other three modes have no address
    # for batch 2 and advance by scrolling.
    by_url = paginates_by_url(args.url)

    if args.concurrency > 1:
        if not by_url:
            logger.warning("--concurrency %d is refused: %s.",
                           args.concurrency,
                           page_flow.concurrency_refusal(args.url))
        else:
            # A DOCUMENTED ENGINE LIMIT, in the same class as this family's
            # "Selenium cannot authenticate a remote CDP endpoint" (§6): the
            # worker pool is implemented in the Playwright engine only. Said
            # out loud rather than run as one worker quietly, which would
            # look like the flag did something.
            logger.warning("--concurrency %d is not implemented in this "
                           "engine — the worker pool lives in "
                           "playwright_scraper.py. Walking the days one at a "
                           "time instead. Exit codes, status and output are "
                           "identical either way.", args.concurrency)

    session = _Session(args, pool).open()
    try:
        first = _fetch_one_page(session, args, pool, 1, args.url)
        outcomes.append(first)

        if not first.ok:
            stop_reason = ("page_load_timeout" if first.load_failed
                           else f"blocked_{first.blocked_by}")
            blocked = first.blocked_by is not None

        elif by_url:
            seen_keys.update(p.sku for p in first.products if p.sku is not None)
            # VERIFY before planning (§7). A tag day with no stories does not
            # 404 — it redirects up to the month view, which is a different
            # renderer holding a different set of stories.
            drift = redirected_away(args.url, first.final_url or args.url)
            planned = []
            if drift:
                logger.warning(
                    "Not walking further days: %s. Pick a day the tag "
                    "actually published on, or use --mode tag.", drift)
                stop_reason = "pagination_redirected"
            elif args.pages > 1:
                planned = [u for u in (page_url(args.url, n)
                                       for n in range(2, args.pages + 1)) if u]

            for index, url in enumerate(planned):
                page_num = index + 2
                if page_flow.page_cap_reached(page_num):
                    logger.warning("Stopping at the %d-page cap.", PAGE_CAP)
                    stop_reason = "page_cap"
                    break
                if pool and pool.rotates_per_page():
                    pool.advance(f"per-page rotation, page {page_num}")
                    session.relaunch()
                time.sleep(args.delay)
                outcome = _fetch_one_page(session, args, pool, page_num, url)
                outcomes.append(outcome)
                if not outcome.ok:
                    stop_reason = ("page_load_timeout" if outcome.load_failed
                                   else f"blocked_{outcome.blocked_by}")
                    blocked = outcome.blocked_by is not None
                    break
                fresh_count = sum(1 for p in outcome.products
                                  if p.sku is None or p.sku not in seen_keys)
                seen_keys.update(p.sku for p in outcome.products
                                 if p.sku is not None)
                if not fresh_count:
                    logger.info("Day %s added no rows not already seen — "
                                "treating that as the end of the listing.",
                                url)
                    stop_reason = "no_new_products"
                    break

        else:
            seen_keys.update(p.sku for p in first.products if p.sku is not None)
            for page_num in range(2, args.pages + 1):
                if page_flow.page_cap_reached(page_num):
                    logger.warning("Stopping at the %d-page cap.", PAGE_CAP)
                    stop_reason = "page_cap"
                    break
                if pool and pool.rotates_per_page() and page_num == 2:
                    logger.warning(
                        "--proxy-rotate per-page cannot be honoured on a "
                        "Medium feed: batch %d exists only inside this "
                        "browser's session, so relaunching on another exit "
                        "would restart the feed at its first batch.",
                        page_num)
                time.sleep(args.delay)
                outcome = _fetch_one_page(session, args, pool, page_num, None)
                outcomes.append(outcome)

                if outcome.state == "exhausted":
                    logger.info("The site offered no further page after page "
                                "%d — treating that as the end of the "
                                "listing.", page_num - 1)
                    stop_reason = "pagination_exhausted"
                    outcomes.pop()
                    break
                if outcome.state == "no_turnover":
                    stop_reason = "next_batch_refused"
                    outcomes.pop()
                    break
                if not outcome.ok:
                    stop_reason = ("page_load_timeout" if outcome.load_failed
                                   else f"blocked_{outcome.blocked_by}")
                    blocked = outcome.blocked_by is not None
                    break

                fresh_count = sum(1 for p in outcome.products
                                  if p.sku is None or p.sku not in seen_keys)
                seen_keys.update(p.sku for p in outcome.products
                                 if p.sku is not None)
                if not fresh_count:
                    logger.info("Page %d added no rows not already seen — "
                                "treating that as the end of the listing.",
                                page_num)
                    stop_reason = "no_new_products"
                    break
    finally:
        session.close()

    all_rows = []
    merged_seen = set()
    fresh_by_batch = {}
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, merged_seen, key="sku")
        fresh_by_batch[oc.page_num] = len(fresh)
        if len(fresh) < len(oc.products):
            # EXPECTED here: a scroll batch re-parses the whole feed, so
            # batch 2 arrives holding batch 1's rows again by construction.
            logger.info("Batch %d: %d row(s) new, %d already seen.",
                        oc.page_num, len(fresh),
                        len(oc.products) - len(fresh))
        all_rows.extend(fresh)

    tag_total_posts = next((o.tag_total_posts for o in outcomes
                              if o.tag_total_posts is not None), None)
    if all_rows and tag_total_posts:
        logger.info("Medium states this tag holds %d stor(ies) in total since "
                    "2003; this run took %d. The two are not comparable and "
                    "the difference is not a gap.",
                    tag_total_posts, len(all_rows))
    if all_rows:
        enriched = sum(1 for r in all_rows
                       if r.data_source and r.data_source != "dom")
        logger.info("Payload coverage over the merged run: %d/%d (%.0f%%) — "
                    "rows built from one of Medium's own payloads rather "
                    "than from the rendered card alone.",
                    enriched, len(all_rows), 100.0 * enriched / len(all_rows))

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)

    extra = {
        "scroll": {o.page_num: o.scroll for o in outcomes if o.scroll},
        "rows_new_per_batch": fresh_by_batch,
        "tag_total_posts": tag_total_posts,
        "inline_payload_rows": sum(1 for r in all_rows
                                   if r.data_source != "dom"),
        "pagination": "infinite-scroll",
    }

    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages, mode=args.mode,
                      source=source_of(final_url),
                      start_url=args.url, final_url=final_url, extra=extra)


def parse_args():
    p = argparse.ArgumentParser(
        description="Medium story scraper (Selenium edition)")
    p.add_argument("--url", default=None,
                   help="A Medium URL: a tag (/tag/{slug}), a tag's day "
                        "archive (/tag/{slug}/archive/{yyyy}/{mm}/{dd}), an "
                        "author (/@{username}) or a story (any URL ending in "
                        "a 12-hex post id, or /p/{id}). A publication home "
                        "page is refused with the reason. Required, unless "
                        "MEDIUM_URL is set in the environment or in .env.")
    p.add_argument("--mode", choices=["tag", "archive", "author", "post"],
                   default=None,
                   help="Which view the URL is. Inferred from the URL by "
                        "default; passing one that disagrees is an error. "
                        "All four yield the same row — what differs is how "
                        "much of it Medium sends (see the module docstring).")
    p.add_argument("--category", default=None,
                   help="Label to tag output rows with. Filled from the URL "
                        "by default.")
    p.add_argument("--pages", type=int, default=1,
                   help=f"Number of pages to walk (default 1, cap {PAGE_CAP}). "
                        f"In --mode archive a page is one day with its own "
                        f"address; every other mode has no per-page address "
                        f"(`?page=2` is ignored and the feed returns its "
                        f"first items again), so a page there is one settled "
                        f"scroll batch.")
    p.add_argument("--delay", type=float, default=3.0,
                   help="Delay between batches, seconds (default 3.0). The "
                        "rate matters more than the address on this site, so "
                        "this is the lever that matters.")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Accepted for family compatibility and not "
                        "implemented in this engine: the worker pool lives in "
                        "playwright_scraper.py, and this engine walks archive "
                        "days one at a time with identical output.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3).")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first page-load retry, doubling "
                        "thereafter (default 2.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="medium_posts", help="Output file prefix")
    p.add_argument("--locale", default="en-US",
                   help="Browser locale. It does NOT decide the site "
                        "language: the HOSTNAME does.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL. NOTE: Selenium cannot authenticate a "
                        "proxy — credentials are stripped with a warning. Use "
                        "playwright_scraper.py or puppeteer_scraper.py for "
                        "an authenticated one.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line. Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES),
                   default="per-run",
                   help="per-run (default). per-page cannot be honoured "
                        "mid-feed here — the next batch lives inside the "
                        "current browser session.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="Retries from other exits when a page is refused.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a fingerprint from 2captcha's Fingerprint API "
                        "and apply it over CDP. Needs --twocaptcha-key.")
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag: Windows, Microsoft Windows or "
                        "Android. NOT a list — Chrome, Desktop and Mobile are "
                        "each rejected by the API with 400.")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use.")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default). Note what neither setting "
                        "reaches, and it is the normal case here: Medium's "
                        "refusal is Cloudflare's MANAGED challenge, which "
                        "publishes no sitekey, so there is nothing for a "
                        "solver to answer. Those are reported as a challenge "
                        "to be retried, and nothing is charged.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score (0.3, 0.7 or 0.9).")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Attach to a running browser at host:port. NOTE: "
                        "Selenium cannot use an AUTHENTICATED endpoint — "
                        "chromedriver's debuggerAddress has nowhere to put a "
                        "password — so the Scraping Browser API is not "
                        "reachable from this engine.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success "
                        "as well as failure.")
    # HEADFUL by default, and the reason is measured rather than habitual.
    #
    # What Medium's Cloudflare actually refuses is the `HeadlessChrome` token
    # in the User-Agent, not headless itself. A bare headless Chromium with
    # its default UA got HTTP 403 and a 5 KB WAF block page on 11 of 11
    # attempts; the SAME browser with an ordinary Chrome UA — which is what
    # `_chrome_ua` builds, from the browser's own version — was never
    # WAF-blocked once. This engine therefore works headless, and that is the
    # family's "the user agent comes from the browser, not a literal" rule
    # (§8) being the difference between data and a block page.
    #
    # Headful is still the default because it is still better: interleaved
    # over 8 rounds on one address, headful was served 8/8 and headless 6/8,
    # the misses being Cloudflare's transient managed challenge. Every figure
    # in the README was taken headful.
    p.add_argument("--headful", dest="headless", action="store_false",
                   default=False,
                   help="Run with a real browser window. THE DEFAULT. "
                        "Measured 8/8 served against headless's 6/8 on one "
                        "address.")
    p.add_argument("--headless", dest="headless", action="store_true",
                   help="Run headless. Works here — this engine never sends "
                        "the `HeadlessChrome` UA token that Medium's "
                        "Cloudflare hard-blocks (11/11 refused with it, 0/11 "
                        "without). Measured 6/8 served against headful's "
                        "8/8, so expect more retries. Needed on a machine "
                        "with no display.")
    args = p.parse_args()
    env_config.apply(args)
    if not args.url:
        p.error("no --url given, and MEDIUM_URL is not set in the environment "
                "or in .env.")
    why = unsupported_reason(args.url)
    if why:
        p.error(why)

    normalized = normalize_url(args.url)
    if normalized != args.url:
        logger.info("Fetching %s instead of %s — Medium's own tracking "
                    "parameters are stripped so one story has one address.",
                    normalized, args.url)
        args.url = normalized

    kind = listing_kind(args.url)
    if args.mode is None:
        args.mode = kind
        logger.info("Reading %s as a %s feed.", args.url, args.mode)
    elif args.mode != kind:
        p.error(f"--mode {args.mode} does not match {args.url!r}, which is a "
                f"{kind} page. The mode follows the URL on this site; leave "
                f"it off and it is inferred.")
    if args.pages > PAGE_CAP:
        logger.warning("--pages %d is above this scraper's %d-batch cap; it "
                       "will stop there.", args.pages, PAGE_CAP)
    if args.mode != "archive" and args.pages > 1:
        # Only the day archive has per-page addresses; the other three extend
        # over a POST to medium.com/_/graphql, and from a refused address
        # every one of those came back 403 while the page came back 200.
        logger.info(
            "--pages above 1 only fetches more in archive mode. A %s feed "
            "has no address for batch 2 and extends over an XHR that a "
            "refused address does not get. For volume use a day archive: "
            "/tag/{slug}/archive/{yyyy}/{mm}/{dd} — up to 128 stories per "
            "fetch, and --concurrency works there.", args.mode)
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key.")
        sys.exit(2)
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
