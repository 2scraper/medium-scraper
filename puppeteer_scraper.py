#!/usr/bin/env python3
"""medium-scraper — pyppeteer edition

A parity engine. It must agree with playwright_scraper.py and
selenium_scraper.py on exit codes, run status, and whether a run crashes or
spends money — the shared modules are what keep it honest, and this file
holds only "how to ask pyppeteer", plus the async-to-sync bridge that lets it
share a synchronous run loop with its twins.

Read playwright_scraper.py's docstring for what is different about this SITE.
Three things are different about this ENGINE:

* **PASS `--chromium-path`.** pyppeteer's own bundled Chromium is build
  117.0.5938.0 — two years old — and because the User-Agent is built from the
  browser's OWN version (§8: never a hardcoded literal), this engine
  truthfully announces `Chrome/117`. Medium's Cloudflare refused that three
  times out of three. Pointed at an installed Chrome with `--chromium-path`
  the same engine returned 53 rows on the first attempt. This is not a
  convenience flag here; it is the difference between data and exit 3.

* **pyppeteer is effectively unmaintained** and its own README points at
  Playwright. It is here for parity and for anyone already committed to it.

* **`--concurrency` above 1 is not implemented here.** The worker pool lives
  in playwright_scraper.py; this engine walks the archive days one at a time
  and produces identical output.

Measured live 2026-09-16: 53 rows from `/tag/python` with `--chromium-path`
pointing at the installed Chrome; blocked on all three attempts without it.

Examples
--------
    python3 puppeteer_scraper.py --url "https://medium.com/tag/python" \\
        --chromium-path "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

    python3 puppeteer_scraper.py \\
        --url "https://medium.com/tag/python/archive/2026/09/10" --pages 3 \\
        --chromium-path /usr/bin/google-chrome
"""

import argparse
import asyncio
import concurrent.futures
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

# At module level, deliberately, and not inside the launch path. The offline
# suite guards `import puppeteer_scraper` behind try/except ImportError and
# REPORTS the skip, and CI's engine-smoke job fails on any reported skip —
# that whole mechanism only works if importing this module actually requires
# the driver. With the import hidden inside _Session.open(), the module
# imports cleanly with no pyppeteer installed at all, the group never skips,
# and CI cannot notice a broken import (§10).
from pyppeteer import launch, connect

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
logger = logging.getLogger("puppeteer_scraper")

FIELD_FLOOR = 90

# Every await in this file goes through the bridge below with a timeout, so a
# hung remote call ends the operation instead of the run. pyppeteer provides
# no connect timeout of its own and its page methods' `timeout` option does
# not cover a browser that has stopped answering at all.
DEFAULT_OP_TIMEOUT = 120
# 150, not 30, and kept identical to the Playwright engine's
# CDP_CONNECT_TIMEOUT_MS — see its comment. Short version: the upgrade was
# measured hanging 121s before the SERVER hung up, so 30s gives up while the
# server is still working. It is NOT a cure for `profile_locked`, which was
# observed with no timed-out connect in its history at all.
CONNECT_TIMEOUT = 150


