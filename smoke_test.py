#!/usr/bin/env python3
"""medium-scraper — offline smoke tests.

One file of plain functions with fixtures loaded from `fixtures_generated.json`,
no pytest required. `tests/test_smoke.py` wraps it as a single pytest test so
`pytest` works as an entry point without a second copy of the checks.

    python3 smoke_test.py

It MUST pass with no engine library installed at all: every
`import playwright_scraper` / `selenium_scraper` / `puppeteer_scraper` is
guarded and the skip is REPORTED, because "skipped, engine absent" reads
exactly like a passing run. CI's engine-smoke job installs each engine in its
own venv and fails if that skip list is non-empty.

The fixtures are cut from real captures by `make_fixtures.py`, which proves
each one parses IDENTICALLY to its untrimmed original, column for column,
and replaces every real author handle with a pseudonym. Do not hand-edit
them.

WHAT THIS SUITE IS FOR, beyond the obvious
------------------------------------------
Most of these checks exist because of a specific failure, in this repo or in
a sibling. The ones worth knowing about before you change anything:

  * `test_values_on_real_fixtures` asserts VALUES, not coverage. A column can
    be 100% populated and entirely wrong. The one that matters here is
    `claps`: Medium's legacy payload carries `virtuals.totalClapCount` AND
    `virtuals.recommends` side by side, both populated on 128 of 128 stories,
    and the second is the retired pre-2017 recommend count — 25 against 248
    on the same story. Reading it would have filled the column completely and
    wrongly, and no coverage check would have said a word.

  * `test_markers_do_not_match_a_good_page` is the §18 rule as a test. Medium
    ships reCAPTCHA Enterprise on every page it
    serves, and `challenge-platform` appears twice on good and refused pages
    alike. A marker that matches every page is worse than no marker, so every
    marker in every set is asserted ABSENT from four pages known to be good.

  * `test_engine_parity` binds every shared-module call in every engine
    against the callee's REAL signature. Two engines in a sibling repo called
    `classify(html, url=...)` where the parameter is positional, both crashed
    on their first fetch, and nothing short of a live run saw it. This repo's
    own first live run hit the same class twice — `scroll_until_settled` was
    called with a `target=` this module no longer takes, and Selenium passed
    `session.driver` to a helper that wanted `session`.

  * `test_publication_pages_are_refused` pins the decision that keeps this
    repo honest: a publication home page carries post IDS and no post data,
    and rows built from it would hold a sku and 26 nulls while the run
    reported success.
"""

import ast
import contextlib
import csv as csv_module
import inspect
import io
import json
import os
import pathlib
import re
import sys
import tempfile
from dataclasses import asdict, fields

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import captcha_solver
import env_config
from fingerprint_client import (fingerprint_user_agent,
                                playwright_context_kwargs)
import page_flow
import product_parser
from diff_runs import diff_products
from output_writer import (Post, Product, save, finish_run, write_csv,
                           run_meta, dedupe_by_key, dedupe_by_sku,
                           ROW_CLASS_BY_MODE, UNIQUE_BY_SKU_MODES,
                           COMPLETE_STOP_REASONS, EXIT_BLOCKED,
                           EXIT_NO_PRODUCTS, EXIT_PARTIAL, EXIT_API_ERROR,
                           LIST_CSV_SEPARATOR, SOURCE_DEFAULT)
from product_parser import (parse_posts, SELECTORS, MEDIUM_HOSTS, LEFT_MEDIUM,
                            PAGE_CAP, NEXT_PAGE_SELECTOR, PAGINATES_BY_URL,
                            PAGE_URL_REASON, RESERVED_FIRST_SEGMENTS,
                            NO_RESULTS_MARKERS, CHALLENGE_MARKERS,
                            BOT_CHALLENGE_MARKERS, BLOCK_MARKERS,
                            apollo_state, archive_years, category_from_url,
                            detect_block_marker, detect_bot_challenge,
                            detect_page_state, is_medium_host, is_no_results,
                            is_supported_host, jsonld_blocks, listing_kind,
                            normalize_url, obvinit_payload, page_url,
                            paginates_by_url, post_id_from_url, post_url_for,
                            posts_from_apollo, posts_from_dom,
                            posts_from_jsonld, posts_from_obvinit,
                            redirected_away, resolve_apollo_paragraphs,
                            served_by_medium, site_host, source_of,
                            strip_tracking, unsupported_reason,
                            _epoch_ms_to_iso)
from proxy_pool import ProxyPool, mask, to_playwright, split_credentials

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
ENGINES = ("playwright_scraper", "selenium_scraper", "puppeteer_scraper")
SHARED_MODULES = {"page_flow": page_flow, "product_parser": product_parser}

# Four fixtures cut from pages Medium actually served. Every marker check
# below asserts its markers are ABSENT from all four (§18).
GOOD_PAGES = ("tag_feed", "archive_day", "author", "post")

_failures = []
_total_checks = 0



def check(label, condition):
    """Print and record one check. Returns the condition so callers can
    accumulate with `ok &= check(...)`."""
    global _total_checks
    _total_checks += 1
    if condition:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s" % label)
        _failures.append(label)
    return bool(condition)


def group(title):
    print("\n== %s" % title)


def _raises(fn):
    """True if `fn()` raises. Used where refusing is the correct behaviour."""
    try:
        fn()
    except Exception:
        return True
    return False


_FIXTURE_PATH = os.path.join(REPO_ROOT, "fixtures_generated.json")
if not os.path.exists(_FIXTURE_PATH):
    # Said in words rather than as a bare FileNotFoundError, because the
    # first time this happened it was not missing from the disk — it was
    # missing from the COMMIT. `.gitignore` carries a blanket `*.json` (a
    # scraper's own output is large and stale by the time anyone reads it),
    # which swallowed it silently: the whole suite was green locally and
    # every CI job died at import. `test_required_files_are_committed` now
    # catches that case directly.
    raise SystemExit(
        f"fixtures_generated.json is missing from {REPO_ROOT}.\n"
        f"If you are in a clean checkout, it should have been committed — "
        f"check that .gitignore's `*.json` rule still carries the "
        f"`!fixtures_generated.json` exception.\n"
        f"If you are regenerating fixtures, run: python3 make_fixtures.py")
with open(_FIXTURE_PATH, encoding="utf-8") as _f:
    FIXTURES = json.load(_f)
URLS = FIXTURES["_URLS"]


def fixture(name):
    return FIXTURES[name]


def rows_of(name, page=1, mode=""):
    return parse_posts(FIXTURES[name], URLS[name], page=page, mode=mode)


def by_sku(name, page=1, mode=""):
    return {r.sku: r for r in rows_of(name, page, mode)}


# ---------------------------------------------------------------------------
def _engine_source(module_name):
    """The engine's source, or None when its driver is not installed.

    Read off disk rather than through `inspect.getsource`, so a check about
    an engine's TEXT does not itself need the engine's library.
    """
    path = os.path.join(REPO_ROOT, module_name + ".py")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_medium_encodings():
    group("Medium's own encodings")

    # Epoch MILLISECONDS, and the two payloads spell the same number
    # differently: the modern state ships an int, the legacy archive ships a
    # string. A converter that only took the int left every archive row's
    # published_at null while the feed rows looked fine — which is invisible
    # until both are in one output.
    check("epoch ms as an int",
          _epoch_ms_to_iso(1789022678474).startswith("2026-09-"))
    check("epoch ms as a STRING parses the same",
          _epoch_ms_to_iso("1789022678474") == _epoch_ms_to_iso(1789022678474))
    check("0 is not a date", _epoch_ms_to_iso(0) is None)
    check("None is not a date", _epoch_ms_to_iso(None) is None)
    check("a non-number is not a date", _epoch_ms_to_iso("soon") is None)
    # True is an int in Python, and a bool reaching a count column would put
    # 1 where the site published nothing.
    check("a bool is not a date", _epoch_ms_to_iso(True) is None)

    # The post id: 12 lowercase hex at the end of the last path segment, or
    # /p/{id}. It is `sku`, because it survives a story being renamed,
    # re-slugged or moved into a publication and none of the URLs do.
    check("id from a full story URL",
          post_id_from_url("https://medium.com/@who/some-slug-d373fe2c96b7")
          == "d373fe2c96b7")
    check("id from /p/",
          post_id_from_url("https://medium.com/p/d373fe2c96b7") == "d373fe2c96b7")
    check("id from an author subdomain",
          post_id_from_url("https://sub.medium.com/a-b-c-bfd47b63fdae")
          == "bfd47b63fdae")
    check("id from a publication custom domain",
          post_id_from_url("https://python.plainenglish.io/x-3e7d4b4b6c72")
          == "3e7d4b4b6c72")
    check("id survives tracking parameters",
          post_id_from_url("https://medium.com/@w/s-d373fe2c96b7?source=tag---a")
          == "d373fe2c96b7")
    check("a listing URL has no id",
          post_id_from_url("https://medium.com/tag/python") is None)
    # 11 or 13 hex is not an id. Without the length being exact, a hash in a
    # URL becomes a story.
    check("11 hex is not an id",
          post_id_from_url("https://medium.com/@w/s-d373fe2c96b") is None)
    check("uppercase hex is not an id",
          post_id_from_url("https://medium.com/@w/s-D373FE2C96B7") is None)
    check("/p/{id} rebuilds to a working address",
          post_url_for("d373fe2c96b7") == "https://medium.com/p/d373fe2c96b7")
    return not _failures


def test_values_on_real_fixtures():
    group("values on real captures")

    # ---- the legacy archive payload: the rich one --------------------------
    arch = by_sku("archive_day")
    story = arch["c2a4db010fd3"]
    # THE assertion of this suite. `virtuals.totalClapCount` is 248 on this
    # story and `virtuals.recommends`, populated right beside it on 128 of
    # 128 stories, is 25 — the retired pre-2017 recommend count. Reading the
    # wrong one fills the column completely and wrongly, and coverage says
    # 100% either way.
    check("claps come from totalClapCount, not from `recommends`",
          story.claps == 248)
    check("responses from responsesCreatedCount", story.responses == 1)
    # A float, as Medium computes it. Not "16 min read" rounded.
    check("reading time is minutes as a float", story.reading_time_min == 15.54)
    check("word count", story.word_count == 3801)
    check("language is Medium's own detection", story.language == "en")
    check("paywall flag", story.is_paywalled is True)
    check("publication name", story.publication == "Data Science Collective")
    check("published_at", story.published_at.startswith("2026-09-10T"))
    check("tags are slugs, in the site's order",
          story.tags[:3] == ["python", "data-science", "data-visualization"])
    check("every archive row came from the legacy payload",
          {r.data_source for r in rows_of("archive_day")} == {"obvinit"})

    # `mediumUrl` is the EMPTY STRING on 128 of 128 stories in this payload,
    # so the address is rebuilt — from the publication's custom domain where
    # there is one, its medium.com slug where there is not, and the author
    # otherwise. All three shapes are in this fixture.
    urls = {r.sku: r.url for r in rows_of("archive_day")}
    check("a story in a medium.com-hosted publication",
          urls["c2a4db010fd3"].startswith(
              "https://medium.com/data-science-collective/"))
    check("a story in a custom-domain publication",
          urls["3e7d4b4b6c72"].startswith("https://python.plainenglish.io/"))
    check("a story on an author's own profile",
          "/@" in urls["fe99be1cb5bb"])
    check("every rebuilt URL still carries its story id",
          all(post_id_from_url(u) == sku for sku, u in urls.items()))

    # ---- the modern state: fewer fields, and that is the SITE ---------------
    feed = by_sku("tag_feed")
    story = feed["d373fe2c96b7"]
    check("claps from the modern state", story.claps == 2)
    check("responses from the modern state", story.responses == 0)
    check("title", story.title == "The fraud test cannot make up its mind")
    # The measured difference between the two payloads, pinned. A tag feed's
    # payload carries 21 fields per story and no reading time at all, so a
    # null here is Medium and not a parsing failure — which is exactly why
    # `data_source` is a column.
    check("a tag-feed row has NO reading time, by the site's design",
          story.reading_time_min is None)
    check("...and an archive row of the same tag does",
          arch["c2a4db010fd3"].reading_time_min is not None)
    check("a tag-feed row has no word count", story.word_count is None)
    check("a paywalled story on the tag feed",
          feed["bfd47b63fdae"].is_paywalled is True)
    check("claps of a second tag-feed story", feed["bfd47b63fdae"].claps == 164)

    # ---- the author page ---------------------------------------------------
    author = rows_of("author")
    check("an author page yields that author's stories", len(author) == 4)
    check("every row on an author page has one author",
          len({r.author_username for r in author}) == 1)
    check("an author-page row DOES carry a reading time",
          author[0].reading_time_min == 17.84)
    check("claps on an author page", author[0].claps == 4919)
    check("an old story keeps its real date",
          author[0].published_at.startswith("2017-09-04T"))

    # ---- the post page -----------------------------------------------------
    post = rows_of("post", mode="post")
    check("a post URL yields exactly one row", len(post) == 1)
    check("...and it is the story the URL names", post[0].sku == "d373fe2c96b7")
    check("a post row carries its body", bool(post[0].content))
    check("content_chars matches the body",
          post[0].content_chars == len(post[0].content))
    check("a post row carries a word count", post[0].word_count == 3129)
    # The canonical is a SEPARATE column from `url`, because Medium's
    # canonical for an imported story points at another site entirely.
    check("canonical_url is kept where it differs from the Medium address",
          post[0].canonical_url and post[0].canonical_url != post[0].url)

    # ---- a non-Latin tag ---------------------------------------------------
    jp = rows_of("tag_nonlatin")
    check("a Japanese tag parses", len(jp) == 4)
    check("a non-Latin title survives intact",
          any("Yamato Ensemble" in (r.title or "") for r in jp))
    check("non-Latin tag slugs survive",
          any("日本語" in (r.tags or []) for r in jp))
    check("a percent-encoded story URL still yields its id",
          all(r.sku == post_id_from_url(r.url) for r in jp))

    # Every row of every fixture, on the two columns nothing may be missing.
    for name in GOOD_PAGES + ("tag_nonlatin",):
        rows = rows_of(name)
        check("%s: every row has a title" % name, all(r.title for r in rows))
        check("%s: every row has a url and a sku" % name,
              all(r.url and r.sku for r in rows))
        check("%s: page and position are unique as a pair" % name,
              len({(r.page, r.position) for r in rows}) == len(rows))
    return not _failures


