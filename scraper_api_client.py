#!/usr/bin/env python3
"""medium-scraper — 2captcha Scraper API edition (fourth engine)

One HTTP request per page, no local browser, no Playwright install. The
2captcha Scraper API fetches the page from its own infrastructure and returns
the HTML; this client parses it with the same `product_parser` the browser
engines use, so the rows and columns are identical.

WHAT IT GETS, AND WHAT IT CANNOT GET — measured 2026-09-15, $0.0005 a request
-----------------------------------------------------------------------------
This path is unusually good on one URL kind and useless on another, and the
reason is the same fact that shapes the whole repo: Medium renders nothing on
the server, but it does inline its own GraphQL results into the response.

    a QUESTION url    HTTP 200, 264,581 bytes
                      6 answers, 6 of 6 payload-backed
                      upvotes, views, creationTime, numeric ids and the FULL
                      answer text all populated

    a TOPIC url       HTTP 200, 99,833 bytes
                      3 inline payloads carrying 0 Answer objects
                      0 rendered cards -> 0 rows, classified `shell`

So it is the RICHEST path this repo has, per row, on a question page — better
than a browser engine's first batch, which reads most of its rows off cards
that carry none of those columns. And it cannot read a topic feed at all,
because a topic's answers arrive over a later XHR and this returns the served
response rather than a rendered DOM.

`--wait-element` does not change that, which was worth checking rather than
assuming: the same topic URL with `--wait-element 'a.answer_timestamp'` spent
sixteen seconds instead of three and returned the same 99,847-byte shell with
the same 0 cards. Whatever the API waits on, it is not handing back the
hydrated document.

What is missing on both is any completeness oracle — but the browser path has
none either, because Medium publishes no per-page counter anywhere (see
`page_flow.page_gap`). The difference is that a browser engine can keep
scrolling and this cannot.

So: reach for this when you want a question's answers cheaply and in full, and
use a browser engine when you want a topic feed or more than the first batch.

    python3 scraper_api_client.py \\
        --url "https://medium.com/What-is-machine-learning-4"

    python3 scraper_api_client.py \\
        --url "https://medium.com/topic/Machine-Learning" \\
        --wait-element 'a.answer_timestamp'

    # $TWOCAPTCHA_KEY is read from the environment or .env, so a key never
    # has to be typed — a secret in argv is readable by anything that can run
    # `ps` and lands in shell history.
"""

import argparse
import json
import logging
import os
import sys
import time
from typing import Optional

import requests

from product_parser import (parse_posts, detect_bot_challenge,
                            detect_page_state, listing_kind, normalize_url,
                            BOT_CHALLENGE_MARKERS)
from output_writer import save
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scraper_api_client")

API_BASE = "https://scraper.2captcha.com"
SYNC_ENDPOINT = f"{API_BASE}/tasks/sync"

# The API caps `timeout` at 120s and rejects bodies over 10,000 bytes.
MAX_API_TIMEOUT = 120

# Exit codes. Kept distinct from 2 (bad usage) on purpose: a remote API
# failing is not the operator passing wrong arguments, and a harness that
# lumps them together sends you looking in the wrong place. An early run
# reported `exit=2` for an HTTP 422 from the API — which reads as "you called
# it wrong".
#
# Imported rather than redefined: the browser engines return the same code for
# a Scraping Browser that will not accept a connection, and two definitions
# of one exit code is how a family's contract drifts.
from output_writer import EXIT_API_ERROR  # noqa: E402

def _mask_credentials(url: str) -> str:
    """Never print a username:password embedded in a ws://... or http://... URL."""
    if "@" not in url:
        return url
    scheme_sep = url.find("://")
    if scheme_sep == -1:
        return url
    scheme, rest = url[:scheme_sep + 3], url[scheme_sep + 3:]
    _, _, host_part = rest.partition("@")
    return f"{scheme}***:***@{host_part}"


def _build_wait_for(args) -> Optional[str]:
    """`waitFor` must be a JSON STRING (double-encoded), per the API docs.
    Passing a nested object is silently wrong.

    Default (no flag): wait for the DOM. On a challenge-protected page
    that resolves instantly against the challenge page itself — which is
    exactly the trap documented in this module's docstring, so
    --wait-text/--wait-element exist to wait on something only the real
    page can contain."""
    if args.wait_text:
        return json.dumps({"text": args.wait_text})
    if args.wait_element:
        return json.dumps({"element": args.wait_element, "checkVisible": True})
    if args.wait_state:
        return json.dumps({"state": args.wait_state})
    return None