class _AsyncBridge:
    """Runs pyppeteer's coroutines on a private event loop, synchronously.

    Exists so this engine can reuse page_flow.py unchanged. That module holds
    the policy all three engines must share, and it is written against plain
    synchronous callables — the right shape for two of the three drivers.
    Bridging here keeps the policy in one place rather than growing an async
    copy of it that would drift.

    The second benefit is what the family's rules actually require: every
    call gets an explicit, enforced timeout. `.result(timeout)` returns
    control even when the browser never answers, which pyppeteer's own API
    does not offer.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="pyppeteer-loop")
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        # pyppeteer leaves CDP calls in flight when a browser closes, and the
        # loop then logs each one at ERROR level, AFTER a successful run has
        # printed its results. Five of those under a "Saved 95 products" line
        # read as a failed run. Only that shape is swallowed; anything else
        # still gets the default handler, because silencing the loop
        # wholesale would hide real faults.
        self.loop.set_exception_handler(self._on_loop_exception)
        self.loop.run_forever()

    @staticmethod
    def _on_loop_exception(loop, context):
        # BOTH, not one or the other. asyncio puts its own words in
        # `message` ("Future exception was never retrieved") and the
        # library's in `exception`, and an `or` between them looks at the
        # exception and never sees the message — which is why these kept
        # printing after they were "handled".
        message = " | ".join(
            str(context.get(k)) for k in ("exception", "message")
            if context.get(k))
        if any(m in message for m in (
                "Target closed", "Connection closed",
                "Task was destroyed but it is pending",
                "Future exception was never retrieved",
                "No session with given id",
                "Event loop is closed")):
            logger.debug("Ignoring teardown noise from pyppeteer: %s", message)
            return
        loop.default_exception_handler(context)

    def run(self, coro, timeout: Optional[float] = DEFAULT_OP_TIMEOUT):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(
                f"pyppeteer call did not return within {timeout}s")

    def close(self):
        """Stop the loop, CANCELLING whatever it still has in flight.

        Stopping the loop outright leaves pyppeteer's background tasks
        pending — its websocket reader and keepalive — and asyncio then
        prints "Task was destroyed but it is pending!" plus a traceback for
        each. That happens AFTER the output is written, so the run is fine
        and the log looks like a crash.

        Cancelling first is the fix, and it has to happen ON the loop thread —
        `call_soon_threadsafe` is what gets it there.
        """
        def _cancel_and_stop():
            pending = [t for t in asyncio.all_tasks(self.loop)
                       if t is not asyncio.current_task(self.loop)]
            for task in pending:
                task.cancel()
            if pending:
                logger.debug("Cancelled %d pending pyppeteer task(s) on "
                             "teardown.", len(pending))
            self.loop.stop()

        self.loop.call_soon_threadsafe(_cancel_and_stop)
        self._thread.join(timeout=5)


@dataclass
class PageOutcome:
    """What one page produced. Mirrors playwright_scraper.PageOutcome."""
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


_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _chrome_ua(version: str) -> str:
    """A desktop-Chrome UA from the browser's own reported version.

    `browser.version()` returns "HeadlessChrome/115.0.0.0"; the marketing
    part is what a real Chrome would send.
    """
    number = version.split("/")[-1] if "/" in version else version
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{number} Safari/537.36")


class _Session:
    """One pyppeteer browser + page, relaunchable onto a different exit."""

    def __init__(self, bridge: _AsyncBridge, args, pool):
        self.bridge, self.args, self.pool = bridge, args, pool
        self.remote = bool(args.cdp_endpoint)
        self.browser = self.page = None

    def open(self):
        if self.remote:
            logger.info("Connecting to an existing browser over CDP: %s",
                        _mask_credentials(self.args.cdp_endpoint))
            # pyppeteer's browserWSEndpoint takes the full ws://user:pass@host
            # form and authenticates on the WebSocket upgrade, so an
            # authenticated Scraping Browser endpoint works here — unlike
            # Selenium's debuggerAddress, which has nowhere to put a password.
            self.browser = self.bridge.run(
                connect(browserWSEndpoint=self.args.cdp_endpoint,
                        ignoreHTTPSErrors=True),
                timeout=getattr(self.args, "cdp_connect_timeout",
                                CONNECT_TIMEOUT))
            self.page = self.bridge.run(self.browser.newPage())
            _watch_graphql(self)
            return self

        launch_args = ["--no-sandbox", "--disable-dev-shm-usage",
                       "--disable-blink-features=AutomationControlled"]
        launch_kwargs = {}
        if self.args.chromium_path:
            launch_kwargs["executablePath"] = self.args.chromium_path
            logger.info("Using the browser at %s instead of pyppeteer's own.",
                        self.args.chromium_path)
        credentials = None
        if self.pool:
            exit_url = self.pool.current
            # Credentials go through page.authenticate(), never onto the
            # command line: --proxy-server= becomes part of the browser's
            # argv, readable by anything that can run `ps`.
            scrubbed, credentials = split_credentials(exit_url)
            launch_args.append(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(exit_url))

        # handleSIGINT/TERM/HUP off, and not for tidiness: pyppeteer installs
        # signal handlers inside launch(), and `signal.signal` raises "signal
        # only works in main thread of the main interpreter" because the
        # event loop here lives on a worker thread. Teardown is handled by
        # _Session.close() in scrape()'s finally block instead.
        self.browser = self.bridge.run(
            launch(headless=self.args.headless, args=launch_args,
                   ignoreHTTPSErrors=True, handleSIGINT=False,
                   handleSIGTERM=False, handleSIGHUP=False, **launch_kwargs),
            timeout=CONNECT_TIMEOUT * 2)
        self.page = self.bridge.run(self.browser.newPage())
        version = self.bridge.run(self.browser.version())
        self.bridge.run(self.page.setUserAgent(_chrome_ua(version)))
        # 1440x900, matching the captures. The viewport decides how far the
        # feed has to be scrolled before Medium fetches the next batch, so
        # keeping the window the measured size keeps the README's numbers
        # meaningful.
        self.bridge.run(self.page.setViewport({"width": 1440, "height": 900}))
        if credentials:
            self.bridge.run(self.page.authenticate(
                {"username": credentials[0], "password": credentials[1]}))
        _watch_graphql(self)
        return self

    def relaunch(self):
        if self.remote:
            return
        try:
            self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error while closing browser: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.bridge.run(self.page.close(), timeout=30)
            else:
                self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


# ---------------------------------------------------------------------------
# page_flow, bound to pyppeteer
# ---------------------------------------------------------------------------
# Only "how to ask this driver" lives here. pyppeteer takes `() => expr` like
# Playwright and unlike Selenium, which is exactly why page_flow names
# operations rather than passing JavaScript across the boundary (§1).
def _count(session, selector: str) -> int:
    try:
        return len(session.bridge.run(session.page.querySelectorAll(selector)))
    except Exception as e:  # noqa: BLE001
        logger.debug("count(%s) failed: %s", selector, e)
        return 0


def _sleep(ms: int) -> None:
    time.sleep(ms / 1000.0)


# Each scroll batch after the first arrives over POST /graphql, and that
# endpoint can be refused while the HTML keeps answering 200. Counting the
# refusals is what separates a feed that ran out (COMPLETE) from one whose
# next batch was refused (PARTIAL) — without it the two are the same
# observation and a throttled run reports "complete" holding its first batch
# (§7). All three engines count this, so all three reach the same verdict.
_GRAPHQL_PATH = "/graphql/"


def _watch_graphql(session) -> None:
    session._graphql_refused = 0

    def _on_response(response):
        try:
            if _GRAPHQL_PATH in response.url and response.status >= 400:
                session._graphql_refused += 1
        except Exception:  # noqa: BLE001 — a listener must never break a run
            pass

    try:
        session.page.on("response", _on_response)
    except Exception as e:  # noqa: BLE001
        logger.debug("Could not attach the response listener: %s", e)


def _graphql_refused_count(session) -> int:
    return getattr(session, "_graphql_refused", 0)


def _content(session) -> Optional[str]:
    try:
        return session.bridge.run(session.page.content())
    except Exception as e:  # noqa: BLE001
        logger.debug("content() unavailable (page navigating?): %s", e)
        return None


def _current_url(session) -> str:
    try:
        return session.bridge.run(session.page.evaluate("() => location.href"))
    except Exception:  # noqa: BLE001
        return ""


def _scroll_to_bottom(session) -> None:
    """Scroll the WINDOW to the end of the document. See page_flow."""
    try:
        session.bridge.run(session.page.evaluate(
            "() => window.scrollTo(0, document.body.scrollHeight)"))
    except Exception as e:  # noqa: BLE001
        logger.debug("scroll failed: %s", e)


def _page_height(session) -> Optional[int]:
    try:
        return session.bridge.run(session.page.evaluate(
            "() => document.body.scrollHeight"))
    except Exception:  # noqa: BLE001
        return None


def _ready_selector(args) -> str:
    return page_flow.ready_selector(args.mode)


def _min_matches(args, html: str = "") -> int:
    return page_flow.min_matches(args.mode, page_flow.tag_total_posts(html))


def _classify(session, html: str, status=None) -> str:
    # `status` POSITIONAL and second, matching the other two engines and the
    # callee's real signature (§17).
    return page_flow.classify(html, status, _current_url(session))


def _parse_for_mode(html: str, url: str, args, page_num: int = 1) -> List:
    return parse_posts(html, url, page=page_num, mode=args.mode)


def handle_captcha_if_present(session, args) -> bool:
    """Detect and solve a challenge. True if something was solved."""
    html = _content(session)
    if html is None:
        return False

    already_rendered = _count(session, _ready_selector(args))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    url = _current_url(session)
    html_challenge = detect_recaptcha_v3(html, url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: session.bridge.run(session.page.evaluate(js)), page_url=url)
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

    session.bridge.run(session.page.evaluate(INJECT_TOKEN_JS, token))
    logger.info("Token injected. Reloading page to continue.")
    _sleep(1500)
    try:
        session.bridge.run(session.page.reload(
            {"waitUntil": "domcontentloaded", "timeout": 60000}))
    except Exception as e:  # noqa: BLE001
        logger.warning("Reload after the solve failed: %s", e)
    return True


def _scroll_the_feed(session, args, html: str, page_num: int) -> dict:
    """Scroll to the bottom until the feed stops growing.

    A BOUNDED SAFETY NET on this site rather than the main event — see the
    Playwright engine's copy for the measurements. No `target`: the only
    total Medium publishes is the tag's whole catalogue since 2003, which is
    not a target for one page.
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
    warning = page_flow.feed_not_extended_warning(args.mode,
                                                  trace["cards_after"], trace)
    if warning:
        logger.info("%s", warning)
    return {"first_paint": before, "reached": trace["cards_after"],
            "rounds": trace["rounds"], "settled": True}