def test_the_four_read_paths():
    group("the four read paths, and what each one can say")

    html = fixture("tag_feed")
    apollo = posts_from_apollo(html, URLS["tag_feed"])
    jsonld = posts_from_jsonld(html, URLS["tag_feed"])
    dom = posts_from_dom(html, URLS["tag_feed"])
    check("the modern state describes stories", len(apollo) >= 4)
    check("JSON-LD describes stories", len(jsonld) >= 1)
    check("the rendered cards describe stories", len(dom) >= 1)
    check("the legacy payload is absent from a tag feed",
          posts_from_obvinit(html, URLS["tag_feed"]) == {})

    arch = fixture("archive_day")
    check("the legacy payload describes an archive day",
          len(posts_from_obvinit(arch, URLS["archive_day"])) == 6)

    # `window["obvInit"](...)` is a JAVASCRIPT object literal, not JSON.
    # Medium escapes `>` as `\x3e` so a `</script>` inside a string cannot
    # close the tag it sits in, and `\xHH` is not legal JSON — one such
    # character in a Thai image alt-text made `json.loads` reject a 970 KB
    # payload, and the whole page silently fell through to the DOM path: 120
    # rows with titles and authors and every other column null, reported as a
    # success. Found by walking four consecutive days rather than one.
    escaped = fixture("archive_escape")
    check("a payload carrying a JS \\xHH escape still parses",
          len(posts_from_obvinit(escaped, URLS["archive_escape"])) == 4)
    rows = rows_of("archive_escape")
    check("...and its rows come from the payload, not the DOM fallback",
          {r.data_source for r in rows} == {"obvinit"})
    check("...with the columns only that payload carries",
          all(r.claps is not None and r.reading_time_min is not None
              for r in rows))
    # The rewrite must not touch a backslash that was itself escaped.
    check("an escaped backslash before an x is left alone",
          product_parser._js_to_json(r'"a\\\\x3e"') == r'"a\\\\x3e"')
    check("a lone JS hex escape is rewritten to its JSON spelling",
          product_parser._js_to_json(r'"a\x3eb"') == r'"a\u003eb"')

    # The fixtures are a COMMITTED artefact, so regenerating them from the
    # same captures must produce the same bytes. It did not: the scrub
    # discovered handles through a set, whose iteration order depends on
    # string hashing, and Python randomises that per process — so every
    # regeneration reassigned the pseudonyms and churned the file. A
    # committed artefact nobody can review a diff of is not reviewable.
    import make_fixtures
    sample = "//alpha.medium.com/x /@beta /@alpha /@gamma-long-handle"
    first = make_fixtures._discovered_handles(sample)
    check("handle discovery returns a stable order",
          first == make_fixtures._discovered_handles(sample))
    check("...longest first, so a prefix is not half-replaced",
          [len(h) for h in first] == sorted((len(h) for h in first),
                                            reverse=True))
    check("the modern state is absent from an archive day",
          posts_from_apollo(arch, URLS["archive_day"]) == {})

    # JSON-LD's `url` is the CANONICAL address, which for a cross-posted
    # story points at another site. The Medium address is rebuilt from the
    # id, and this is the trap the whole parser is careful about.
    for node in jsonld.values():
        check("a JSON-LD row's url is a medium.com address",
              node["url"].startswith("https://medium.com/p/"))
        break

    # The DOM path must not invent rows out of page chrome. A publication's
    # "About" tab is itself a Medium post and its nav link carries a 12-hex
    # id; reading it produced a row titled "About" with every other column
    # null.
    chrome = ('<html><body><nav><a href="/about-c2da52f08a07">About</a></nav>'
              '<footer><a href="/help-aa11bb22cc33">Help</a></footer>'
              '</body></html>')
    check("a link outside a card is not a story",
          posts_from_dom(chrome, "https://medium.com/") == {})
    carded = ('<html><body><article><h2>A Real Story</h2>'
              '<a href="/@w/a-real-story-d373fe2c96b7">A Real Story</a>'
              '</article></body></html>')
    check("a link inside a card IS a story",
          list(posts_from_dom(carded, "https://medium.com/")) == ["d373fe2c96b7"])

    # A publication page's state is 167 references with no data behind them.
    # Rows built from those would be a sku and 26 nulls, reported as success.
    stubs = ('<html><body><script>window.__APOLLO_STATE__ = '
             '{"Post:aaaaaaaaaaaa":{"__typename":"Post","id":"aaaaaaaaaaaa"},'
             '"Post:bbbbbbbbbbbb":{"__typename":"Post","id":"bbbbbbbbbbbb"}};'
             '</script></body></html>')
    check("bare id references produce no rows",
          posts_from_apollo(stubs, "https://medium.com/some-publication") == {})

    # A payload that arrived half-written must not take the run down.
    broken = '<html><script>window.__APOLLO_STATE__ = {"Post:aaa":</script></html>'
    check("an unparseable state returns {} rather than raising",
          apollo_state(broken) == {})
    check("an absent state returns {}", apollo_state("<html></html>") == {})
    check("an absent legacy payload returns {}",
          obvinit_payload("<html></html>") == {})
    check("no html at all is not a crash", parse_posts("", "https://medium.com/tag/x") == [])

    # The body lives in normalized Paragraph nodes, not on the post.
    check("the body is reassembled from the state's paragraphs",
          bool(resolve_apollo_paragraphs(fixture("post"))))
    check("a listing page has no body to reassemble",
          resolve_apollo_paragraphs(fixture("tag_feed")) is None)
    return not _failures


def test_a_post_page_holds_more_than_its_own_story():
    group("a post page's recommendations are not the answer")

    # A post page describes the story its URL names AND whatever Medium
    # recommends beside it. Before this was pinned, a single-story URL came
    # back with five rows — four of them the site's suggestions.
    html = fixture("post")
    url = URLS["post"]
    check("post mode returns exactly the story asked for",
          [r.sku for r in parse_posts(html, url, mode="post")] == ["d373fe2c96b7"])
    # Read as a listing, the same page may legitimately describe more. The
    # mode is what narrows it, and the mode follows the URL.
    listing = parse_posts(html, "https://medium.com/tag/python", mode="tag")
    check("read as a listing, the same page is not narrowed",
          len(listing) >= 1)
    # Asked for a story the page does not describe, it must return NOTHING
    # rather than the recommendations — answering a different question than
    # the one asked is worse than answering none.
    other = parse_posts(html, "https://medium.com/p/ffffffffffff", mode="post")
    check("a post page that lacks the story asked for yields no rows",
          other == [])
    return not _failures


def test_publication_pages_are_refused():
    group("a publication page is refused, with the measurement")

    for url in ("https://medium.com/better-programming",
                "https://medium.com/data-science-collective"):
        why = unsupported_reason(url)
        check("%s is refused" % url, why is not None)
        check("...and the refusal carries the measurement",
              why and "167" in why and "no title" in why)
        check("...and it names what to do instead",
              why and "--mode archive" in why and "--mode post" in why)
    # A story ON a publication is a story, not a publication page.
    check("a publication's STORY url is supported",
          is_supported_host("https://medium.com/data-science-collective/"
                            "a-slug-c2a4db010fd3"))
    check("...and reads as a post",
          listing_kind("https://medium.com/data-science-collective/"
                       "a-slug-c2a4db010fd3") == "post")

    # Search is refused too, and for its own measured reason.
    why = unsupported_reason("https://medium.com/search?q=python")
    check("a search URL is refused", why is not None)
    check("...naming the empty state", why and "EMPTY state" in why)

    # A host that left Medium is refused with THAT reason rather than with
    # "is not a Medium site", which is false and sends the reader looking for
    # a typo (§5).
    why = unsupported_reason("https://towardsdatascience.com/")
    check("a former publication is refused", why is not None)
    check("...with the reason it actually left",
          why and "WordPress" in why)
    check("...and the refusal does not claim it was never Medium",
          why and "not a Medium" not in why)
    return not _failures


def test_urls():
    group("URL shapes")

    check("medium.com is a Medium host", is_medium_host("medium.com"))
    check("www is too", is_medium_host("www.medium.com"))
    check("an author subdomain is too", is_medium_host("sub.medium.com"))
    # Not a suffix match on the string: `notmedium.com` must not pass.
    check("a lookalike host is not Medium", not is_medium_host("notmedium.com"))
    check("a custom domain is not asserted to be Medium",
          not is_medium_host("python.plainenglish.io"))
    check("...but it is still accepted, and checked at parse time",
          is_supported_host("https://python.plainenglish.io/x-3e7d4b4b6c72"))

    check("a tag", listing_kind("https://medium.com/tag/python") == "tag")
    check("a tag tab", listing_kind("https://medium.com/tag/python/recommended") == "tag")
    check("a month archive is still a tag view",
          listing_kind("https://medium.com/tag/python/archive/2026/09") == "tag")
    check("a DAY archive is its own mode",
          listing_kind("https://medium.com/tag/python/archive/2026/09/10")
          == "archive")
    check("an author", listing_kind("https://medium.com/@quincylarson") == "author")
    check("a story under an author",
          listing_kind("https://medium.com/@w/slug-d373fe2c96b7") == "post")
    check("a short story URL",
          listing_kind("https://medium.com/p/d373fe2c96b7") == "post")
    check("a non-Latin tag",
          listing_kind("https://medium.com/tag/%E6%97%A5%E6%9C%AC%E8%AA%9E") == "tag")
    check("a publication", listing_kind("https://medium.com/better-programming")
          == "publication")
    check("one of Medium's own routes is not a publication",
          listing_kind("https://medium.com/membership") == "unknown")
    check("every reserved segment is lowercase and bare",
          all(seg == seg.lower() and "/" not in seg
              for seg in RESERVED_FIRST_SEGMENTS))

    # Tracking parameters are stripped so one story has one address. Medium
    # hangs seventy characters of referrer provenance off every internal
    # link, and it changes with the referrer.
    dirty = ("https://medium.com/@w/slug-d373fe2c96b7"
             "?source=tag_archive------python---3------------------&gi=abc")
    check("tracking is stripped",
          normalize_url(dirty) == "https://medium.com/@w/slug-d373fe2c96b7")
    check("two spellings of one story normalize together",
          normalize_url(dirty) == normalize_url(
              "https://medium.com/@w/slug-d373fe2c96b7/#responses"))
    # The HOST is deliberately left alone: which address answered is what
    # `source` records, and rewriting it here would erase that.
    check("the host is not rewritten",
          site_host(normalize_url("https://sub.medium.com/a-d373fe2c96b7"))
          == "sub.medium.com")
    check("a non-tracking parameter survives",
          "q=1" in normalize_url("https://medium.com/tag/python?q=1"))
    check("source_of reads the host",
          source_of("https://python.plainenglish.io/x-3e7d4b4b6c72")
          == "python.plainenglish.io")

    check("a tag's label", category_from_url("https://medium.com/tag/python")
          == "python")
    check("an archive day's label",
          category_from_url("https://medium.com/tag/python/archive/2026/09/10")
          == "python 2026-09-10")
    check("an author's label",
          category_from_url("https://medium.com/@quincylarson") == "@quincylarson")

    for bad, why in (("", "no URL"), ("ftp://medium.com/tag/x", "http"),
                     ("https://medium.com/nonsense/deep/path", "not a Medium")):
        check("%r is refused" % bad, unsupported_reason(bad) is not None)
    return not _failures


def test_pagination():
    group("pagination")

    archive = "https://medium.com/tag/python/archive/2026/09/10"
    check("only the day archive paginates by URL", paginates_by_url(archive))
    for other in ("https://medium.com/tag/python",
                  "https://medium.com/@quincylarson",
                  "https://medium.com/p/d373fe2c96b7"):
        check("%s does not" % other, not paginates_by_url(other))
        # None, not a built URL. `?page=2` on a tag feed is IGNORED rather
        # than failing, so a run built on it would add no new sku, call the
        # listing exhausted and report COMPLETE holding one page (§18).
        check("...and page_url returns None for it", page_url(other, 2) is None)

    check("page 1 is the URL given", page_url(archive, 1) == archive)
    check("page 2 is the day before",
          page_url(archive, 2)
          == "https://medium.com/tag/python/archive/2026/09/09")
    check("page 11 crosses the month boundary",
          page_url(archive, 11)
          == "https://medium.com/tag/python/archive/2026/08/31")
    check("page 300 crosses the year boundary",
          page_url(archive, 300).startswith(
              "https://medium.com/tag/python/archive/2025/"))
    check("the tag survives the walk",
          "/tag/python/" in page_url(archive, 5))
    check("a non-Latin tag survives the walk",
          page_url("https://medium.com/tag/%E6%97%A5%E6%9C%AC%E8%AA%9E"
                   "/archive/2026/09/10", 2)
          == "https://medium.com/tag/%E6%97%A5%E6%9C%AC%E8%AA%9E"
             "/archive/2026/09/09")
    check("an impossible date is refused rather than guessed",
          page_url("https://medium.com/tag/python/archive/2026/02/31", 2) is None)

    # The site publishes its own archive index, which is what lets a walk be
    # planned rather than guessed (§7).
    years = archive_years(fixture("archive_day"))
    check("the archive index lists years with stories", len(years) > 10)
    check("...as four-digit strings",
          all(len(y) == 4 and y.isdigit() for y in years))
    check("a tag feed publishes no archive index",
          archive_years(fixture("tag_feed")) == [])

    # VERIFY before planning. A day with no stories does not 404 — it
    # redirects up to the month, which is a different renderer holding a
    # different set of stories, so HTTP 200 and a full page is not proof the
    # requested day was served.
    check("a redirect up to the month is detected",
          redirected_away(archive,
                          "https://medium.com/tag/python/archive/2026/09")
          is not None)
    check("...and names both addresses",
          "2026/09/10" in redirected_away(
              archive, "https://medium.com/tag/python/archive/2026/09"))
    check("the same address is not a redirect",
          redirected_away(archive, archive) is None)
    check("tracking on the landed URL is not a redirect",
          redirected_away(archive, archive + "?source=x") is None)
    # Measured 2026-09-16: `www.medium.com/tag/python` is served on 4 of 4
    # attempts and lands on `medium.com/tag/python`. That is a HOST redirect
    # and must not read as a pagination one — the comparison is on the PATH
    # for this reason, and a host-only difference is not a different listing.
    check("a www -> apex redirect is not a pagination redirect",
          redirected_away("https://www.medium.com/tag/python/archive/2026/09/10",
                          "https://medium.com/tag/python/archive/2026/09/10")
          is None)
    check("...and www is a supported host",
          is_supported_host("https://www.medium.com/tag/python")
          and is_medium_host("www.medium.com"))
    # A publication's custom domain is served too (measured: challenged on
    # three attempts, served on the fourth) and its story URLs carry the id.
    check("a custom-domain publication's story is a post",
          listing_kind("https://python.plainenglish.io/x-3e7d4b4b6c72") == "post")

    check("PAGINATES_BY_URL is true for this repo", PAGINATES_BY_URL is True)
    check("the reason names what does and does not paginate",
          "day archive" in PAGE_URL_REASON.lower())
    # Deliberately empty: Medium publishes no next control in any mode, and a
    # selector that matched nothing would be a third source of truth.
    check("no next-page selector is claimed", NEXT_PAGE_SELECTOR == "")
    check("the page cap is a real number", 1 < PAGE_CAP <= 100)
    return not _failures