def fetch_html(args) -> str:
    payload = {
        "task_type": "scrape",
        "url": args.url,
        "data_format": "raw",   # we want HTML; product_parser does the rest
        "format": "json",       # so we get {"status", "headers", "body"}
        "timeout": min(args.timeout, MAX_API_TIMEOUT),
    }

    wait_for = _build_wait_for(args)
    if wait_for:
        payload["waitFor"] = wait_for
        logger.info("waitFor: %s", wait_for)

    if args.cdp_url:
        payload["cdpurl"] = args.cdp_url
        logger.info("Routing through an existing browser session: %s",
                    _mask_credentials(args.cdp_url))

    logger.info("POST %s (url=%s)", SYNC_ENDPOINT, args.url)
    resp = requests.post(
        SYNC_ENDPOINT,
        headers={"Authorization": f"Bearer {args.key}", "Content-Type": "application/json"},
        json=payload,
        # Give the HTTP call more headroom than the API-side task timeout,
        # otherwise a task that legitimately runs the full 120s looks like
        # a client-side network failure.
        timeout=min(args.timeout, MAX_API_TIMEOUT) + 30,
    )

    # The API returns its own per-task metadata (price, timings, status)
    # in an x-debug header — worth logging, it's the only place the real
    # cost of the call shows up.
    debug = resp.headers.get("x-debug")
    if debug:
        logger.info("x-debug: %s", debug)

    if resp.status_code != 200:
        # 422 = task ran but errored (this is what a bad/unreachable
        # cdpurl produces: "CDP connect failed (user cdpurl) after N
        # attempts"); 402 = out of balance; 408 = sync wait exceeded.
        raise RuntimeError(
            f"Scraper API returned HTTP {resp.status_code}: {resp.text[:500]}"
        )

    body = resp.json()
    html = body.get("body") or ""
    upstream_status = body.get("status")
    logger.info("Upstream page status %s, %d bytes of HTML.", upstream_status, len(html))
    # The STATUS is returned alongside the HTML, not thrown away. It used to
    # be, and that cost this engine the family's central distinction. On this
    # site a refusal carries no markup at all — nothing a challenge check
    # on it, so the challenge check below finds nothing and the run fell
    # through to "0 products" and exit 4. A pipeline branching on the exit
    # code then reads a block as an empty category. See detect_page_state,
    # which the three browser engines already reach through page_flow.
    return html, upstream_status


def main() -> int:
    args = parse_args()

    if not args.key:
        logger.error("No 2captcha API key. Pass --key, or better, export TWOCAPTCHA_KEY.")
        return 2

    # A challenge page is not necessarily final (see _run_once), so a
    # single attempt is not evidence. Each retry is a fresh billable task —
    # $0.0005 at the observed rate — so the default is deliberately low.
    attempts = max(1, args.retries + 1)
    for attempt in range(1, attempts + 1):
        rc = _run_once(args, attempt, attempts)
        if rc != 3 or attempt == attempts:
            return rc
        logger.info("Challenge page on attempt %d/%d — retrying in %ds.",
                    attempt, attempts, args.retry_delay)
        time.sleep(args.retry_delay)
    return rc