def _fetch_one_page(session, args, pool, page_num: int,
                    url: Optional[str]) -> PageOutcome:
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
                    session.bridge.run(session.page.goto(
                        url, {"waitUntil": "domcontentloaded",
                              "timeout": 60000}))
                    load_failed = False
                    break
                except Exception as e:  # noqa: BLE001
                    load_failed = True
                    if attempt < args.retries:
                        pause = args.retry_delay * (2 ** (attempt - 1))
                        logger.warning("Timeout loading %s (attempt %d/%d: "
                                       "%s) — retrying in %.1fs.", url,
                                       attempt, args.retries, str(e)[:120],
                                       pause)
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
                    # NOT the end of the listing — see the Playwright engine
                    # for the measurement. Reporting a refusal as "exhausted"
                    # would make a throttled run say "complete".
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
            session.bridge.run(session.page.screenshot(
                {"path": f"{args.out}_page{page_num}_debug.png"}))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        served = served_by_medium(html or "")
        vendor = detect_bot_challenge(html or "", url=_current_url(session))
        logger.error(
            "The site did not serve this request — %d bytes, %s the site's "
            "own asset hosts, saved to %s. This is exit 3, distinct from a "
            "genuinely empty result (exit 4).", len(html or ""),
            "which references" if served else "with no reference to",
            debug_html)
        if not args.chromium_path and not args.cdp_endpoint:
            logger.error(
                "This engine launched pyppeteer's OWN Chromium, which is the "
                "build this site was measured refusing. Pass --chromium-path "
                "pointing at an installed Chrome before concluding anything "
                "about the address.")
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
            logger.info("No answer cards appeared within %.0fs. If this feed "
                        "genuinely holds nothing, that is the expected "
                        "answer and the run will report 0 rows (exit 4).",
                        content_timeout / 1000)
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

    bridge = _AsyncBridge()
    session = _Session(bridge, args, pool).open()
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
                    logger.warning("Stopping at the %d-batch cap.", PAGE_CAP)
                    stop_reason = "page_cap"
                    break
                if pool and pool.rotates_per_page() and page_num == 2:
                    logger.warning(
                        "--proxy-rotate per-page cannot be honoured on a "
                        "Medium feed: batch %d exists only inside this "
                        "browser's session (it is reached by scrolling, not "
                        "by an address), so relaunching on another exit "
                        "would restart the feed at its first batch.",
                        page_num)
                time.sleep(args.delay)
                outcome = _fetch_one_page(session, args, pool, page_num, None)
                outcomes.append(outcome)

                if outcome.state == "exhausted":
                    logger.info("The feed stopped growing after batch %d, "
                                "with nothing refused behind it — treating "
                                "that as the end of the "
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
        bridge.close()

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
        description="Medium story scraper (pyppeteer edition)")
    p.add_argument("--url", default=None,
                   help="A Medium URL: a topic (/topic/{Slug}), a question "
                        "(/{Question-Slug}) or a profile (/profile/{Slug}). "
                        "An answer permalink is accepted and read as its "
                        "question. Required, unless MEDIUM_URL is set in the "
                        "environment or .env.")
    p.add_argument("--mode", choices=["tag", "archive", "author", "post"],
                   default=None,
                   help="Which view the URL is. Inferred from the URL by "
                        "default; passing one that disagrees is an error.")
    p.add_argument("--category", default=None,
                   help="Label to tag output rows with. Filled from the URL "
                        "by default.")
    p.add_argument("--pages", type=int, default=1,
                   help=f"Number of scroll BATCHES to walk (default 1, cap "
                        f"{PAGE_CAP}). Medium has no per-page address in any "
                        f"mode.")
    p.add_argument("--delay", type=float, default=3.0,
                   help="Delay between batches, seconds (default 3.0).")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Accepted for family compatibility and REFUSED above "
                        "1: a Medium feed has no per-batch address.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3).")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first page-load retry, doubling "
                        "thereafter (default 2.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="medium_posts", help="Output file prefix")
    p.add_argument("--chromium-path", default=None, metavar="PATH",
                   help="Use the browser at PATH instead of pyppeteer's own "
                        "Chromium. A convenience on this site rather than a "
                        "requirement: Medium was measured serving a bundled "
                        "Chromium the full feed. e.g. "
                        "'/Applications/Google Chrome.app/Contents/MacOS/"
                        "Google Chrome' or /usr/bin/google-chrome.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL. Credentials are sent over CDP "
                        "(page.authenticate), never on the command line.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line. Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES),
                   default="per-run",
                   help="per-run (default). per-page cannot be honoured "
                        "mid-listing here.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="Retries from other exits when a page is refused.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
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
                   help="Connect to a running browser over CDP, e.g. "
                        "ws://user:pass@host:port. pyppeteer authenticates on "
                        "the WebSocket upgrade, so the Scraping Browser API "
                        "endpoint works from this engine.")
    p.add_argument("--cdp-connect-timeout", type=float,
                   default=CONNECT_TIMEOUT, metavar="SECONDS",
                   help=f"How long to wait for --cdp-endpoint to accept the "
                        f"connection (default {CONNECT_TIMEOUT}). Deliberately "
                        f"high, and identical to the Playwright engine's: a "
                        f"Scraping Browser provisions a browser when the "
                        f"WebSocket upgrade arrives, and one was measured "
                        f"taking 121s before the SERVER gave up. Giving up "
                        f"earlier than the server does leaves the profile held "
                        f"by a half-open session.")
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
            "fetch.", args.mode)
    return args


if __name__ == "__main__":
    args = parse_args()
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