def test_page_state():
    group("which of five states a response is")

    for name in GOOD_PAGES + ("tag_nonlatin",):
        check("%s classifies as content" % name,
              detect_page_state(fixture(name), 200, URLS[name]) == "content")
    check("Cloudflare's managed challenge",
          detect_page_state(fixture("challenge"), 403, URLS["challenge"])
          == "challenge")
    check("Cloudflare's WAF refusal",
          detect_page_state(fixture("blocked_waf"), 403, URLS["blocked_waf"])
          == "blocked")
    # A former publication now running WordPress: `<article>` elements and
    # schema.org `Article` blocks, and NONE of Medium's asset hosts. Without
    # the asset gate this classified as `content` and would have put
    # WordPress rows into a Medium run.
    check("a page that is not Medium's",
          detect_page_state(fixture("not_medium"), 200, URLS["not_medium"])
          == "blocked")
    check("no response at all is blocked, not empty",
          detect_page_state(None, None, "https://medium.com/tag/python")
          == "blocked")

    # The status is positional and SECOND, matching `page_flow.classify`.
    # Two engines in a sibling repo passed it as a keyword and both crashed
    # on their first fetch (§17).
    check("detect_page_state takes status positionally, second",
          list(inspect.signature(detect_page_state).parameters)[:2]
          == ["html", "status"])
    check("page_flow.classify agrees",
          list(inspect.signature(page_flow.classify).parameters)[:2]
          == ["html", "status"])

    # A payload proves content even where nothing has painted, and it is
    # asked BEFORE any threshold — ordered by what each signal proves rather
    # than by what is cheap (§17).
    unpainted = re.sub(r"<article\b.*?</article>", "", fixture("tag_feed"),
                       flags=re.S)
    check("a state with no painted cards is still content",
          detect_page_state(unpainted, 200, URLS["tag_feed"]) == "content")
    # ...and a served page with neither is a SHELL, which waits rather than
    # refetching. A refetch of a shell buys another shell.
    shell = ('<html><head><link href="https://cdn-client.medium.com/a.css">'
             '<link href="https://miro.medium.com/x.png">'
             '<link href="https://glyph.medium.com/f.woff"></head>'
             '<body></body></html>')
    check("served, ours, nothing in it yet is a shell",
          detect_page_state(shell, 200, "https://medium.com/tag/python")
          == "shell")
    check("the same page without our assets is blocked",
          detect_page_state("<html><body></body></html>", 200,
                            "https://medium.com/tag/python") == "blocked")
    # Medium's own "no stories" copy is a POSITIVE signal and beats the
    # status check and every threshold: a correct empty answer must not be
    # reported as a block (§17).
    empty = shell.replace("<body>", "<body>No stories")
    check("the site's own empty answer is `empty`, not `blocked`",
          detect_page_state(empty, 200, "https://medium.com/tag/x") == "empty")
    check("...even under a 403",
          detect_page_state(empty, 403, "https://medium.com/tag/x") == "empty")
    check("is_no_results reads the site's copy", is_no_results(empty))
    check("...and not a page that merely mentions stories",
          not is_no_results("<html>Top stories about Python</html>"))
    return not _failures


def test_markers_do_not_match_a_good_page():
    group("§18: a marker that matches every page is worse than no marker")

    # EVERY marker in EVERY set, asserted absent from four pages Medium
    # actually served. This is the check that would have caught `g-recaptcha`
    # — which Medium ships on every page as Enterprise v3 invisible, 3 to 4
    # occurrences per page — before it made a good 364 KB tag feed report
    # "challenge: recaptcha".
    for name in GOOD_PAGES:
        html = fixture(name).lower()
        for label, markers in (("challenge", CHALLENGE_MARKERS),
                               ("vendor", BOT_CHALLENGE_MARKERS),
                               ("block", BLOCK_MARKERS)):
            for marker in markers:
                check("%s marker %r is absent from %s"
                      % (label, marker, name), marker.lower() not in html)
        check("%s is not read as a challenge" % name,
              detect_bot_challenge(fixture(name)) is None)
        check("%s is not read as blocked" % name,
              detect_block_marker(fixture(name)) is None)

    # And the markers that ARE kept must match the pages they are for.
    check("the challenge page is named as Cloudflare's",
          detect_bot_challenge(fixture("challenge")) == "cloudflare")
    check("the WAF page is named separately from the challenge",
          detect_block_marker(fixture("blocked_waf")) == "cloudflare-waf")
    check("a page that is not Medium's says so",
          detect_block_marker(fixture("not_medium")) == "not-served-by-medium")

    # `challenge-platform` is the one that did not make it in: it appears
    # exactly twice on every page Medium serves, refused and served alike.
    # Pinned so nobody adds it back.
    for name in GOOD_PAGES:
        check("challenge-platform is on the good page %s too" % name,
              "challenge-platform" in fixture(name))
    check("...and it is therefore in no marker set",
          not any("challenge-platform" in m
                  for m in CHALLENGE_MARKERS + BOT_CHALLENGE_MARKERS
                  + BLOCK_MARKERS))
    # Same for reCAPTCHA, which Medium ships on every page it serves.
    check("no reCAPTCHA marker is in any set",
          not any("recaptcha" in m.lower()
                  for m in CHALLENGE_MARKERS + BOT_CHALLENGE_MARKERS
                  + BLOCK_MARKERS))

    # The positive asset signal. All three hosts are needed, not one: the
    # legacy archive renderer ships ZERO `cdn-client.medium.com` references,
    # so anchoring on that alone would report every 128-story archive page as
    # blocked.
    for name in GOOD_PAGES:
        check("%s is built out of Medium's assets" % name,
              served_by_medium(fixture(name)))
    check("the archive fixture has no cdn-client references at all",
          "cdn-client.medium.com" not in
          fixture("archive_day").split("</head>")[1])
    for name in ("challenge", "blocked_waf", "not_medium"):
        check("%s is not" % name, not served_by_medium(fixture(name)))

    # The Scraping Browser's auto-solve extension injects its own turnstile
    # hunter into every page it loads. This repo's marker set CAN match one,
    # which is the condition §8 sets for adding the filter.
    injected = (fixture("tag_feed") +
                '<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenoo'
                'emhbpbo/content/captcha/turnstile/hunter.js" '
                'data-ts-input="cf-turnstile-response"></script>')
    check("an extension-injected turnstile tag is not the site's",
          detect_bot_challenge(injected) is None)
    check("...and the page still classifies as content",
          detect_page_state(injected, 200, URLS["tag_feed"]) == "content")
    return not _failures


def test_a_challenge_is_never_paid_for_here():
    group("no solve is ever bought for a challenge this repo does not intercept")

    html = fixture("challenge")
    # Cloudflare's MANAGED challenge: `cType: 'managed'`, no sitekey and no
    # iframe IN THE MARKUP. That is a fact about the markup, not about the
    # challenge — Cloudflare passes sitekey/action/cData/chlPageData to
    # `turnstile.render()` once and keeps nothing, and a repo that installs
    # an init script to capture them can buy a TurnstileTaskProxyless solve
    # (foodpanda-scraper does). This repo deliberately does not, because the
    # challenge here is transient: 9 of 27 first attempts met it and a fresh
    # context cleared all 9. So the policy must not spend a solve on it.
    check("the fixture really is the managed kind", "managed" in html)
    check("it publishes no sitekey", "data-sitekey" not in html)
    check("it renders no iframe", "<iframe" not in html.lower())
    check("the policy does not solve a challenge",
          page_flow.should_solve("challenge") is False)
    check("...nor a block", page_flow.should_solve("blocked") is False)
    check("...nor anything else",
          not any(page_flow.should_solve(state) for state in page_flow.STATE_POLICY))
    # It IS retried, and counted as blocked if the retries run out: letting
    # it through hands a 28 KB interstitial to the parser and reports exit 4
    # on a tag holding hundreds of thousands of stories.
    check("a challenge is retried", page_flow.should_retry("challenge"))
    check("a challenge that survives is blocked, not empty",
          page_flow.counts_as_blocked("challenge"))
    check("a challenge is not parsed", not page_flow.should_parse("challenge"))
    # And the retry has to be a NEW context: waiting inside the same one
    # cleared none of the nine measured, a fresh context cleared all nine.
    check("the retry policy says a fresh context is required",
          page_flow.RETRY_NEEDS_FRESH_CONTEXT is True)
    for engine in ENGINES:
        src = _engine_source(engine)
        if src is None:
            continue
        check("%s consults RETRY_NEEDS_FRESH_CONTEXT" % engine,
              "RETRY_NEEDS_FRESH_CONTEXT" in src)
    return not _failures


def test_captcha_capability_claims_match_the_code():
    """§19: a sentence is the most expensive bug this family can ship.

    Two directions, and both have been shipped wrong in this family before:

      * claiming a captcha CANNOT be solved, when what is true is that this
        repo does not implement the task type. 2Captcha solves enterprise
        reCAPTCHA and Cloudflare Turnstile and has for years, so a sentence
        like "neither is solvable" tells a reader not to buy something that
        would have worked. "Unsolvable" is a property of a PAGE that carries
        no widget, never of the vendor.
      * claiming this repo DOES solve something whose task type is not built
        anywhere in it, which is the same error facing the other way.

    Nothing else in the suite can catch either one: no test fails, no run
    crashes and the output is correct.
    """
    group("captcha capability claims (§19)")
    ok = True
    docs = {}
    for name in ("README.md", "CHANGELOG.md"):
        path = os.path.join(REPO_ROOT, name)
        if os.path.exists(path):
            docs[name] = open(path, encoding="utf-8", errors="replace").read()

    # A conclusion about the PRODUCT. Phrases about a widget-less page
    # ("no widget", "nothing for a solver at any price to answer") are
    # deliberately NOT here: that page really does carry nothing to answer.
    FORBIDDEN = (
        "cannot be solved",
        "can't be solved",
        "neither is solvable",
        "is not solvable",
        "solver is inapplicable",
        "no solver can",
    )
    for name, text in docs.items():
        low = text.lower()
        for phrase in FORBIDDEN:
            hit = phrase in low
            # A released CHANGELOG section is history and is left verbatim;
            # a correction leads the newer section instead (§19).
            if hit and name == "CHANGELOG.md":
                continue
            ok &= check(f"{name}: no {phrase!r} — write "
                        f"'this repo does not implement X' instead", not hit)

    # And the other direction: a claim that something IS solved here has to
    # be backed by a task type and, for Turnstile, by the interception hook
    # that is the only way to obtain a Challenge page's parameters.
    solver = open(os.path.join(REPO_ROOT, "captcha_solver.py"),
                  encoding="utf-8").read()
    engines = " ".join(_engine_source(e) or "" for e in ENGINES)
    readme_low = docs.get("README.md", "").lower()

    claims_turnstile = ("turnstile" in readme_low
                        and "does not implement" not in readme_low)
    if claims_turnstile:
        ok &= check("README claims Turnstile solving, so the task type exists",
                    "TurnstileTaskProxyless" in solver)
        ok &= check("...and an engine installs the turnstile.render hook",
                    "TURNSTILE_INTERCEPT_JS" in engines)
    else:
        ok &= check("README does not claim Turnstile solving — nothing to back",
                    True)

    # Whatever a task type is built for, the solver must actually be able to
    # name it. A builder with no consumer is the dead-code defect wearing a
    # capability's clothes.
    for task in ("TurnstileTaskProxyless", "RecaptchaV2EnterpriseTaskProxyless"):
        if task in solver:
            ok &= check(f"{task} is reachable from an engine",
                        "solve_recaptcha" in engines or "solve_turnstile" in engines)
    return ok