def _run_once(args, attempt: int = 1, attempts: int = 1) -> int:
    if attempts > 1:
        logger.info("Attempt %d/%d", attempt, attempts)

    try:
        html, upstream_status = fetch_html(args)
    except requests.RequestException as e:
        logger.error("Network error talking to the Scraper API: %s", e)
        return EXIT_API_ERROR
    except RuntimeError as e:
        # HTTP 4xx/5xx from the API, including the 422 that a busy or
        # unreachable cdpurl produces.
        logger.error("%s", e)
        return EXIT_API_ERROR

    if args.dump_html:
        with open(args.dump_html, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Raw HTML written to %s", args.dump_html)

    # Same policy as the browser engines: the status decides the blocked
    # case, because this site's refusal has no marker to detect.
    state = detect_page_state(html, status=upstream_status, url=args.url)
    if state == "blocked":
        dump = f"{args.out}_scraperapi_debug.html"
        with open(dump, "w", encoding="utf-8") as f:
            f.write(html)
        logger.error(
            "The site did not serve the Scraper API's request (upstream "
            "HTTP %s, %d bytes) — saved to %s. Measured 2026-09-10: the "
            "Scraper API's own exit is a datacentre address, and this site "
            "answers those with NOTHING, while the same task routed through "
            "a Scraping Browser session returned 200 and 417 KB. Pass "
            "--cdp-url. This is exit 3, distinct from an empty result "
            "(exit 4).", upstream_status, len(html), dump)
        return 3

    vendor = detect_bot_challenge(html)
    if vendor:
        logger.error(
            "The Scraper API returned a %s bot-challenge page (%d bytes), not real content.",
            vendor, len(html),
        )
        logger.error("A challenge page is not a final answer — retry before concluding "
                     "anything (--retries). This site needs a rendered browser in the "
                     "path: pass --cdp-url, or use playwright_scraper.py / "
                     "puppeteer_scraper.py directly.")
        return 3

    products = parse_posts(html, args.url, mode=listing_kind(args.url))
    logger.info("Parsed %d answer(s).", len(products))

    if not products:
        dump = f"{args.out}_scraperapi_debug.html"
        with open(dump, "w", encoding="utf-8") as f:
            f.write(html)
        logger.warning("0 answers parsed — saved the raw response to %s so "
                       "you can see what actually came back. On a TOPIC URL "
                       "this is the expected result without --wait-element: "
                       "Medium sends a topic's answers over a later XHR, so a "
                       "fetch that does not render returns a shell. A "
                       "QUESTION URL carries its first answers in the "
                       "served payload and needs no wait.", dump)
        return 4

    return save(products, args.out, args.format, allow_empty=args.allow_empty)


def parse_args():
    p = argparse.ArgumentParser(
        description="Medium answer scraper — 2captcha Scraper API edition "
                    "(no local browser). Measured: a QUESTION url yields 6 "
                    "answers with every column populated for $0.0005, and a "
                    "TOPIC url yields ZERO — with or without --wait-element — "
                    "because a topic's answers arrive over a later XHR and "
                    "this returns the served response. Use a browser engine "
                    "for a topic feed.")
    # NOT required: prefer the TWOCAPTCHA_KEY env var. A key passed on the
    # command line is visible to anyone who can run `ps`, and it lands in
    # shell history and in any log that echoes the command line.
    p.add_argument("--key", default=os.environ.get("TWOCAPTCHA_KEY"),
                   help="2captcha.com API key (sent as a Bearer token). "
                        "Defaults to $TWOCAPTCHA_KEY, which is the safer way to pass it.")
    p.add_argument("--url", default=None,
                   help="A Medium question or profile URL. A TOPIC url "
                        "returns nothing here — measured, and --wait-element "
                        "does not help — because a topic's answers arrive "
                        "over a later XHR. Required, unless MEDIUM_URL is set "
                        "in the environment or in .env.")
    p.add_argument("--category", default=None, help="Label to tag output rows with. Defaults to the category segment of the URL, so the column is never empty just because the flag was omitted.")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="medium_posts_scraperapi", help="Output file prefix")
    p.add_argument("--timeout", type=int, default=60,
                   help=f"API-side task timeout in seconds (1-{MAX_API_TIMEOUT}, default 60)")
    p.add_argument("--cdp-url", default=None,
                   help="Route the fetch through an existing browser session over CDP "
                        "(sent as the API's `cdpurl` param), e.g. ws://user:pass@host:port")
    wait = p.add_mutually_exclusive_group()
    wait.add_argument("--wait-text", default=None,
                      help="Wait until this string appears on the page, e.g. '$'. Use this "
                           "on protected sites — a DOM/load wait is satisfied instantly by "
                           "the challenge page itself.")
    wait.add_argument("--wait-element", default=None,
                      help="Wait until this CSS selector is visible, e.g. 'a[href*=\"-item-\"]'")
    wait.add_argument("--wait-state", choices=["load", "domcontentloaded"], default=None,
                      help="Wait for a page load state instead of specific content")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 products were parsed. Off by "
                        "default so a failed fetch can't overwrite a good result.")
    p.add_argument("--retries", type=int, default=1,
                   help="Extra attempts if a bot-challenge page comes back. One retry is "
                        "usually worth it. Each attempt is a separate billable task, so "
                        "this defaults to 1.")
    p.add_argument("--retry-delay", type=int, default=10,
                   help="Seconds between retries (default 10)")
    p.add_argument("--dump-html", default=None,
                   help="Also write the raw returned HTML to this path (always, even on success)")
    args = p.parse_args()
    # This client uses --key and --cdp-url rather than --twocaptcha-key and
    # --cdp-endpoint, so the env mapping is spelled out instead of defaulted.
    env_config.apply(args, keys={
        "TWOCAPTCHA_KEY": "key",
        "MEDIUM_CDP_ENDPOINT": "cdp_url",
        "MEDIUM_URL": "url",
    })
    if not args.url:
        p.error("no --url given, and MEDIUM_URL is not set in the environment "
                "or in .env.")
    return args


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
