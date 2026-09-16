"""page_flow.py — what to do with the page Medium just gave us.

Medium answers a request five ways, and four of them want a different
response, which is why this module exists rather than the same triage being
written three times inside three engines and drifting apart (§1):

    content    one of Medium's two payloads describes stories, or its own
               cards are rendered on a page built out of its own assets
    empty      Medium's own "no stories" copy
    shell      served, built out of Medium's assets, nothing shipped or
               painted yet. Wants a WAIT, not a refetch
    challenge  Cloudflare's managed interstitial. Worth RETRYING in a FRESH
               CONTEXT and worth nothing to a solver — see the block
               constants below
    blocked    Cloudflare's WAF refusal, or a page that is not Medium's

The policy lives in `STATE_POLICY` as DATA, so an engine cannot quietly
disagree with its twins about whether a page is worth retrying or worth
paying for.

`content` is the NORMAL first state here
----------------------------------------
Unlike its sibling repos' sites, Medium server-renders its data. The first
response of a tag feed already carries `__APOLLO_STATE__` with 34 stories in
it, and a day archive carries `window["obvInit"]` with 128 — before a single
pixel has painted and before any scrolling. So the readiness wait and the
scroll loop below are both BOUNDED SAFETY NETS rather than the main event,
and a run that parsed the first response and stopped would already have the
data.

That is measured rather than assumed, and the measurement is unflattering to
the scroll: every one of the 11 POSTs to `medium.com/_/graphql` came back 403
from the address these numbers were taken on, so the infinite scroll that
would extend a feed extended nothing. A tag feed scrolled twelve times to a
stable height held exactly the 34 stories its first response shipped.

Everything here is pure or driven through small callables, so each engine
passes its own driver's primitives and keeps its browser plumbing to itself:

    count(selector) -> int              how many elements match
    scroll_to_bottom() -> None          scroll the window to the document end
    page_height() -> Optional[int]      document.body.scrollHeight
    sleep(ms) -> None                   wait

No JavaScript crosses that boundary in either direction (§1): Selenium's
`execute_script` takes a function BODY with an explicit `return` where
Playwright and pyppeteer take `() => expr`, so this module names the
OPERATION and each engine spells it in its own driver's dialect.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from product_parser import (SELECTORS, PAGE_CAP, CONCURRENCY_REASON,
                            PAGE_URL_REASON, detect_block_marker,
                            detect_page_state, is_challenge_page,
                            obvinit_payload, paginates_by_url,
                            posts_from_apollo, posts_from_obvinit,
                            served_by_medium)

logger = logging.getLogger("page_flow")


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
READY_SELECTOR = SELECTORS["item_card"]

# Above 1, per §5: waiting for a single match resolves on the page's own
# first card long before a feed paints. Measured first paints: 15 cards on a
# tag feed, 10 on an author profile, 128 on a day archive, 1 on a post page —
# which is why `min_matches` clamps for the mode that genuinely has one.
MIN_CARD_MATCHES = 2

# Generous against a measured first paint of 3-6s, and against a day archive
# whose response is 2.3 MB and takes visibly longer to parse than to fetch.
CONTENT_TIMEOUT_MS = 25_000


def ready_selector(mode: str = "") -> str:
    """One selector for every mode: `article, .streamItem` covers both of
    Medium's renderers, and no mode renders something neither matches."""
    return READY_SELECTOR


def min_matches(mode: str = "", expected: Optional[int] = None) -> int:
    """How many matches mean "painted".

    Clamped to 1 in post mode, where the page has exactly one card and
    waiting for two spends the whole timeout on a page that was ready
    immediately. `expected` clamps it further for a caller that knows better.
    """
    floor = 1 if mode == "post" else MIN_CARD_MATCHES
    if expected is None or expected <= 0:
        return floor
    return max(1, min(floor, expected))


def content_timeout_ms(mode: str = "") -> int:
    return CONTENT_TIMEOUT_MS