def test_page_flow_policy():
    group("STATE_POLICY — the triage as DATA, not three if-chains")
    ok = True
    for state in ("content", "empty", "shell", "challenge", "blocked"):
        ok &= check(f"{state} has a full policy row",
                    set(page_flow.STATE_POLICY[state]) ==
                    {"parse", "retry", "solve", "blocked"})
    ok &= check("content is parsed and not retried",
                page_flow.should_parse("content") and not page_flow.should_retry("content"))
    ok &= check("empty is a final answer, not a fault",
                not page_flow.should_parse("empty") and not page_flow.should_retry("empty"))
    ok &= check("shell is parsed after the wait, never refetched",
                page_flow.should_parse("shell") and not page_flow.should_retry("shell"))
    # THE DIFFERENCE FROM EVERY SIBLING REPO, and it was found by running
    # the thing (§15). A challenge is retried first; if the retries are spent
    # and it is still a challenge, the run is BLOCKED (exit 3), not empty
    # (exit 4). With this False the first live run parsed the 6 KB
    # interstitial as a feed and reported "ran fine, found nothing" on a
    # topic holding hundreds of answers.
    ok &= check("a challenge that survives its retries counts as blocked",
                page_flow.counts_as_blocked("challenge") is True)
    ok &= check("but it is retried before that verdict is reached",
                page_flow.should_retry("challenge") is True)
    ok &= check("and it is never parsed",
                page_flow.should_parse("challenge") is False)
    ok &= check("blocked counts towards exit 3",
                page_flow.counts_as_blocked("blocked")
                and not page_flow.counts_as_blocked("empty"))
    ok &= check("an unknown state falls back to the blocked row",
                page_flow.should_parse("nonsense") is False)

    group("Retrying a block DOES help here — and the engines CONSULT that")
    # A policy constant nothing reads is the same defect as dead code (§17).
    ok &= check("RETRY_ON_BLOCKED is True on this site",
                page_flow.RETRY_ON_BLOCKED is True)
    ok &= check("the budget is non-zero", page_flow.BLOCK_RETRIES_WITHOUT_POOL > 0)
    ok &= check("a pool buys more attempts",
                page_flow.BLOCK_RETRIES_WITH_POOL >= page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    consulted = []
    for engine in ENGINES:
        src = open(os.path.join(REPO_ROOT, f"{engine}.py"), encoding="utf-8").read()
        consulted.append("RETRY_ON_BLOCKED" in src)
    ok &= check("every engine reads RETRY_ON_BLOCKED", all(consulted))

    group("Readiness — a count poll, never an evaluated string")
    ok &= check("MIN_CARD_MATCHES is above 1 (§5)", page_flow.MIN_CARD_MATCHES > 1)
    ok &= check("the anchor is the card itself, in every mode",
                all(page_flow.ready_selector(m) == SELECTORS["item_card"]
                    for m in ("topic", "question", "profile")))
    # A question with a single answer can never reach the floor, so the
    # site's own answer count lowers it.
    ok &= check("min_matches is clamped by what the question says it holds",
                page_flow.min_matches("question", 1) == 1)
    ok &= check("and is not raised above the floor",
                page_flow.min_matches("question", 50) == page_flow.MIN_CARD_MATCHES)
    # Medium publishes no per-page counter. What it does publish on a day
    # archive is the size of the tag's WHOLE catalogue since 2003, which is
    # recorded beside the run and never used as an expectation: reading it as
    # a gap would report a quarter of a million missing stories on a page
    # that rendered everything it had.
    ok &= check("the tag's whole-catalogue count is read where the site gives it",
                page_flow.tag_total_posts(fixture("archive_day")) > 100000)
    ok &= check("a tag feed publishes no such count",
                page_flow.tag_total_posts(fixture("tag_feed")) is None)
    ok &= check("page_gap is None, always, and not 0",
                page_flow.page_gap(fixture("archive_day"), 6) is None)

    calls = []

    def count(_sel):
        calls.append(1)
        return 0 if len(calls) < 4 else 9

    found = page_flow.wait_for_count(count, lambda ms: None, "x", 5, 5_000)
    ok &= check("wait_for_count returns the count it reached", found == 9)
    ok &= check("a wait that never satisfies still returns, bounded",
                page_flow.wait_for_count(lambda s: 0, lambda ms: None, "x", 5, 400) == 0)
    return ok


def test_scroll_loop():
    group("The scroll — the WINDOW, and THREE stable rounds (§8)")
    ok = True
    # The opposite of a sibling repo, whose body never scrolled. Here the
    # window is what moves, and every engine must scroll to the document's
    # own end rather than by a fixed wheel distance — a fixed wheel stopped
    # three rounds short of the bottom on that sibling's grid, so the
    # lazy-load trigger was never reached and a run took 30 of 50 cards while
    # looking settled.
    for engine in ENGINES:
        src = _engine_source(engine)
        if src is None:
            continue
        ok &= check(f"{engine} scrolls to document.body.scrollHeight",
                    "document.body.scrollHeight" in src)
        ok &= check(f"{engine} has no inner scroll container to chase",
                    "scroll_container" not in src)

    # A pause is not an ending: the next batch takes longer to arrive than a
    # single pause, so the loop needs three quiet rounds rather than one.
    counts = [3, 7, 13, 13, 13, 13, 13, 13]
    state = {"i": 0}
    heights = {"h": 1000}

    def count(_sel):
        return counts[min(state["i"], len(counts) - 1)]

    def scroll():
        state["i"] += 1
        heights["h"] += 500

    trace = page_flow.scroll_until_settled(
        count, scroll, lambda: heights["h"], lambda ms: None)
    ok &= check("the loop settles on the count the feed stopped at",
                trace["cards_after"] == 13)
    ok &= check("and records where it started", trace["cards_before"] == 3)
    ok &= check("and how many rounds it spent", trace["rounds"] >= 3)
    ok &= check("three stable rounds, not one",
                page_flow.SCROLL_STABLE_ROUNDS >= 3)

    # The measured case on THIS site: the feed never grows, because the XHR
    # that would extend it is refused. The loop must settle and stop rather
    # than spending the whole budget chasing it.
    rounds = {"n": 0}

    def scroll2():
        rounds["n"] += 1

    trace2 = page_flow.scroll_until_settled(
        lambda s: 7, scroll2, lambda: 500, lambda ms: None)
    ok &= check("a feed that never grows settles and stops",
                trace2["cards_after"] == 7)
    ok &= check("and does not spend the whole budget",
                rounds["n"] <= page_flow.SCROLL_STABLE_ROUNDS + 1)

    # ...and the warning that reports it talks about CARDS, not rows. On an
    # author page the rendered card count went 10 -> 0 across four rounds
    # because Medium replaces that feed's DOM; a message that read the card
    # count as a story count would announce "0 stories" on a run that wrote
    # ten complete ones.
    dropped = {"rounds": 4, "cards_before": 10, "cards_after": 0}
    warning = page_flow.feed_not_extended_warning("author", 10, dropped)
    ok &= check("a card count that DROPS is reported as a DOM replacement",
                warning and "10 -> 0" in warning and "payload" in warning)
    ok &= check("...and the warning never claims a story count",
                warning and "0 stories" not in warning)
    ok &= check("archive mode does not get a scroll warning: it walks URLs",
                page_flow.feed_not_extended_warning("archive", 128, dropped)
                is None)
    ok &= check("nor does post mode, which is one story",
                page_flow.feed_not_extended_warning("post", 1, dropped) is None)
    ok &= check("a feed that DID grow gets no warning",
                page_flow.feed_not_extended_warning(
                    "tag", 30, {"rounds": 2, "cards_before": 15,
                                "cards_after": 30}) is None)

    # A feed that grows forever must not hang the run — and on an infinite
    # scroll that is not a hypothetical.
    forever = {"n": 0}

    def count3(_sel):
        forever["n"] += 1
        return forever["n"]

    page_flow.scroll_until_settled(count3, lambda: None,
                                   lambda: forever["n"] * 10,
                                   lambda ms: None)
    ok &= check("a forever-growing feed is bounded by the round budget",
                forever["n"] <= page_flow.SCROLL_MAX_ROUNDS * 2 + 2)
    return ok


def test_throttle_is_not_completion():
    group("A refused batch is NOT an exhausted listing (§7)")
    ok = True
    # The distinction this repo's policy turns on. A scroll that produced
    # nothing new means one of two things, and they map to opposite run
    # statuses: the feed ran out (COMPLETE) or the GraphQL call behind it was
    # refused (PARTIAL). Collapsing them is how a throttled run reports
    # "complete" while holding its first batch.
    ok &= check("`no_new_products` is a COMPLETE stop reason",
                "no_new_products" in COMPLETE_STOP_REASONS)
    ok &= check("`next_batch_refused` is NOT",
                "next_batch_refused" not in COMPLETE_STOP_REASONS)
    ok &= check("advance_feed has two distinct outcomes, not a bool",
                page_flow.ADVANCED != page_flow.NO_GROWTH)

    # Driven with the browser stubbed out (§10): a live run cannot always
    # reach this, because batch 1 decides whether there is a batch 2 at all.
    calls = {"scrolls": 0}
    counts = iter([4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4,
                   4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4,
                   4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4])

    def count(_selector):
        return next(counts, 4)

    def scroll():
        calls["scrolls"] += 1

    verdict = page_flow.advance_feed(scroll, count, lambda: 1000,
                                     lambda _ms: None, timeout_ms=3_000)
    ok &= check("a feed that never grows reports NO_GROWTH",
                verdict == page_flow.NO_GROWTH)
    ok &= check("and it did scroll rather than give up immediately",
                calls["scrolls"] >= 1)

    grown = iter([4, 4, 9])

    def growing(_selector):
        return next(grown, 9)

    verdict = page_flow.advance_feed(lambda: None, growing, lambda: 1000,
                                     lambda _ms: None, timeout_ms=5_000)
    ok &= check("a feed that grows reports ADVANCED",
                verdict == page_flow.ADVANCED)

    group("Every engine must reach the same verdict (§6)")
    # Selenium has no response listener, so the refused-GraphQL count comes
    # out of Chrome's performance log instead. If it silently answered 0, one
    # engine would report `complete` where its twins report `partial` on the
    # identical run — which is exactly the drift the shared modules exist to
    # prevent.
    for engine in ENGINES:
        source = _engine_source(engine)
        if source is None:
            continue
        ok &= check(f"{engine} counts refused GraphQL responses",
                    "_graphql_refused" in source)
        # The same THRESHOLD in all three, not merely the same field name. A
        # threshold that differed would mean one engine reporting `complete`
        # where its twins report `partial` on the identical run — and this
        # caught exactly that: the Playwright engine kept a sibling's
        # `status == 429` while the other two used `>= 400`.
        ok &= check(f"{engine} treats any >= 400 as a refusal, not only 429",
                    ">= 400" in source and "== 429" not in source)
        ok &= check(f"{engine} watches the same endpoint path",
                    '_GRAPHQL_PATH = "/graphql/"' in source)
        ok &= check(f"{engine} distinguishes exhausted from refused",
                    "no_turnover" in source and "exhausted" in source)
        # And from a third ending the canary's first dispatch produced: the
        # page being REPLACED mid-scroll. A feed that stopped growing because
        # Cloudflare arrived has not run out, and `exhausted` is a COMPLETE
        # stop reason — so without this a run blocked halfway would claim the
        # listing ended.
        ok &= check(f"{engine} checks WHAT PAGE it is on before calling the "
                    f"feed exhausted",
                    "blocked_mid_scroll" in source
                    and "counts_as_blocked" in source)
    return ok


def test_output_contract():
    group("The output contract (§9)")
    ok = True
    ok &= check("every mode yields the same row class",
                set(ROW_CLASS_BY_MODE.values()) == {Post})
    ok &= check("the four modes are the four views",
                set(ROW_CLASS_BY_MODE) == {"tag", "archive", "author", "post"})
    ok &= check("every mode is one row per sku",
                set(UNIQUE_BY_SKU_MODES) == set(ROW_CLASS_BY_MODE))
    ok &= check("Product is kept as an alias so family code keeps importing",
                Product is Post)
    ok &= check("JSON and CSV agree on column order",
                [f.name for f in fields(Post)]
                == list(asdict(Post()).keys()))
    ok &= check("source defaults to the main host, never to an empty string",
                SOURCE_DEFAULT == "medium.com" and Post().source == SOURCE_DEFAULT)

    group("Complete stop reasons")
    ok &= check("`completed` is complete", "completed" in COMPLETE_STOP_REASONS)
    ok &= check("`no_new_products` is complete — the data-side condition",
                "no_new_products" in COMPLETE_STOP_REASONS)
    ok &= check("a refused batch is NOT a complete stop reason",
                "next_batch_refused" not in COMPLETE_STOP_REASONS)
    ok &= check("nor is a block",
                not any(r.startswith("blocked") for r in COMPLETE_STOP_REASONS))
    return ok


def test_writers_and_finish_run():
    group("Writers")
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        # A run that finds nothing writes NOTHING — never replacing last
        # night's good output with [].
        with open(prefix + ".json", "w") as f:
            f.write('[{"sku": "keep-me"}]')
        rc = save([], prefix, "both")
        ok &= check("0 rows returns exit 4", rc == EXIT_NO_PRODUCTS)
        ok &= check("0 rows leaves the previous good output alone",
                    "keep-me" in open(prefix + ".json").read())
        rc = save([], prefix, "json", allow_empty=True)
        ok &= check("--allow-empty is the opt-out", rc == EXIT_NO_PRODUCTS
                    and json.load(open(prefix + ".json")) == [])

        # An empty CSV still carries its header, so a consumer reads a table
        # with no rows instead of failing on a zero-byte file.
        csv_path = os.path.join(tmp, "empty.csv")
        write_csv([], csv_path, row_cls=Post)
        header = next(csv_module.reader(open(csv_path)))
        ok &= check("an empty CSV keeps the header",
                    header == [f.name for f in fields(Post)])

        # `tags` is a LIST, and CSV cannot hold one. The family's writer
        # joins it with LIST_CSV_SEPARATOR so the cell stays readable in a
        # spreadsheet and round-trippable by splitting on the same string —
        # `repr()` of a Python list, which is the default if this is not
        # handled, is neither readable nor parseable by anything but Python.
        row = rows_of("tag_feed")[1]
        csv2 = os.path.join(tmp, "rows.csv")
        write_csv([row], csv2, row_cls=Post)
        body = list(csv_module.DictReader(open(csv2)))[0]
        ok &= check("tags is the one list column", isinstance(row.tags, list))
        ok &= check("...and the CSV joins it rather than repr-ing it",
                    body["tags"] == LIST_CSV_SEPARATOR.join(row.tags))
        ok &= check("...and it round-trips by splitting on the same string",
                    body["tags"].split(LIST_CSV_SEPARATOR) == row.tags)
        ok &= check("every OTHER column is scalar",
                    not any(isinstance(getattr(row, f.name), (list, dict))
                            for f in fields(Post) if f.name != "tags"))
        ok &= check("the CSV round-trips the sku",
                    body["sku"] == row.sku)
        ok &= check("the list separator is still available for the family",
                    bool(LIST_CSV_SEPARATOR))

        group("finish_run — the status/exit mapping all three engines share")
        def run(rows, stop_reason, blocked=False, allow_empty=False):
            out = os.path.join(tmp, f"r{abs(hash(stop_reason))}{len(rows)}{blocked}")
            code = finish_run(rows, out, "json", allow_empty, blocked=blocked,
                              stop_reason=stop_reason, pages_requested=2,
                              pages_completed=1, start_url="u", final_url="u")
            meta_path = out + ".meta.json"
            meta = json.load(open(meta_path)) if os.path.exists(meta_path) else None
            return code, meta

        rows = rows_of("tag_feed")
        code, meta = run(rows, "completed")
        ok &= check("a finished run is complete, exit 0",
                    code == 0 and meta["status"] == "complete")
        code, meta = run(rows, "next_batch_refused")
        ok &= check("a REFUSED batch is partial, exit 6",
                    code == EXIT_PARTIAL and meta["status"] == "partial")
        code, meta = run(rows, "no_new_products")
        ok &= check("a feed that added nothing new is complete, exit 0",
                    code == 0 and meta["status"] == "complete")
        code, meta = run(rows, "pagination_exhausted")
        ok &= check("a feed that ran out is complete, exit 0",
                    code == 0 and meta["status"] == "complete")
        code, meta = run([], "blocked_cloudflare", blocked=True)
        ok &= check("blocked with no rows is exit 3", code == EXIT_BLOCKED)
        ok &= check("a FAILED run writes no sidecar beside good data",
                    meta is None)
        code, meta = run([], "completed")
        ok &= check("empty and not blocked is exit 4", code == EXIT_NO_PRODUCTS)

        # THE DISTINCTION A LIVE DEAD PROXY FOUND. Zero rows has three
        # causes and they are not the same thing (§8: blocked is not empty).
        # This one used to return exit 4 — "ran fine, found nothing" — on a
        # run that never reached the site at all, while the sidecar beside
        # it correctly said `status: failed`, `pages_completed: 0`. A
        # pipeline branching on the exit code, which is what this family
        # says exit codes are for, would have recorded an empty catalogue.
        code, meta = run([], "page_load_timeout", allow_empty=True)
        ok &= check("0 rows because nothing was FETCHED is exit 6, not 4",
                    code == EXIT_PARTIAL)
        ok &= check("and the sidecar says failed, not complete",
                    meta is not None and meta["status"] == "failed")
        code, meta = run([], "next_batch_refused", allow_empty=True)
        ok &= check("a refused batch with no rows is exit 6 too",
                    code == EXIT_PARTIAL)
        code, meta = run([], "blocked_cloudflare", blocked=True, allow_empty=True)
        ok &= check("but a BLOCK still outranks both, at exit 3",
                    code == EXIT_BLOCKED)

        group("The sidecar records WHICH pages failed, by number")
        out = os.path.join(tmp, "meta")
        finish_run(rows, out, "json", False, blocked=False,
                   stop_reason="blocked_x", pages_requested=5, pages_completed=3,
                   pages_failed=[2, 4], start_url="u", final_url="u",
                   mode="tag", source="medium.com",
                   extra={"rows_new_per_batch": {2: 10},
                          "pagination": "infinite-scroll"})
        meta = json.load(open(out + ".meta.json"))
        ok &= check("pages_failed is a list of numbers", meta["pages_failed"] == [2, 4])
        ok &= check("mode and source are recorded",
                    meta["mode"] == "tag" and meta["source"] == "medium.com")
        ok &= check("the per-batch row counts ride in the sidecar",
                    meta["rows_new_per_batch"] == {"2": 10})
        ok &= check("and so does the pagination model, which a consumer "
                    "needs before comparing two runs",
                    meta["pagination"] == "infinite-scroll")
        ok &= check("extra cannot overwrite a run field",
                    meta["status"] == "partial")

    group("Merging and dedupe")
    seen = set()
    p1 = rows_of("tag_feed", 1)
    ok &= check("a fresh batch keeps every row",
                len(dedupe_by_key(p1, seen)) == len(p1))
    # EXPECTED here rather than exceptional: a scroll batch re-parses the
    # WHOLE feed, so batch 2 arrives holding batch 1's rows again by
    # construction, and a batch that drops all of them is how this repo knows
    # the feed is exhausted.
    ok &= check("the same feed again is fully dropped",
                dedupe_by_key(rows_of("tag_feed", 2), seen) == [])
    ok &= check("dedupe_by_sku is the same function",
                dedupe_by_sku([], set()) == [])
    # A row with no key is always kept: there is nothing to check a duplicate
    # against, and dropping it would be a silent data loss.
    keyless = [Post(sku=None, title="a"), Post(sku=None, title="b")]
    ok &= check("keyless rows are kept, not collapsed",
                len(dedupe_by_key(keyless, set())) == 2)
    return ok


def test_diff():
    group("diff_runs — what counts as a change here")
    ok = True

    def row(**kw):
        base = dict(sku="d373fe2c96b7", title="A story title",
                    claps=100, responses=2, reading_time_min=7.5,
                    word_count=1800, content_chars=None,
                    is_paywalled=False, publication=None,
                    data_source="obvinit")
        base.update(kw)
        return base

    out = diff_products([row()], [row(claps=140)])
    ok &= check("a real clap move, same view, is `changed`",
                len(out["changed"]) == 1 and not out["source_changed"])

    # THE BUCKET THIS SITE NEEDS MOST. A row read off a tag feed has no
    # reading time and no word count; the same story read off its day archive
    # has both. That is our two snapshots differing, not the site.
    out = diff_products([row(data_source="apollo", reading_time_min=None,
                             word_count=None)],
                        [row()])
    ok &= check("a counter appearing with the view is `source_changed`, "
                "not `changed`",
                len(out["source_changed"]) == 1 and not out["changed"])
    ok &= check("the bucket names both views",
                out["source_changed"][0]["data_source"]
                == {"old": "apollo", "new": "obvinit"})

    # A body appearing with post mode is the same artefact.
    out = diff_products([row(data_source="apollo", content_chars=None)],
                        [row(content_chars=18352)])
    ok &= check("a body appearing with the view is `source_changed` too",
                len(out["source_changed"]) == 1 and not out["changed"])

    # But a title moving alongside is a REAL change and must not be
    # swallowed by the same bucket: Medium lets a story be retitled.
    out = diff_products([row(data_source="apollo", reading_time_min=None)],
                        [row(title="A retitled story")])
    ok &= check("a title change survives a view change",
                len(out["changed"]) == 1
                and "title" in out["changed"][0]["changes"])

    # And so is a story going behind the paywall, which is the event this
    # column exists for and which no view difference can explain away.
    out = diff_products([row(data_source="apollo", reading_time_min=None)],
                        [row(is_paywalled=True)])
    ok &= check("a story moving behind the paywall survives a view change",
                len(out["changed"]) == 1
                and "is_paywalled" in out["changed"][0]["changes"])

    group("The tolerance, which unlike the family's has a real use here")
    out = diff_products([row(claps=1000)], [row(claps=1004)],
                        price_tolerance_pct=1.0)
    ok &= check("a live counter ticking is `within_tolerance`",
                len(out["within_tolerance"]) == 1 and not out["changed"])
    out = diff_products([row(claps=1000)], [row(claps=1004)])
    ok &= check("and the DEFAULT reports it, deciding nothing for the reader",
                len(out["changed"]) == 1 and not out["within_tolerance"])
    out = diff_products([row(claps=1000)],
                        [row(claps=1004, title="A retitled story")],
                        price_tolerance_pct=1.0)
    ok &= check("a title change alongside is never absorbed by a tolerance",
                len(out["changed"]) == 1 and not out["within_tolerance"])

    group("added / removed / unmatchable")
    out = diff_products([row()], [row(sku="bfd47b63fdae")])
    ok &= check("a new permalink is added and the old one removed",
                len(out["added"]) == 1 and len(out["removed"]) == 1)
    out = diff_products([row(sku=None)], [row(sku=None)])
    ok &= check("a row with no sku is unmatchable, not added or removed",
                out["unmatchable_old"] == 1 and out["unmatchable_new"] == 1
                and not out["added"] and not out["removed"])
    out = diff_products([row(), row()], [row()])
    ok &= check("a duplicate sku within one file is counted, not clobbered",
                out["unmatchable_old"] == 1)

    group("lifecycle is emitted and always empty, for the family's shape")
    ok &= check("the key is there", "lifecycle" in diff_products([], []))
    ok &= check("and it is empty", diff_products([row()], [row(upvotes=1)])
                ["lifecycle"] == [])

    group("--fail-on-change ignores what is about US, not the site")
    source = _fail_on_change_source()
    ok &= check("it fails on added/removed/changed",
                'result["added"] or result["removed"] or result["changed"]'
                in source)
    ok &= check("and on nothing else",
                "source_changed" not in source.split("fail_on_change")[-1]
                .split("return")[0])
    return ok


def _fail_on_change_source():
    """The `--fail-on-change` condition, as written."""
    src = open(os.path.join(REPO_ROOT, "diff_runs.py"), encoding="utf-8").read()
    match = re.search(r"if args\.fail_on_change and \(([^)]*)\)", src)
    return match.group(1) if match else src


def test_env_config():
    group("env_config — precedence and placeholders")
    ok = True
    ok &= check("every ENV_KEYS value is a real CLI destination",
                set(env_config.ENV_KEYS.values()) ==
                {"twocaptcha_key", "cdp_endpoint", "proxy", "url"})
    # A variable mapped onto a flag with a non-empty default would be
    # silently inert — a setting that looks configurable and is not.
    ok &= check("--out is deliberately NOT mapped",
                "out" not in env_config.ENV_KEYS.values())

    # .env.example must document exactly what the code reads, both ways.
    example = open(os.path.join(REPO_ROOT, ".env.example"), encoding="utf-8").read()
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", example, re.M))
    ok &= check("every ENV_KEYS name is in .env.example",
                set(env_config.ENV_KEYS) <= documented)
    ok &= check("every .env.example name is read by the code",
                documented <= set(env_config.ENV_KEYS))

    group("A COPIED .env.example must read as unset (§17)")
    # `cp .env.example .env` followed by a run used to connect with the
    # literal string `{login}-zone-...` as a username and get a 401 — the
    # confusing auth error a long way from its cause that this rule exists to
    # prevent. Round-tripped through the real loader.
    saved = {k: os.environ.get(k) for k in env_config.ENV_KEYS}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as f:
                f.write(example)
            with contextlib.redirect_stderr(io.StringIO()):
                env_config.load_env(path, override=True)
                # Every CREDENTIAL in the example must read as unset. The
                # two credentialled URLs are written the way the vendor
                # documents them, so a literal-only placeholder list misses
                # both — `cp .env.example .env` then connected with the
                # string `{login}-zone-...` as a username and got a 401 a
                # long way from its cause (§17).
                for name in ("TWOCAPTCHA_KEY", "MEDIUM_CDP_ENDPOINT",
                             "MEDIUM_PROXY"):
                    ok &= check(f"{name} from a copied example reads as unset",
                                env_config.env_value(name) is None)
                # And the non-credential default must still be USABLE, or
                # the check above would pass by making everything unset.
                url = env_config.env_value("MEDIUM_URL")
            ok &= check("MEDIUM_URL from the example survives and is usable",
                        url is not None and is_supported_host(url))
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return ok


def test_fingerprint_is_read_through_the_shared_helper():
    group("--fingerprint reads the UA through ONE helper (§16)")
    ok = True
    # The defect this pins was live in FOUR sibling repos at once and was
    # live here too, in the Selenium engine, until a live call to the API
    # showed what it returns. The UA is at `userAgent.userAgent` in the
    # chromium format and at `data.ua` in the raw one; `userAgent.value` —
    # which that engine read — exists in NEITHER. So `--fingerprint` set no
    # user agent at all, silently, and the run presented a Windows
    # fingerprint's screen, locale and timezone over a local Chromium's UA.
    # That is the identity MISMATCH the flag exists to avoid.
    for engine in ENGINES:
        source = _engine_source(engine)
        if source is None:
            continue
        if "fingerprint" not in source:
            continue
        ok &= check(f"{engine} does not reach into the response shape itself",
                    'get("userAgent")' not in source
                    and '["userAgent"]' not in source)

    # And the helper itself, against the three shapes the API is known to
    # return. Fixtures rather than a live call: the suite must pass offline.
    ok &= check("chromium format: userAgent.userAgent",
                fingerprint_user_agent({"userAgent": {"userAgent": "UA-1"}}) == "UA-1")
    ok &= check("raw format: data.ua",
                fingerprint_user_agent({"data": {"ua": "UA-2"}}) == "UA-2")
    ok &= check("a bare string is accepted too",
                fingerprint_user_agent({"userAgent": "UA-3"}) == "UA-3")
    ok &= check("and `value`, which the API does NOT return, is still read "
                "rather than being made an error — a response shape this "
                "repo has not seen is not a reason to set no UA",
                fingerprint_user_agent({"userAgent": {"value": "UA-4"}}) == "UA-4")
    ok &= check("nothing recognisable -> None, never a fabricated UA",
                fingerprint_user_agent({"userAgent": {}}) is None)

    group("Its kwargs are ones the driver actually accepts (§10)")
    # An unknown key in new_context(**kwargs) is a TypeError at launch, on
    # the paid path, at runtime.
    fp = {"userAgent": {"userAgent": "UA"},
          "screen": {"width": 1920, "height": 1080},
          "intl": {"contentLocale": "en-US", "timeZone": "America/New_York"}}
    kwargs = playwright_context_kwargs(fp)
    try:
        from playwright.sync_api import Browser
        allowed = set(inspect.signature(Browser.new_context).parameters)
        unknown = sorted(set(kwargs) - allowed)
        ok &= check(f"every context kwarg is a real one "
                    f"{'' if not unknown else unknown}", not unknown)
    except ImportError:
        skips_note = "playwright absent, context-kwarg binding not checked"
        ok &= check(skips_note, True)

    # The locale must come from the fingerprint, not be built out of its
    # country: a sibling family shipped `en-{country}` and gave every German
    # fingerprint the locale `en-DE`, which is not a locale anyone has.
    ok &= check("locale comes from the fingerprint's own intl block",
                kwargs.get("locale") == "en-US")
    ok &= check("and so does the timezone, which was never applied at all "
                "in four sibling repos",
                kwargs.get("timezone_id") == "America/New_York")
    return ok


def test_fingerprint_cache_is_key_aware():
    group("a cached fingerprint must not make a bad key look good")
    ok = True
    import fingerprint_client as fc

    params = {"format": "chromium", "tags": "Windows", "country": "us"}
    a = fc._cache_path("/tmp", params, False, "key-one")
    b = fc._cache_path("/tmp", params, False, "key-two")
    same = fc._cache_path("/tmp", params, False, "key-one")
    # Measured 2026-09-16 before this was fixed: get_fingerprint() with the
    # key "deadbeef"*4 returned fingerprint 5393493 off disk and raised
    # nothing, because a REAL key had cached the same parameters earlier. A
    # user whose fingerprint subscription lapsed would see --fingerprint keep
    # working on their own machine and 401 on a fresh one — §16's "a path
    # that looks like it works", in the one place this family has already
    # been bitten five times.
    ok &= check("two keys do not share a cache entry", a != b)
    ok &= check("the same key is stable across calls", a == same)
    ok &= check("different parameters still differ",
                a != fc._cache_path("/tmp", {**params, "country": "de"},
                                    False, "key-one"))
    # The key must not be recoverable from the path it produces.
    ok &= check("the key never reaches the filename",
                "key-one" not in a and len(pathlib.Path(a).stem) == 16)
    # And the caller must actually pass it — a key-aware helper nobody hands
    # a key to is the §17 defect this family names dead policy.
    src = open(os.path.join(REPO_ROOT, "fingerprint_client.py"),
               encoding="utf-8").read()
    ok &= check("get_fingerprint passes the key to the cache path",
                src.count("_cache_path(cache_dir, params, generate, api_key)") == 2)
    return ok


def test_proxy_pool():
    group("Credentials never reach argv or a log")
    ok = True
    url = "http://user:" + "s3cr3t" + "@exit.example.com:2334"
    masked = mask(url)
    ok &= check("the password is masked", "s3cr3t" not in masked)
    ok &= check("the host and port are KEPT — that is the point of the log",
                "exit.example.com" in masked and "2334" in masked)
    scrubbed, credentials = split_credentials(url)
    ok &= check("split_credentials strips them from the address",
                "s3cr3t" not in scrubbed and credentials == ("user", "s3cr3t"))
    pw = to_playwright(url)
    ok &= check("Playwright gets them in its own fields, not in the server URL",
                pw["password"] == "s3cr3t" and "s3cr3t" not in pw["server"])

    group("A worker owns one exit; rotation is a fresh browser")
    pool = ProxyPool(["http://a@h1:1", "http://b@h2:2", "http://c@h3:3"])
    first = pool.current
    pool.advance("test")
    ok &= check("advance moves to a different exit", pool.current != first)
    ok &= check("the pool knows its size", len(pool) == 3)
    return ok


def test_credentials_never_reach_a_log():
    group("An EXCEPTION MESSAGE is a log (§8)")
    ok = True
    secret = "hunter2"
    # Concatenated rather than interpolated, so no line in this file holds a
    # complete `scheme://user:pass@host` literal. That keeps ci_checks.py's
    # credential scan meaningful on the one file where a real credential is
    # most likely to be pasted while debugging — an allowlist entry here
    # would switch the check off exactly where it matters.
    endpoint = "ws://user:" + secret + "@cb.2captcha.com:9222"
    for engine in ENGINES:
        try:
            module = __import__(engine)
        except ImportError:
            continue
        masker = getattr(module, "_mask_credentials", None)
        if masker is None:
            ok &= check(f"{engine} has a credential masker", False)
            continue
        # Globally, not once: a Playwright connection error repeats the
        # endpoint five times, and a masker that handles the first prints the
        # password the other four while looking like it works.
        repeated = " ".join([endpoint] * 5)
        ok &= check(f"{engine} masks EVERY occurrence",
                    secret not in masker(repeated))
        ok &= check(f"{engine} keeps the host and port",
                    "cb.2captcha.com:9222" in masker(endpoint))
    # And the solver redacts a key out of an error message, because the
    # fingerprint API takes its key as a query parameter and `requests` puts
    # the full URL into the text of every error it raises.
    key = "a" * 32
    redacted = captcha_solver._redact(f"GET https://x/y?key={key} failed")
    ok &= check("the solver redacts a key from an error message",
                key not in redacted)
    return ok


def test_driver_primitives_tolerate_a_navigation():
    group("Every driver primitive survives the page moving under it")
    ok = True
    # The canary's FIRST dispatch caught this, which is exactly why §15 says
    # to dispatch it once rather than trusting the badge. A scroll batch was
    # polling the card count when the page navigated — Cloudflare's challenge
    # can arrive at any moment here — and one engine raised
    # `Execution context was destroyed, most likely because of a navigation`.
    # Exit 1, a CRASH, where the honest answer was "blocked".
    #
    # Its two twins had guarded the same call from the start. That is the
    # same shape as the refused-GraphQL threshold: two engines agree, one
    # does not, and only a live run in a different environment shows it.
    #
    # Checked as TEXT so this needs no engine library, and by structure
    # rather than by phrasing: the function must contain a try and a return
    # of 0.
    for engine in ENGINES:
        source = _engine_source(engine)
        if source is None:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name != "_count":
                continue
            guarded = any(isinstance(child, ast.Try) for child in node.body)
            zero = any(isinstance(child, ast.Return)
                       and isinstance(child.value, ast.Constant)
                       and child.value.value == 0
                       for child in ast.walk(node))
            ok &= check(f"{engine}._count catches the driver's error", guarded)
            ok &= check(f"{engine}._count answers 0 rather than raising", zero)
            break
        else:
            ok &= check(f"{engine} has a _count primitive", False)

    # The other primitives the scroll loop drives, for the same reason.
    for engine in ENGINES:
        source = _engine_source(engine)
        if source is None:
            continue
        tree = ast.parse(source)
        for name in ("_page_height", "_scroll_to_bottom"):
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == name:
                    guarded = any(isinstance(c, ast.Try) for c in node.body)
                    ok &= check(f"{engine}.{name} is guarded too", guarded)
                    break
            else:
                ok &= check(f"{engine} has {name}", False)
    return ok


def test_concurrency_machinery(skips):
    """§10: drive the worker pool with the browser stubbed out.

    A live run cannot reach this. Page 1 is fetched alone and its answer
    decides whether the rest may be addressed, so a blocked page 1 means the
    workers never start — and on this site page 1 is blocked often enough
    that a live test would pass by not running.

    The pool only exists in the Playwright engine (Selenium and pyppeteer
    walk the days one at a time and say so), which is why this group targets
    that engine alone.
    """
    group("the archive worker pool, with no browser in it")
    ok = True
    try:
        import playwright_scraper as eng
    except ImportError:
        skips.append("playwright_scraper (playwright not installed)")
        return ok

    import threading
    import types

    class _Args:
        delay = 0
        out = "unused"

    def _run(pages, behaviour, concurrency=3):
        """Drive the real dispatcher against a stubbed session + fetcher.

        `behaviour(page_num)` returns the PageOutcome for that page, or
        raises to simulate a worker dying.
        """
        seen, lock = [], threading.Lock()
        fake_session = types.SimpleNamespace(
            pool=None, close=lambda: None, open=lambda: fake_session)

        def fake_fetch(session, args, pool, page_num, url):
            with lock:
                seen.append(page_num)
            return behaviour(page_num, url)

        real_session, real_fetch, real_pw = (
            eng._BrowserSession, eng._fetch_one_page, eng.sync_playwright)
        eng._BrowserSession = lambda *a, **k: fake_session
        eng._fetch_one_page = fake_fetch
        # The dispatcher opens a Playwright context per worker; hand it one
        # that does nothing rather than launching three real browsers.
        #
        # A real CLASS, not a SimpleNamespace with `__enter__` attached:
        # Python looks dunder methods up on the TYPE, so an instance
        # attribute named `__enter__` is never called and `with` raises —
        # which the worker's own except-clause swallows, leaving a test that
        # "passes" against zero workers. It cost this check a debugging pass.
        class _NoPlaywright:
            def __enter__(self):
                return None

            def __exit__(self, *exc):
                return False

        eng.sync_playwright = _NoPlaywright
        try:
            specs = [(n, "https://medium.com/tag/python/archive/2026/09/%02d" % n)
                     for n in pages]
            return eng._fetch_pages_concurrently(_Args(), None, specs,
                                                 concurrency), seen
        finally:
            eng._BrowserSession, eng._fetch_one_page, eng.sync_playwright = (
                real_session, real_fetch, real_pw)

    def good(page_num, url, rows=3):
        o = eng.PageOutcome(page_num=page_num, url=url)
        o.products = [Post(sku="%012x" % (page_num * 1000 + i), url=url,
                           title="t%d" % i) for i in range(rows)]
        o.state = "content"
        return o

    # 1. Every queued page is fetched EXACTLY once. A page fetched twice is
    #    paid for twice and deduped silently; a page fetched zero times is a
    #    hole the run would report as complete.
    (results, unattempted, exhausted), seen = _run(
        list(range(2, 10)), lambda n, u: good(n, u))
    ok &= check("every queued day is fetched", sorted(seen) == list(range(2, 10)))
    ok &= check("...exactly once", len(seen) == len(set(seen)))
    ok &= check("every fetch produced an outcome", len(results) == 8)
    ok &= check("nothing is left unattempted when all succeed", unattempted == [])
    ok &= check("the end-of-listing event did not fire", not exhausted)

    # 2. Outcomes come back in ARRIVAL order and must be restorable to PAGE
    #    order — §8's "merge in page order, not arrival order". With workers
    #    the two genuinely differ.
    ordered = sorted(results, key=lambda o: o.page_num)
    ok &= check("outcomes carry their page number",
                [o.page_num for o in ordered] == list(range(2, 10)))
    ok &= check("...and each kept its own URL",
                all(str(o.page_num).zfill(2) in o.url for o in ordered))

    # 3. A day with no rows ends dispatch. Without this, asking for 40 days
    #    of a tag that published on three fetches 37 empty ones.
    def empty_after_4(page_num, url):
        return good(page_num, url, rows=0 if page_num >= 4 else 3)

    (results, unattempted, exhausted), seen = _run(
        list(range(2, 40)), empty_after_4, concurrency=2)
    ok &= check("an empty day stops dispatch", exhausted)
    ok &= check("...and most of the queue is never fetched", len(seen) < 12)
    ok &= check("...with the unfetched days reported, not counted as failed",
                len(unattempted) == 38 - len(seen))
    ok &= check("unattempted days are page NUMBERS, in order",
                unattempted == sorted(unattempted))

    # 4. A worker that raises must not hang the run and must not take its
    #    siblings' pages with it. This is the one that would otherwise be
    #    discovered as a hung CI job.
    def explode_on_5(page_num, url):
        if page_num == 5:
            raise RuntimeError("simulated worker death")
        return good(page_num, url)

    (results, unattempted, exhausted), seen = _run(
        list(range(2, 8)), explode_on_5, concurrency=2)
    ok &= check("a dying worker does not hang the run", True)  # reaching here IS the check
    ok &= check("...and its siblings' pages still arrive",
                {o.page_num for o in results} >= {2, 3, 4})
    ok &= check("...and nothing claims the dead worker's pages succeeded",
                5 not in {o.page_num for o in results})

    # 5. The pool is refused where it cannot help, and that refusal is DATA
    #    rather than three copies of an if-chain.
    ok &= check("a day archive may use workers",
                page_flow.concurrency_limit(
                    "https://medium.com/tag/python/archive/2026/09/10") is None)
    for url in ("https://medium.com/tag/python",
                "https://medium.com/@someone",
                "https://medium.com/p/d373fe2c96b7"):
        ok &= check("%s is capped at one worker" % url,
                    page_flow.concurrency_limit(url) == 1)
        ok &= check("...with a reason naming the day archive",
                    "day archive" in (page_flow.concurrency_refusal(url) or ""))
    return ok


def test_engine_parity(skips):
    group("The three engines agree — flags, in BOTH directions (§17)")
    ok = True
    flagsets = {}
    for engine in ENGINES:
        src = open(os.path.join(REPO_ROOT, f"{engine}.py"), encoding="utf-8").read()
        flagsets[engine] = set(re.findall(r'p\.add_argument\("(--[a-z0-9-]+)"', src))

    # The family contract (§9). Every engine must carry all of these.
    contract = {"--url", "--pages", "--category", "--format", "--out", "--delay",
                "--retries", "--retry-delay", "--concurrency", "--proxy",
                "--proxy-file", "--proxy-rotate", "--proxy-shuffle",
                "--proxy-block-retries", "--twocaptcha-key", "--captcha-api",
                "--solve-captcha", "--min-score", "--cdp-endpoint",
                "--allow-empty", "--dump-html", "--headless", "--headful",
                "--mode"}
    for engine, flags in flagsets.items():
        missing = contract - flags
        ok &= check(f"{engine} carries the whole contract "
                    f"{'' if not missing else sorted(missing)}", not missing)

    # And the DOCUMENTED differences, asserted in both directions so closing
    # one needs a README edit rather than a quiet patch.
    documented_extra = {
        # --cdp-connect-timeout is on the two engines that can actually USE
        # an authenticated CDP endpoint. Selenium cannot (chromedriver's
        # debuggerAddress has nowhere to put a password), so a connect
        # timeout there would be a flag for a path that does not exist.
        "playwright_scraper": {"--locale", "--fingerprint", "--fp-tags",
                               "--fp-country", "--browser-channel",
                               "--cdp-connect-timeout"},
        "selenium_scraper": {"--locale", "--fingerprint", "--fp-tags",
                             "--fp-country"},
        "puppeteer_scraper": {"--chromium-path", "--cdp-connect-timeout"},
    }
    for engine, extra in documented_extra.items():
        actual = flagsets[engine] - contract
        ok &= check(f"{engine}'s extra flags are exactly the documented set",
                    actual == extra)

    group("Every shared-module call binds against the real signature (§17)")
    problems = []
    for engine in ENGINES:
        path = os.path.join(REPO_ROOT, f"{engine}.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        imported = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in SHARED_MODULES:
                for alias in node.names:
                    imported[alias.asname or alias.name] = (node.module, alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = None
            if isinstance(node.func, ast.Name) and node.func.id in imported:
                module, name = imported[node.func.id]
                fn = getattr(SHARED_MODULES[module], name, None)
            elif (isinstance(node.func, ast.Attribute)
                  and isinstance(node.func.value, ast.Name)
                  and node.func.value.id in SHARED_MODULES):
                fn = getattr(SHARED_MODULES[node.func.value.id], node.func.attr, None)
            if fn is None or not callable(fn):
                continue
            if any(isinstance(a, ast.Starred) for a in node.args):
                continue
            if any(k.arg is None for k in node.keywords):
                continue
            try:
                signature = inspect.signature(fn)
            except (TypeError, ValueError):
                continue
            try:
                signature.bind(*[object()] * len(node.args),
                               **{k.arg: object() for k in node.keywords})
            except TypeError as exc:
                problems.append(f"{engine}:{node.lineno} {getattr(fn,'__name__','?')}: {exc}")
    ok &= check(f"no call site disagrees with its callee "
                f"{'' if not problems else problems[:3]}", not problems)

    group("Every engine imports its driver at MODULE level (§10)")
    # Without this the module imports cleanly with no driver installed, the
    # skip below never fires, and CI's engine-smoke job cannot notice a
    # broken import.
    drivers = {"playwright_scraper": "playwright",
               "selenium_scraper": "selenium",
               "puppeteer_scraper": "pyppeteer"}
    for engine, driver in drivers.items():
        tree = ast.parse(open(os.path.join(REPO_ROOT, f"{engine}.py"),
                              encoding="utf-8").read())
        top_level = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(driver):
                top_level.append(node)
            if isinstance(node, ast.Import):
                top_level += [a for a in node.names if a.name.startswith(driver)]
        ok &= check(f"{engine} imports {driver} at module level", bool(top_level))

    group("The browser channel — unforced here, and that is a measurement")
    # The opposite of a sibling repo, which must drive real Chrome or be
    # refused. Playwright's bundled Chromium was measured serving the full page,
    # so no channel is forced — and pinning that here is what stops someone
    # copying the sibling's default back in and quietly changing what the
    # README's numbers describe.
    try:
        import playwright_scraper as pws
        ok &= check("Playwright forces no browser channel",
                    pws.DEFAULT_BROWSER_CHANNEL is None)
    except ImportError:
        skips.append("playwright_scraper (playwright not installed)")
    sel = _engine_source("selenium_scraper") or ""
    ok &= check("Selenium says there is nothing to choose",
                "there is nothing to choose" in sel)
    pup = _engine_source("puppeteer_scraper") or ""
    # The opposite of what a sibling repo asserts, and it is measured: this
    # engine's OWN bundled Chromium is build 117.0.5938.0, the UA follows the
    # browser's real version, and Medium refused it 3 times out of 3. The
    # engine must say so in its docstring and in its block advice, because a
    # reader who concludes "my address is burned" from that is going to buy a
    # proxy they do not need.
    ok &= check("pyppeteer names the Chromium build it is refused on",
                "117.0.5938.0" in pup)
    ok &= check("...and tells the reader to pass --chromium-path",
                "--chromium-path" in pup)
    ok &= check("...and the README agrees",
                "117.0.5938.0" in open(
                    os.path.join(REPO_ROOT, "README.md"),
                    encoding="utf-8").read())

    group("The coverage floor is one number, not three")
    floors = []
    for engine in ENGINES:
        src = _engine_source(engine)
        match = re.search(r"^FIELD_FLOOR = (\d+)", src or "", re.M)
        floors.append(match.group(1) if match else None)
    ok &= check(f"all three engines share a FIELD_FLOOR ({floors[0]})",
                len(set(floors)) == 1 and floors[0] is not None)

    group("Engines import cleanly (skipped if the driver is absent)")
    for engine in ENGINES:
        try:
            __import__(engine)
            ok &= check(f"{engine} imports", True)
        except ImportError as exc:
            skips.append(f"{engine} ({exc})")
            print(f"  SKIP  {engine} — {exc}")
    return ok


def test_module_attributes_exist(skips):
    group("Every `module.name` an engine reaches for actually exists (§17)")
    ok = True
    # The gap the signature-binding check leaves, found by a live run rather
    # than by reading. `_min_matches` in one engine called
    # `page_flow.expected_cards(...)` — a name renamed in the other two and
    # not in that one — and the run died with AttributeError on its FIRST
    # fetch, exit 1. Invisible to import, to --help, to compileall, to the
    # undefined-NAME walk (it is an attribute, not a name) and to 490 green
    # assertions, because nothing but a live fetch reaches that line.
    #
    # This walks every `page_flow.X` and `product_parser.X` in every engine
    # and asserts X is really there. It needs no engine library: the modules
    # being reached INTO are the shared ones, and the reaching files are read
    # as text.
    for engine in ENGINES:
        source = _engine_source(engine)
        if source is None:
            skips.append(f"{engine} (source missing)")
            continue
        tree = ast.parse(source)
        missing = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if not isinstance(node.value, ast.Name):
                continue
            module = SHARED_MODULES.get(node.value.id)
            if module is None:
                continue
            if not hasattr(module, node.attr):
                missing.append(f"{node.value.id}.{node.attr}")
        ok &= check(f"{engine} reaches for nothing that is not there "
                    f"{'' if not missing else sorted(set(missing))}",
                    not missing)
    return ok


def test_no_dead_public_names():
    group("Every public name in the policy modules has a reader (§17)")
    ok = True
    # §17's check 5, automated. A public name nothing reads is dead code, and
    # a policy CONSTANT nothing reads is worse: the prose beside it reads
    # like enforcement. A sibling repo shipped `RETRY_ON_BLOCKED` with a
    # paragraph of measured justification and no engine consulting it.
    #
    # Scoped to the two modules that hold this repo's decisions, because the
    # family core is shared and its unused corners are another repo's
    # problem. References are counted across the whole repository INCLUDING
    # the defining module, so a helper used only by its own neighbours
    # counts — what this catches is a name with no reader anywhere at all.
    scanned = []
    for path in sorted(pathlib.Path(REPO_ROOT).rglob("*.py")):
        parts = path.relative_to(REPO_ROOT).parts
        # Skip local tools and any nested checkout. Matching on RELATIVE
        # parts, not on the absolute path: the absolute one can itself sit
        # under a directory this would otherwise exclude, and then the
        # corpus comes back empty and every name reads as dead — which is
        # how this check first "found" 91 dead names in a healthy module.
        if path.name.startswith("_"):
            continue
        if any(part in {"worktrees", ".venv", "venv", "build", "dist"}
               for part in parts):
            continue
        scanned.append(path.read_text(encoding="utf-8"))
    corpus = "\n".join(scanned)
    if len(corpus) < 10_000:
        return check("the dead-name corpus is not empty (it would make "
                     "every name look dead)", False)

    for module in ("product_parser", "page_flow"):
        source = open(os.path.join(REPO_ROOT, module + ".py"),
                      encoding="utf-8").read()
        tree = ast.parse(source)
        names = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                if not node.name.startswith("_"):
                    names.append(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        names.append(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id.isupper():
                    names.append(node.target.id)
        dead = []
        for name in names:
            # Two references minimum: the definition, and at least one read.
            hits = len(re.findall(r"\b" + re.escape(name) + r"\b", corpus))
            if hits < 2:
                dead.append(name)
        ok &= check(f"{module} has no unread public name "
                    f"{'' if not dead else sorted(dead)}", not dead)
    return ok


def test_no_undefined_names():
    group("Names that resolve, not just parse (§10)")
    # `compileall` proves a file PARSES, not that its names RESOLVE. A live
    # run of a sibling repo's engine died with NameError on a line reached
    # only while fetching, after an import had been removed — invisible to
    # import, --help, compileall and 400+ green assertions. Kept COARSE so it
    # under-reports rather than inventing problems.
    ok = True
    for name in sorted(os.listdir(REPO_ROOT)):
        if not name.endswith(".py") or name == "smoke_test.py":
            continue
        undefined = _undefined_names(os.path.join(REPO_ROOT, name))
        ok &= check(f"{name}: no undefined names "
                    f"{'' if not undefined else sorted(undefined)[:5]}", not undefined)
    return ok


def _undefined_names(path):
    source = open(path, encoding="utf-8").read()
    tree = ast.parse(source)
    bound = set(dir(__builtins__) if not isinstance(__builtins__, dict)
                else __builtins__.keys())
    bound |= set(dir(__import__("builtins")))
    # Module-level dunders are always bound and are not imports.
    bound |= {"__file__", "__name__", "__doc__", "__package__", "__spec__",
              "__loader__", "__builtins__", "__debug__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                for arg in (args.posonlyargs + args.args + args.kwonlyargs):
                    bound.add(arg.arg)
                if args.vararg:
                    bound.add(args.vararg.arg)
                if args.kwarg:
                    bound.add(args.kwarg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.comprehension,)):
            pass
        elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
            bound.update(node.names)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return used - bound


def test_dockerfile_matches_its_entrypoint():
    group("The Dockerfile COPY list against the import graph (§10)")
    # All three repos in this family once shipped an image that died with
    # ModuleNotFoundError on every invocation, --help included, because one
    # module was missing from an explicit COPY list. This check needs no
    # Docker.
    ok = True
    path = os.path.join(REPO_ROOT, "Dockerfile")
    if not os.path.exists(path):
        return check("a Dockerfile exists", False)
    dockerfile = open(path, encoding="utf-8").read()
    # Join backslash continuations first: the COPY list spans five lines, and
    # a line-by-line reader sees an empty list and passes vacuously.
    joined = re.sub(r"\\\s*\n\s*", " ", dockerfile)
    copied = set()
    for line in joined.splitlines():
        if line.strip().upper().startswith("COPY"):
            # [1:-1]: the first token is COPY and the LAST is the
            # destination. Including the destination made `./` look like
            # "copy everything" and the check passed vacuously.
            for token in line.split()[1:-1]:
                if token.endswith(".py"):
                    copied.add(os.path.basename(token))
                elif token in ("./", "."):
                    copied.update(n for n in os.listdir(REPO_ROOT)
                                  if n.endswith(".py"))

    # Walk the entrypoint's own import graph.
    entrypoints = [n for n in ENGINES if f"{n}.py" in dockerfile]
    if not entrypoints:
        entrypoints = ["playwright_scraper"]
    needed, queue = set(), list(entrypoints)
    local = {n[:-3] for n in os.listdir(REPO_ROOT) if n.endswith(".py")}
    while queue:
        module = queue.pop()
        if module in needed:
            continue
        needed.add(module)
        tree = ast.parse(open(os.path.join(REPO_ROOT, f"{module}.py"),
                              encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in local:
                queue.append(node.module)
            elif isinstance(node, ast.Import):
                queue += [a.name for a in node.names if a.name in local]
    missing = {f"{m}.py" for m in needed} - copied
    ok &= check(f"every module the entrypoint imports is COPYed "
                f"{'' if not missing else sorted(missing)}", not missing)

    group("The image carries no secrets and no test material")
    for unwanted in (".env", "smoke_test.py", "fixtures_generated.json",
                     "captures"):
        ok &= check(f"{unwanted} is not COPYed into the image",
                    unwanted not in copied and f"COPY {unwanted}" not in dockerfile)
    return ok


def test_no_file_describes_another_site():
    group("No shipped file still describes a different site (§17)")
    ok = True
    # A sibling repo's audit found "a shipped file still described another
    # site", and this repo inherited the same thing FOUR times over: a
    # CONTRIBUTING section about `data-testid="divSRPContentProducts"` and
    # sold counts, a list of invariants about auction lots and reserve
    # prices, a captcha module explaining an Akamai "Access Denied" page, and
    # an issue template about seller feedback scores. All four were copied in
    # with the family core and all four read as authoritative.
    #
    # Nothing here can tell a paragraph about Medium from a paragraph about a
    # rental site in general. What it CAN do is notice the vocabulary of the
    # specific siblings this repo was copied from, which is where the real
    # leakage comes from.
    foreign = {
        "akamai": "a sibling's bot manager",
        "datadome": "a sibling's bot manager",
        "bot or not": "a sibling's challenge page",
        "reserve_price_set": "a sibling's auction column",
        "seller_score": "a sibling's seller column",
        "stay_dates": "a sibling's booking column",
        "fewo-direkt": "a sibling's storefront",
        "stayz.com.au": "a sibling's storefront",
        "vrbo": "a sibling repo",
        "tokopedia": "a sibling repo",
        "catawiki": "a sibling repo",
        "craigslist": "a sibling repo",
        "mediamarkt": "a sibling repo",
        "farfetch": "a sibling repo",
        "divsrpcontentproducts": "a sibling's grid selector",
        "lodging-card-responsive": "a sibling's card selector",
    }
    # A CONTEXT allowlist, the same shape ci_checks.py uses for credentials,
    # because one of these words is legitimate in exactly one place. §8 says
    # captcha DETECTION stays broad — which challenge a visitor meets depends
    # on the exit and on what the address has been doing — so
    # `BOT_CHALLENGE_MARKERS` names vendors this site has never served, on
    # purpose. That is a marker list, not a description of the site, and the
    # difference is the whole point of this check.
    allowed = {("product_parser.py", "datadome")}

    checked = 0
    for path in sorted(pathlib.Path(REPO_ROOT).rglob("*")):
        rel = path.relative_to(REPO_ROOT)
        if not path.is_file() or path.suffix not in (".py", ".md", ".yml",
                                                     ".yaml", ".toml",
                                                     ".example"):
            continue
        if any(part in {"worktrees", ".venv", "venv", "build", "dist", ".git"}
               for part in rel.parts) or path.name.startswith("_"):
            continue
        # The suite names these words in order to ban them, so it cannot be
        # scanned for them without failing on its own check.
        if path.name == "smoke_test.py":
            continue
        checked += 1
        lowered = path.read_text(encoding="utf-8", errors="replace").lower()
        hits = sorted({word for word in foreign
                       if word in lowered
                       and (path.name, word) not in allowed})
        ok &= check(f"{rel} describes this site "
                    f"{'' if not hits else hits}", not hits)
    ok &= check(f"…and {checked} files were actually scanned", checked > 20)
    return ok


def test_wording():
    group("Wording enforced by a test (§12)")
    ok = True
    banned = {
        "cloud browser": "Scraping Browser API",
        "antidetect browser": "Scraping Browser API",
        "gate.2prx.com": "2captcha.com/proxy",
        "2prx.com": "2captcha.com/proxy",
        "--antidetect": "removed",
        "ANTIDETECT_LOCAL_API": "removed",
    }
    shipped = [n for n in os.listdir(REPO_ROOT)
               if n.endswith((".py", ".md", ".txt", ".toml", ".yml", ".example"))]
    for name in shipped:
        if name == "smoke_test.py":
            continue  # this file names them in order to ban them
        text = open(os.path.join(REPO_ROOT, name), encoding="utf-8",
                    errors="replace").read().lower()
        for phrase, instead in banned.items():
            ok &= check(f"{name}: no {phrase!r} (write {instead!r})",
                        phrase.lower() not in text)

    group("Removed flags stay removed — scoped to the ENGINES")
    # --country is banned on a scraper (it could disagree with the URL, and
    # here the storefront IS the hostname) and legitimate on
    # fingerprint_client.py, where it picks a fingerprint locale.
    for engine in ENGINES:
        src = open(os.path.join(REPO_ROOT, f"{engine}.py"), encoding="utf-8").read()
        ok &= check(f"{engine} has no --country flag",
                    'add_argument("--country"' not in src)
    return ok


def test_no_capture_leaks():
    group("Committed fixtures carry no credential-shaped material (§10)")
    ok = True
    text = open(_FIXTURE_PATH, encoding="utf-8").read()
    # PATTERNS, not the literals one capture happened to contain, so the next
    # capture is caught too.
    patterns = {
        "a 24+ char hex run": r"\b[0-9a-fA-F]{24,}\b",
        "a reCAPTCHA site key": r"\b6L[A-Za-z0-9_-]{20,}",
        "a Turnstile site key": r"\b0x4[A-Za-z0-9]{15,}",
        "an embedded credential": r"[a-z]+://[^\s\"/@]+:[^\s\"/@]+@",
    }
    for label, pattern in patterns.items():
        found = re.findall(pattern, text)
        ok &= check(f"no {label} in fixtures_generated.json "
                    f"{'' if not found else found[:2]}", not found)
    ok &= check("the challenge fixture still names its vendor after scrubbing",
                detect_bot_challenge(fixture("challenge")) == "cloudflare")

    # And the PEOPLE, which is the other half of §10 and the half a
    # credential grep does not cover. make_fixtures.py replaces every author
    # name, profile slug, credential and answer body before writing; these
    # are the shapes that would show a pass having been skipped.
    ok &= check("no author name survives except the placeholder",
                "Fixture Author" in text)
    ok &= check("every author handle is a placeholder",
                not re.search(r"/@(?!fixture-author)[A-Za-z]", text))
    # Medium's own asset hosts are hosts, not people, and they must SURVIVE:
    # `served_by_medium` needs at least three of them, and a scrub that took
    # them out would turn every good fixture into a blocked one.
    subdomains = set(re.findall(r"(?<=//)([a-z0-9-]+)\.medium\.com", text))
    ok &= check("every author subdomain is a placeholder",
                not {s for s in subdomains
                     if not s.startswith("fixture-author")
                     and s not in ("miro", "cdn-client", "glyph")})
    ok &= check("...and Medium's own asset hosts survived the scrub",
                {"miro", "cdn-client", "glyph"} <= subdomains)
    return ok


def test_ci_checks_is_wired_up():
    group("One credential check, invoked from CI and from here (§17)")
    ok = True
    script = os.path.join(REPO_ROOT, ".github", "ci_checks.py")
    ok &= check("ci_checks.py exists", os.path.exists(script))
    workflow = os.path.join(REPO_ROOT, ".github", "workflows", "tests.yml")
    if os.path.exists(workflow):
        text = open(workflow, encoding="utf-8").read()
        # A check nothing runs is not a check; two sources of truth that
        # disagree is worse.
        ok &= check("tests.yml CALLS ci_checks.py rather than reimplementing it",
                    "ci_checks.py" in text)
    if os.path.exists(script):
        # And it must pass on THIS repo. A check that fails on its own
        # repository is a check nobody can read.
        import subprocess
        result = subprocess.run([sys.executable, script, "--all"],
                                cwd=REPO_ROOT, capture_output=True, text=True)
        ok &= check(f"ci_checks.py passes on this repo "
                    f"{'' if result.returncode == 0 else result.stdout[-300:]}",
                    result.returncode == 0)
    return ok


def test_sample_output():
    group("sample_output is cut from a real run")
    ok = True
    for name, loader in (("sample_output.json", json.load),):
        path = os.path.join(REPO_ROOT, name)
        if not os.path.exists(path):
            ok &= check(f"{name} exists", False)
            continue
        rows = loader(open(path, encoding="utf-8"))
        ok &= check(f"{name} is a non-empty list", isinstance(rows, list) and rows)
        columns = [f.name for f in fields(Product)]
        ok &= check(f"{name} columns match Product exactly",
                    all(list(r) == columns for r in rows))
        # Fabrication markers — a sample nobody ran reads exactly like one
        # somebody did.
        text = json.dumps(rows)
        for marker in ("example.com", "lorem", "PLACEHOLDER", "TODO", "foo bar"):
            ok &= check(f"{name}: no {marker!r}", marker.lower() not in text.lower())
        ok &= check(f"{name}: every row names the host that served it",
                    all(is_supported_host("https://%s/" % r["source"])
                        or r["source"].endswith(".medium.com")
                        or r["source"] in MEDIUM_HOSTS for r in rows))
        ok &= check(f"{name}: every sku is a real post id",
                    all(re.fullmatch(r"[0-9a-f]{12}", r["sku"]) for r in rows))
        ok &= check(f"{name}: page+position unique",
                    len({(r["page"], r["position"]) for r in rows}) == len(rows))
    csv_path = os.path.join(REPO_ROOT, "sample_output.csv")
    if os.path.exists(csv_path):
        header = next(csv_module.reader(open(csv_path, encoding="utf-8")))
        ok &= check("sample_output.csv header matches Product",
                    header == [f.name for f in fields(Product)])
    else:
        ok &= check("sample_output.csv exists", False)
    return ok


def test_required_files_are_committed():
    group("Everything the suite needs is tracked by git")
    # A blanket `*.json` / `*.csv` in .gitignore — which this repo wants,
    # because a scraper's own output is large and stale — silently swallowed
    # `fixtures_generated.json`. The suite was green on the machine that
    # wrote it and every CI job died with FileNotFoundError at import. A
    # check that a file EXISTS cannot see that; only asking git can.
    ok = True
    import subprocess
    required = ("fixtures_generated.json", "sample_output.json",
                "sample_output.csv", ".env.example", "README.md",
                "CHANGELOG.md", "Dockerfile", "requirements.txt",
                ".github/ci_checks.py", ".github/workflows/tests.yml",
                ".github/workflows/canary.yml", "tests/test_smoke.py")
    result = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                            capture_output=True, text=True)
    if result.returncode != 0:
        print("  SKIP  not a git checkout — cannot verify what is committed")
        return ok
    tracked = set(result.stdout.split())
    for name in required:
        ok &= check(f"{name} is committed, not just present on disk",
                    name in tracked)
    # And the other direction: nothing a scraper produced should be.
    leaked = [f for f in tracked
              if re.search(r"_debug\.(html|png)$|\.meta\.json$|^captures/|^\.env$",
                           f)]
    ok &= check(f"no run output or capture is committed "
                f"{'' if not leaked else leaked[:3]}", not leaked)
    return ok


def test_readme_claims():
    group("README numbers exist and are dated")
    ok = True
    path = os.path.join(REPO_ROOT, "README.md")
    if not os.path.exists(path):
        return check("README.md exists", False)
    readme = open(path, encoding="utf-8").read()
    ok &= check("the README names all four modes",
                all(("--mode " + m) in readme
                    for m in ("tag", "archive", "author", "post")))
    ok &= check("it states what the block actually is",
                "403" in readme and "managed" in readme.lower())
    ok &= check("it says a captcha solve buys nothing here",
                "sitekey" in readme)
    # The finding that decides whether a reader gets any data at all, and
    # the one this repo got wrong before measuring it properly.
    ok &= check("it names the HeadlessChrome UA token as what is blocked",
                "HeadlessChrome" in readme)
    ok &= check("it says which modes carry a reading time and which do not",
                "reading_time_min" in readme)
    ok &= check("it warns that a publication page carries ids and no data",
                "publication" in readme.lower() and "no post data" in readme)
    ok &= check("it says the page parameter is IGNORED rather than failing",
                "?page=2" in readme and "ignored" in readme)
    ok &= check("it says concurrency is refused",
                "--concurrency" in readme)
    # Every claim is measured or absent: a number without a date goes stale
    # invisibly.
    ok &= check("measurements carry a date", "2026-09-16" in readme)

    group("The README's numbers match the artefacts on disk (§17)")
    # A number a reader can check is worth more than one they cannot. These
    # are re-derived from the committed fixtures rather than retyped, so a
    # claim that goes stale fails here rather than in a reader's terminal.
    feed = rows_of("tag_feed")
    archive = rows_of("archive_day")
    ok &= check("every archive row comes from the legacy payload, as stated",
                all(r.data_source == "obvinit" for r in archive))
    ok &= check("every archive row carries a reading time, as stated",
                all(r.reading_time_min is not None for r in archive))
    ok &= check("NO tag-feed row carries one, as stated",
                all(r.reading_time_min is None for r in feed))
    ok &= check("the README says which columns that affects",
                all(c in readme for c in ("reading_time_min", "word_count",
                                          "language")))
    # Every column the README lists must exist, and every column that exists
    # must be listed — in both directions, because a column missing from the
    # list is a column nobody knows they have.
    listed = set(re.findall(r"`([a-z_]+)`", readme.split("### Columns")[1]
                            .split("---")[0]))
    actual = {f.name for f in fields(Post)}
    ok &= check("every column the README lists exists", listed <= actual)
    ok &= check("every column that exists is listed", actual <= listed)
    # The engine table's pyppeteer row names the build it was refused on.
    ok &= check("the pyppeteer limitation names the Chromium build",
                "117.0.5938.0" in readme)

    # The two paid paths were unverified in the first version of this repo
    # and are now measured. Both directions are pinned: a docstring that
    # still says UNMEASURED after the numbers exist is stale, and numbers
    # written without a run are the thing §13 forbids. If a future change
    # removes the ability to measure them, the label comes back and this
    # check is what says so.
    api = open(os.path.join(REPO_ROOT, "scraper_api_client.py"),
               encoding="utf-8").read()
    ok &= check("the Scraper API path carries its measurements",
                "MEASURED 2026-09-16" in api and "2,305,814" in api)
    ok &= check("...and no longer claims to be unmeasured",
                "UNMEASURED" not in api)
    ok &= check("the README records what the paid paths bought",
                "Scraper API" in readme and "Scraping Browser" in readme
                and "$0.0005" in readme)
    # The author-page figure is the one that changes what a reader would buy,
    # so it is pinned by number rather than by the word.
    ok &= check("...including the author-page figure",
                "55 rows" in readme and "10" in readme)
    return ok


# The floor the README and CHANGELOG state. A FLOOR rather than the exact
# count, because an exact count goes stale the next time anyone adds a check
# and a stale number in a README is worse than no number (§17). Raise it when
# it is comfortably passed; it can only ever be an under-claim.
CLAIMED_CHECK_FLOOR = 650


def main() -> int:
    ok = True
    skips = []

    ok &= test_medium_encodings()
    ok &= test_values_on_real_fixtures()
    ok &= test_the_four_read_paths()
    ok &= test_a_post_page_holds_more_than_its_own_story()
    ok &= test_publication_pages_are_refused()
    ok &= test_urls()
    ok &= test_pagination()
    ok &= test_page_state()
    ok &= test_markers_do_not_match_a_good_page()
    ok &= test_a_challenge_is_never_paid_for_here()
    ok &= test_captcha_capability_claims_match_the_code()
    ok &= test_page_flow_policy()
    ok &= test_scroll_loop()
    ok &= test_throttle_is_not_completion()
    ok &= test_output_contract()
    ok &= test_writers_and_finish_run()
    ok &= test_diff()
    ok &= test_env_config()
    ok &= test_fingerprint_is_read_through_the_shared_helper()
    ok &= test_fingerprint_cache_is_key_aware()
    ok &= test_proxy_pool()
    ok &= test_credentials_never_reach_a_log()
    ok &= test_driver_primitives_tolerate_a_navigation()
    ok &= test_concurrency_machinery(skips)
    ok &= test_engine_parity(skips)
    ok &= test_module_attributes_exist(skips)
    ok &= test_no_dead_public_names()
    ok &= test_no_undefined_names()
    ok &= test_dockerfile_matches_its_entrypoint()
    ok &= test_no_file_describes_another_site()
    ok &= test_wording()
    ok &= test_no_capture_leaks()
    ok &= test_ci_checks_is_wired_up()
    ok &= test_sample_output()
    ok &= test_required_files_are_committed()
    ok &= test_readme_claims()

    passed = _total_checks - len(_failures)
    if passed < CLAIMED_CHECK_FLOOR:
        ok = False
        _failures.append(
            f"the README and CHANGELOG claim over {CLAIMED_CHECK_FLOOR} "
            f"checks and only {passed} ran — either checks were removed or "
            f"the claim needs lowering")

    print()
    if _failures:
        print("%d check(s) FAILED:" % len(_failures))
        for f in _failures:
            print("  - %s" % f)
    if skips:
        print("%d engine group(s) SKIPPED — an optional engine library is "
              "absent. CI's engine-smoke job installs each engine in its own "
              "venv and fails if this list is non-empty, because a skip reads "
              "exactly like a passing run:" % len(skips))
        for s in skips:
            print("  - %s" % s)
    print("smoke_test: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