def wait_for_count(count: Callable[[str], int], sleep: Callable[[int], None],
                   selector: str, minimum: int, timeout_ms: int,
                   poll_ms: int = 500) -> int:
    """Poll until `minimum` elements match, or the timeout runs out.

    Polls through the driver's own element-count primitive rather than
    waiting on an evaluated STRING. That is not a style preference: a
    sibling repo's site ships a Content-Security-Policy without
    `unsafe-eval`, and Playwright's `wait_for_function` — which hands the
    browser a string to evaluate — died there with an `EvalError` and took
    the whole run down with exit 1. Counting elements over the protocol works
    under any CSP and spells the same in all three drivers.

    Returns the count it ended on, whether or not it reached the floor:
    reporting a timeout is not the same as exiting on one (§8).
    """
    waited = 0
    found = count(selector)
    while found < minimum and waited < timeout_ms:
        sleep(poll_ms)
        waited += poll_ms
        found = count(selector)
    if found < minimum:
        logger.info("readiness wait ended at %d matches (wanted %d) after "
                    "%dms", found, minimum, waited)
    return found


# ---------------------------------------------------------------------------
# Lazy loading
# ---------------------------------------------------------------------------
# §8's second case. Scroll to `document.body.scrollHeight` rather than
# wheeling a fixed distance — a fixed wheel stops short on a long grid and
# the trigger is never reached — and require the count AND the height to hold
# still for THREE rounds, because the next batch takes longer to arrive than
# a single pause.
SCROLL_STABLE_ROUNDS = 3
SCROLL_MAX_ROUNDS = 12
SCROLL_PAUSE_MS = 2_000


def scroll_until_settled(count: Callable[[str], int],
                         scroll_to_bottom: Callable[[], None],
                         page_height: Callable[[], Optional[int]],
                         sleep: Callable[[int], None],
                         selector: str = "",
                         max_rounds: int = SCROLL_MAX_ROUNDS) -> Dict[str, int]:
    """Scroll to the bottom until neither the card count nor the height moves.

    Returns a small trace — rounds spent, cards before and after — which goes
    into the run's sidecar. On this site that trace is usually "12 rounds, 34
    cards, 34 cards", and a reader seeing it should conclude the site refused
    to extend rather than that the scroll failed: see the module docstring.
    """
    selector = selector or READY_SELECTOR
    started = count(selector)
    stable = 0
    last_height = None
    rounds = 0
    while rounds < max_rounds and stable < SCROLL_STABLE_ROUNDS:
        before = count(selector)
        scroll_to_bottom()
        sleep(SCROLL_PAUSE_MS)
        rounds += 1
        height = page_height()
        after = count(selector)
        if after == before and height == last_height:
            stable += 1
        else:
            stable = 0
        last_height = height
    ended = count(selector)
    return {"rounds": rounds, "cards_before": started, "cards_after": ended}


# ---------------------------------------------------------------------------
# Classification and the policy that follows from it
# ---------------------------------------------------------------------------
def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "") -> str:
    """Which of the five states this response is.

    `status` is positional and comes SECOND, matching `detect_page_state`.
    Getting that wrong is not a style question: a sibling repo shipped two of
    three engines calling this as `classify(html, url=...)`, both crashed on
    their first fetch, and nothing short of a live run or a signature-binding
    check saw it (§17). This repo's smoke suite binds every shared-module
    call in every engine for that reason.
    """
    if html is None:
        return "blocked"
    return detect_page_state(html, status, url)


# The retry/solve/blocked decision as DATA rather than as three copies of an
# if-chain in three engines (§1).
#
#   parse    is there anything on this page worth writing down?
#   retry    would fetching it again, in a fresh context, plausibly help?
#   solve    is there something to pay a solver for?
#   blocked  does this count towards exit 3?
STATE_POLICY: Dict[str, Dict[str, bool]] = {
    "content":   {"parse": True,  "retry": False, "solve": False, "blocked": False},
    # The site was asked and answered. Not retried: a second fetch of a tag
    # with no stories returns the same tag with no stories.
    "empty":     {"parse": False, "retry": False, "solve": False, "blocked": False},
    # Served and still painting. Parsed rather than discarded, because by the
    # time an engine asks, the readiness wait has already run — and because
    # on this site a shell that carries a payload has already classified as
    # `content`, so reaching `shell` at all means the payload was absent and
    # the DOM is the only thing that can still arrive.
    "shell":     {"parse": True,  "retry": False, "solve": False, "blocked": False},
    # Retried, NOT solved, and counted as blocked if the retries run out.
    #
    # Retried because it is transient and clears: of 27 first attempts across
    # the capture runs, 9 came back as the interstitial and every one of the
    # 9 was served in full on the next attempt. Counted as blocked if it
    # survives, because a challenge that never clears is a refusal, and
    # letting it through hands a 28 KB interstitial to the parser and reports
    # exit 4 — "ran fine, found nothing" — on a tag holding hundreds of
    # thousands of stories. Blocked is not empty (§8).
    "challenge": {"parse": False, "retry": True,  "solve": False, "blocked": True},
    "blocked":   {"parse": False, "retry": True,  "solve": False, "blocked": True},
}


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["parse"]


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["blocked"]


def is_unpainted(state: str, html: Optional[str]) -> bool:
    """Whether this page is served but has not painted its feed yet."""
    if state != "shell":
        return False
    return served_by_medium(html or "")


# ---------------------------------------------------------------------------
# Blocks, and what actually clears one
# ---------------------------------------------------------------------------
# Medium refuses in two distinguishable ways, and they want different
# responses. Both measured 2026-09-16 from one DATACENTRE address:
#
#   the WAF refusal     HTTP 403, 5 KB, `<title>Attention Required! |
#                       Cloudflare</title>`, "Sorry, you have been blocked",
#                       `cf-error-details`. Nothing to solve, nothing to wait
#                       for. Served to `curl` and to EVERY headless browser.
#
#   the managed         HTTP 403, 28 KB, `<title>Just a moment...</title>`,
#   challenge           `_cf_chl_opt = {… cType: 'managed' …}`. Transient.
#
# Three facts drive the constants below, and each is a constant rather than a
# paragraph an engine might not read (§17: a policy constant nothing consults
# is the same defect as dead code):
#
#   * THE USER-AGENT IS THE DISCRIMINATOR, not the address and not headless
#     as such. A bare headless Chromium sending its default UA — the one
#     carrying the `HeadlessChrome` token — was WAF-blocked on 11 of 11
#     attempts. The same browser with an ordinary Chrome UA was never
#     WAF-blocked. The engines build their UA from the browser's own version
#     (§8), so they never send that token; `curl` and a naive headless
#     script do, which is why they get nothing. Headful is still the default
#     on a smaller margin: 8/8 served against headless's 6/8, interleaved.
#
#   * A FRESH CONTEXT CLEARS THE CHALLENGE; A RELOAD DOES NOT. Waiting 9
#     seconds inside the same context never cleared it once. Tearing the
#     context down and opening a new one cleared it on the next attempt in 9
#     of 9 cases. So a retry here must be a new context, which is the same
#     shape as this family's "a rotation is a fresh browser" rule (§8),
#     applied to a case with no proxy in it.
#
#   * IT IS NOT SOLVABLE. Measured on the interstitial Medium served: 0
#     `data-sitekey` attributes, 0 iframes, and `cType: 'managed'` in
#     Cloudflare's own config. There is nothing to hand 2Captcha, so
#     `should_solve` is False for this state and nothing is ever charged
#     for it.
RETRY_ON_BLOCKED = True
BLOCK_RETRIES_WITHOUT_POOL = 3
BLOCK_RETRIES_WITH_POOL = 4
SOLVES_PER_PAGE = 1

# Whether a retry has to discard the browser context rather than reload the
# page. True here on the measurement above. Consulted by all three engines;
# a constant no engine read would be the §17 defect this comment warns about.
RETRY_NEEDS_FRESH_CONTEXT = True


def block_advice(html: Optional[str], headless: bool, has_pool: bool) -> str:
    """What a reader should actually DO about this block.

    Ordered by what the measurements say helps most, which on this site is
    not "buy a proxy": it is "use a real window".
    """
    marker = detect_block_marker(html or "") or "HTTP 403"
    lead = f"blocked ({marker})"
    hints: List[str] = []

    if headless:
        hints.append("try a real window: --headful was served 8/8 against "
                     "headless's 6/8, interleaved on one address. Note this "
                     "engine already avoids the thing Medium's Cloudflare "
                     "HARD-blocks — the `HeadlessChrome` token in the "
                     "User-Agent, 11 of 11 refused with it and 0 of 11 "
                     "without — so a headless run here fails the same way a "
                     "headful one does, just a little more often")

    if is_challenge_page(html or ""):
        lead = "blocked by Cloudflare's managed challenge"
        hints.append("it is transient and it clears in a FRESH BROWSER "
                     "CONTEXT, not on a reload: 9 of 27 first attempts were "
                     "challenged and all 9 were served in full on the next "
                     "attempt, while waiting inside the same context never "
                     "cleared one. --retries already opens a new context "
                     "between attempts")
        hints.append("no solve was attempted and nothing was charged — a "
                     "managed challenge publishes no sitekey (0 "
                     "data-sitekey attributes, 0 iframes, cType 'managed'), "
                     "so there is nothing for a captcha solver to answer")

    if not has_pool:
        hints.append("if a rested address keeps being refused, the rate is "
                     "what this site limits on: raise --delay, or spread the "
                     "load with --proxy-file")
    else:
        hints.append("with a pool in play, raise --delay before raising the "
                     "request rate: N exits still means N times the traffic")
    return lead + ". " + "; ".join(hints) + "."


# ---------------------------------------------------------------------------
# Pagination — one mode has addresses, three do not
# ---------------------------------------------------------------------------
# `--mode archive` walks `/tag/{slug}/archive/{yyyy}/{mm}/{dd}` backwards a
# day at a time. Those are real, independent addresses: 128 stories in one
# fetch, `paging` empty, and page 5 knowable without fetching page 4 — which
# is what makes `--concurrency` meaningful there and nowhere else (§7).
#
# The other three modes have no per-batch address at all. `?page=2` on a tag
# feed is not an error, it is IGNORED, and a run built on it would add no new
# sku, call the listing exhausted and report COMPLETE holding one page (§18).
# For those modes a "page" is one settled scroll batch, and the terminating
# condition is the DATA one §7 asks for: a batch that adds no unseen sku ends
# the listing.
NEXT_BATCH_TIMEOUT_MS = 45_000
NEXT_BATCH_POLL_MS = 1_000

ADVANCED = "advanced"            # the feed grew
NO_GROWTH = "no_growth"          # scrolled, and nothing new arrived


def advance_feed(scroll_to_bottom: Callable[[], None],
                 count: Callable[[str], int],
                 page_height: Callable[[], Optional[int]],
                 sleep: Callable[[int], None],
                 timeout_ms: int = NEXT_BATCH_TIMEOUT_MS) -> str:
    """Scroll once more and wait for the feed to actually grow.

    Returns ADVANCED or NO_GROWTH, and the two must not collapse into one
    bool: a sibling repo mapped both to False, the engine mapped False to
    `pagination_exhausted`, and a run that was being rate-limited reported
    status "complete" holding page 1 (§7).

    Readiness is the CARD COUNT growing. The document height is watched too
    but is not sufficient on its own — a page can grow by the height of a
    footer or a promo while no batch is in flight.
    """
    before = count(READY_SELECTOR)
    before_height = page_height()
    scroll_to_bottom()
    waited = 0
    while waited < timeout_ms:
        sleep(NEXT_BATCH_POLL_MS)
        waited += NEXT_BATCH_POLL_MS
        now = count(READY_SELECTOR)
        if now > before:
            logger.info("feed grew %d -> %d after %dms", before, now, waited)
            return ADVANCED
        # Keep asking: one scroll can land short of the trigger once the page
        # has grown underneath it.
        now_height = page_height()
        if now_height != before_height:
            before_height = now_height
            scroll_to_bottom()
    logger.info("feed did not grow within %dms — treating the listing as "
                "exhausted", timeout_ms)
    return NO_GROWTH


def page_cap_reached(page_num: int) -> bool:
    return page_num >= PAGE_CAP


# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------
def page_gap(html: Optional[str], parsed: int) -> Optional[int]:
    """None, always, on this site — and that is the honest answer.

    Medium publishes no per-page counter. What it does publish, on a day
    archive, is `tag.postCount` — 257,497 for `python` — and that is the size
    of the tag's whole catalogue since 2003, not of the day. Reading it as a
    gap would report a quarter of a million missing stories on a page that
    rendered everything it had.

    None rather than 0, because an unknown gap is not a gap of zero and the
    two must not read the same in a sidecar (§8).
    """
    return None


def tag_total_posts(html: Optional[str]) -> Optional[int]:
    """How many stories the tag holds IN TOTAL, where Medium says so.

    Not an expectation for this page — see `page_gap`. It goes in the
    sidecar's `extra` under its own name, so a consumer can see the size of
    the catalogue beside what the run actually read without mistaking one for
    the other.
    """
    tag = obvinit_payload(html).get("tag") or {}
    count = tag.get("postCount")
    return count if isinstance(count, int) and count > 0 else None


# ---------------------------------------------------------------------------
# A feed that did not extend is the site, not the scroll
# ---------------------------------------------------------------------------
# §8's third case: "a different page was served to this SESSION — no amount
# of waiting or scrolling helps". On Medium the mechanism is blunter than a
# session variant. The scroll fires a POST to `medium.com/_/graphql`, and
# from the address these numbers were taken on every one of those POSTs came
# back 403 while the HTML page itself came back 200. The browser scrolled,
# the request went out, Cloudflare refused it, and the feed stayed the length
# the server had rendered it.
#
# So a settled scroll that added nothing is CORRECT behaviour here and must
# not be made more patient to chase it: more rounds against a feed the site
# will not extend just spends time. It is reported instead.
def feed_not_extended_warning(mode: str, rows: int,
                              trace: Optional[Dict[str, int]]) -> Optional[str]:
    """A note when scrolling a scroll-paginated mode added no cards.

    Deliberately talks about CARDS and not about rows. The rows come from the
    page's payload, which scrolling does not touch — and on an author page
    the rendered card count was measured going 10 -> 0 across four scroll
    rounds, because Medium replaces that feed's DOM as it scrolls. A message
    that reported the card count as a story count would announce "0 stories"
    on a run that wrote ten complete ones.

    None for `archive`, which does not scroll for its rows — it walks day
    URLs — and None for `post`, which is one story by definition.
    """
    if mode in ("archive", "post") or not trace:
        return None
    before = trace.get("cards_before", 0)
    after = trace.get("cards_after", 0)
    if after > before:
        return None
    return (f"{trace.get('rounds', 0)} round(s) of scrolling added no cards "
            f"({before} -> {after}; a drop means Medium replaced the feed's "
            f"DOM, which costs this run nothing because the rows come from "
            f"the page's payload). That is the ADDRESS rather than the "
            f"scroll: the feed extends over a POST to medium.com/_/graphql, "
            f"and from a refused address every one of those came back 403 "
            f"while the page itself came back 200. Two things do change it, "
            f"both measured: --cdp-endpoint, where the same author page grew "
            f"10 cards to 60 across 8 rounds and yielded 55 rows against 10; "
            f"and --mode archive, where a tag's day URLs give up to 128 "
            f"stories per fetch from any address and can be walked with "
            f"--concurrency.")


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def concurrency_limit(url: str = "") -> Optional[int]:
    """No limit for a day-archive walk; 1 for everything else."""
    return None if paginates_by_url(url) else 1


def concurrency_refusal(url: str) -> Optional[str]:
    """Why concurrency above 1 is refused for this URL.

    Refused WITH the reason rather than silently running one worker, which
    would look like the flag did something.
    """
    if not paginates_by_url(url):
        return (f"{PAGE_URL_REASON}, so {CONCURRENCY_REASON}. Batch 5 of an "
                f"infinite scroll exists only inside the browser that "
                f"scrolled through batches 1-4. Point --url at a day archive "
                f"— /tag/{{slug}}/archive/{{yyyy}}/{{mm}}/{{dd}} — to use "
                f"workers, or run several tags in parallel, one process each")
    return None
