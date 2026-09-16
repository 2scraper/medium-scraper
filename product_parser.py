"""
product_parser.py
-----------------
Medium extraction. THIS FILE IS THE SITE — everything else in the repo is
family core with a handful of named constants (CLAUDE.md §1).

What Medium actually publishes, measured 2026-09-16
---------------------------------------------------
Medium serves the same catalogue through two entirely different renderers,
and which one answers depends on the URL rather than on anything negotiable:

    window.__APOLLO_STATE__     the modern site. Tag feeds, author profiles,
                                publication pages and post pages. 10-34
                                normalized nodes keyed `Post:{id}`, joined by
                                `__ref` pointers to `User` and `Collection`.

    window["obvInit"]({...})    the LEGACY renderer, and the only thing that
                                answers a tag's DAY archive. 128 posts in one
                                2.3 MB response, 84 fields each, with
                                `references.{Post,User,Collection}`,
                                `streamItems` for the order and
                                `archiveIndex` listing which years hold
                                stories. No Apollo state on that page at all.

Both are read here, richest first, and `data_source` on every row says which
one built it. A third path reads the tag feed's JSON-LD and a fourth reads
the rendered cards; both are fallbacks and both are measured below.

Four traps this file exists to avoid
------------------------------------
1.  **The canonical URL is not the Medium URL.** JSON-LD's `url` and `@id`
    are the canonical address, which for a cross-posted story points at
    another site entirely — 4 of 20 entries on one tag feed pointed at
    habr.com and dev.to. `Post.mediumUrl` in the modern state is right; in
    the LEGACY payload that same field is the empty string on 128 of 128
    posts, so the address is rebuilt from `uniqueSlug` and the author.

2.  **`virtuals.recommends` is not the clap count.** It is populated on 128
    of 128 archive posts and it is the retired pre-2017 recommend count: 25
    beside a `totalClapCount` of 248 on the same story. Reading it fills the
    column completely and wrongly, which no coverage check would catch.

3.  **A publication page carries ids and no data.** 167 `Post` nodes on
    betterprogramming.pub and 51 on a medium.com-hosted publication, every
    one of them `{__typename, id}` with no title, no author, no date. Rows
    built from those would be `sku` and nothing else while the run reported
    success. `_is_stub` drops them and `unsupported_reason` refuses the URL
    with that measurement rather than pretending the mode works.

4.  **`challenge-platform` appears on every page Medium serves.** Twice, on
    good pages and refused ones alike — it is Cloudflare's ordinary script
    injection, not a signal. Every marker below was counted on a page known
    to be good before it was allowed into the set (CLAUDE.md §18).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from output_writer import SOURCE_DEFAULT, Post

logger = logging.getLogger("product_parser")


# ===========================================================================
# Hosts
# ===========================================================================
# Medium serves one catalogue from three shapes of address:
#
#   medium.com / www.medium.com          the site itself
#   {username}.medium.com                an author's subdomain, which Medium
#                                        mints automatically — 30 of the 34
#                                        posts on one tag feed had an author
#                                        with `hasSubdomain: true`
#   a publication's own domain           betterprogramming.pub and several
#                                        thousand others
#
# The third cannot be enumerated, so it is not refused on the hostname. It is
# accepted and then CHECKED: a page that carries neither payload and none of
# Medium's asset hosts is not Medium, and `detect_page_state` says so.
MEDIUM_HOSTS = ("medium.com", "www.medium.com")

# Hosts that were Medium publications and are not any more. Refusing these by
# name, WITH the reason, is the §5 rule: "is not a Medium site" is false and
# sends the reader looking for a typo.
LEFT_MEDIUM: Dict[str, str] = {
    # Measured 2026-09-16: HTTP 200, 210 KB, 25 <article>, two JSON-LD blocks,
    # ZERO Apollo state and 7 `wp-content` references.
    "towardsdatascience.com": "moved off Medium and now runs on WordPress",
    "www.towardsdatascience.com": "moved off Medium and now runs on WordPress",
}


def site_host(url: str) -> Optional[str]:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return None
    return host or None


def is_medium_host(host: Optional[str]) -> bool:
    if not host:
        return False
    return host in MEDIUM_HOSTS or host.endswith(".medium.com")


def source_of(url: str) -> str:
    return site_host(url) or SOURCE_DEFAULT


# ===========================================================================
# URL shapes
# ===========================================================================
# Every Medium story URL ends in the post's id: twelve lowercase hex
# characters, after a hyphen, at the end of the last path segment. Checked
# against every story link in six captures — 100% carried one. It is also the
# `id` on every node of both payloads, which is why it and not the slug is
# `sku`: a story keeps its id when it is renamed, re-slugged or moved into a
# publication, and keeps none of its URLs.
_POST_ID = r"[0-9a-f]{12}"
_SKU_IN_URL_RE = re.compile(r"-(" + _POST_ID + r")(?:[/?#]|$)")
_SHORT_POST_RE = re.compile(r"^/p/(" + _POST_ID + r")(?:[/?#]|$)")

# `/tag/{slug}` and its three tabs, then the archive with its optional date.
# The slug can be any non-ASCII text — `/tag/日本語` is a live tag — so it is
# matched as "not a slash" rather than as a word.
_TAG_RE = re.compile(r"^/tag/(?P<slug>[^/?#]+)"
                     r"(?:/(?P<tab>recommended|archive))?"
                     r"(?:/(?P<year>\d{4})(?:/(?P<month>\d{2})(?:/(?P<day>\d{2}))?)?)?"
                     r"/?$")
_AUTHOR_RE = re.compile(r"^/@(?P<username>[^/?#]+)/?$")
_POST_UNDER_AUTHOR_RE = re.compile(r"^/@(?P<username>[^/?#]+)/(?P<slug>[^/?#]+)/?$")

# First path segments medium.com uses for its own routes, so a bare
# `/{something}` is only read as a publication when it is none of these.
RESERVED_FIRST_SEGMENTS = frozenset("""
    p m tag search about jobs help policy membership creators partner-program
    plans me new-story stats notifications settings following bookmarks lists
    topics feed sitemap robots.txt favicon.ico manifest.json osd.xml _ signin
    signup collections publications developers press blog careers legal
""".split())

# Query parameters Medium hangs off every internal link to describe where the
# click came from — `?source=tag_archive------python---3---...`. Seventy-odd
# characters of provenance that change per referrer, so two runs of the same
# listing would otherwise produce different `url` values for the same story.
TRACKING_PARAMS = frozenset("""
    source responsesOpen sortBy sk gi postPublishedType referrer ref
    utm_source utm_medium utm_campaign utm_content utm_term
""".split())


def strip_tracking(url: str) -> str:
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in TRACKING_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(kept), ""))


def normalize_url(url: str) -> str:
    """One spelling per story, so `seen` and `diff_runs.py` agree.

    Tracking stripped, fragment dropped, a trailing slash removed from a path
    that is not the root. The HOST is deliberately left alone: a story served
    from an author's subdomain and the same story on medium.com are the same
    story, but which address answered is what `source` records, and rewriting
    it here would erase that.
    """
    url = strip_tracking((url or "").strip())
    parts = urlsplit(url)
    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def post_id_from_url(url: str) -> Optional[str]:
    path = urlsplit(url or "").path
    short = _SHORT_POST_RE.match(path)
    if short:
        return short.group(1)
    found = _SKU_IN_URL_RE.search(path)
    return found.group(1) if found else None


def post_url_for(post_id: str) -> str:
    """The address every Medium post answers on, whatever it is slugged as.

    `medium.com/p/{id}` redirects to the story's own canonical spelling and
    works for a story on an author subdomain or a custom domain alike. Used
    where a payload gives an id and nothing to build a prettier URL from.
    """
    return "https://medium.com/p/%s" % post_id


# ===========================================================================
# Which view of the site a URL asks for
# ===========================================================================
def listing_kind(url: str) -> str:
    """Which mode this URL is, from its shape alone.

    `archive` is separated from `tag` because it is a genuinely different
    page: a different renderer, 128 rows instead of 20, and the only one of
    the four that a second page can be addressed for.
    """
    host = site_host(url)
    path = urlsplit(url or "").path or "/"

    if host and not is_medium_host(host) and host not in LEFT_MEDIUM:
        # A publication's custom domain. Its story URLs still carry the id.
        if post_id_from_url(url):
            return "post"
        return "publication"

    if _SHORT_POST_RE.match(path):
        return "post"

    tag = _TAG_RE.match(path)
    if tag:
        if tag.group("tab") == "archive" and tag.group("day"):
            return "archive"
        return "tag"

    if path.startswith("/search"):
        return "search"

    if _AUTHOR_RE.match(path):
        return "author"
    if _POST_UNDER_AUTHOR_RE.match(path):
        return "post" if post_id_from_url(url) else "author"

    if host and host.endswith(".medium.com") and host not in MEDIUM_HOSTS:
        # An author subdomain. `/` is the profile, `/{slug}-{id}` a story.
        return "post" if post_id_from_url(url) else "author"

    segments = [s for s in path.split("/") if s]
    if segments and segments[0] not in RESERVED_FIRST_SEGMENTS:
        if post_id_from_url(url):
            return "post"
        if len(segments) == 1:
            return "publication"
    return "unknown"


def category_from_url(url: str) -> Optional[str]:
    """A human label for what was asked for — the tag, the author, the date."""
    kind = listing_kind(url)
    path = urlsplit(url or "").path or "/"
    tag = _TAG_RE.match(path)
    if tag:
        slug = tag.group("slug")
        parts = [p for p in (tag.group("year"), tag.group("month"),
                             tag.group("day")) if p]
        return "%s %s" % (slug, "-".join(parts)) if parts else slug
    author = _AUTHOR_RE.match(path) or _POST_UNDER_AUTHOR_RE.match(path)
    if author:
        return "@" + author.group("username")
    if kind == "post":
        return post_id_from_url(url)
    host = site_host(url)
    if kind == "publication":
        segments = [s for s in path.split("/") if s]
        return segments[0] if segments else host
    return None


def unsupported_reason(url: str) -> Optional[str]:
    """Why this URL cannot be run, in the site's own terms, or None.

    Every refusal here names a MEASUREMENT. §5's rule: a wrong reason costs
    more than a missing one, because it sends the reader looking for a typo
    that is not there.
    """
    if not url or not url.strip():
        return "no URL given"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return "not an http(s) URL"
    host = site_host(url)
    if not host:
        return "no hostname in the URL"

    if host in LEFT_MEDIUM:
        return ("%s %s — its pages carry no Medium payload at all, so there "
                "is nothing here to read" % (host, LEFT_MEDIUM[host]))

    kind = listing_kind(url)
    if kind == "publication":
        return ("a Medium publication page carries post IDS and no post data "
                "— measured 167 `Post` nodes on betterprogramming.pub and 51 "
                "on a medium.com-hosted publication, every one of them "
                "`{__typename, id}` with no title, author or date. Rows built "
                "from that would hold a sku and nothing else while the run "
                "reported success. Use --mode archive on a tag the "
                "publication writes under, or --mode post on the story URLs")
    if kind == "search":
        return ("a Medium search page ships an EMPTY state — measured one key "
                "in `__APOLLO_STATE__` and zero posts — and fills itself over "
                "an XHR that this repo does not replay. Use --mode tag")
    if kind == "unknown":
        return ("not a Medium tag, author, archive or story URL. Expected one "
                "of /tag/{slug}, /tag/{slug}/archive/{yyyy}/{mm}/{dd}, "
                "/@{username}, or a story URL ending in a 12-hex post id")
    return None


def is_supported_host(url: str) -> bool:
    return unsupported_reason(url) is None


# ===========================================================================
# Pagination (§7)
# ===========================================================================
# Only the day archive is addressable, and the whole repo leans on it.
#
# Measured 2026-09-16 from one address: every one of the 11 POSTs to
# `medium.com/_/graphql` came back 403, so the infinite scroll that extends a
# tag feed, an author page or a publication page adds NOTHING. A tag feed
# scrolled twelve times to a stable height held exactly the 34 posts its
# first response shipped. What the page ships in its state is the run's data.
#
# `/tag/{slug}/archive/{yyyy}/{mm}/{dd}` is the exception: an independent
# address holding a whole day, `paging` empty, 128 posts in one fetch. A run
# walks it backwards a day at a time.
PAGINATES_BY_URL = True

PAGE_URL_REASON = (
    "only a tag's DAY archive has per-page addresses. A tag feed, an author "
    "page and a publication page extend over an XHR with no URL that names "
    "batch 2, so those modes are one fetch each"
)

# A day of a busy tag was 128 stories and 2.3 MB. Forty of those is 5 GB of
# HTML and a week and a half of catalogue, which is past the point where a
# run should be a run rather than a pipeline.
PAGE_CAP = 40


def paginates_by_url(url: str = "") -> bool:
    return listing_kind(url) == "archive"


def page_url(url: str, page: int) -> Optional[str]:
    """Page N of a day archive is the day N-1 days earlier.

    Returns None for every other mode rather than building something that
    would LOOK like it worked: a `?page=2` on a tag feed is ignored by Medium
    and answers with the first items again, so a run that trusted it would
    find no new sku, call the listing exhausted, and report a complete run
    holding one page (§18).
    """
    if page <= 1:
        return url
    if listing_kind(url) != "archive":
        return None
    parts = urlsplit(url)
    match = _TAG_RE.match(parts.path)
    if not match or not match.group("day"):
        return None
    try:
        day = datetime(int(match.group("year")), int(match.group("month")),
                       int(match.group("day")), tzinfo=timezone.utc)
    except ValueError:
        return None
    day = day.fromordinal(day.toordinal() - (page - 1))
    path = "/tag/%s/archive/%04d/%02d/%02d" % (match.group("slug"), day.year,
                                               day.month, day.day)
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


CONCURRENCY_REASON = (
    "a tag feed, an author page and a post page are each ONE fetch with no "
    "second address to hand a worker, so there is nothing to run in "
    "parallel. --mode archive walks independent day URLs and does support "
    "--concurrency"
)


def concurrency_limit(url: str = "") -> Optional[int]:
    """The highest --concurrency this URL can use, or None for no limit."""
    return None if listing_kind(url) == "archive" else 1


def concurrency_refusal(url: str) -> Optional[str]:
    return None if listing_kind(url) == "archive" else CONCURRENCY_REASON


# ===========================================================================
# Selectors — the DOM fallback only
# ===========================================================================
# Both payloads are read before any of this. These exist for the case where a
# page rendered its cards and shipped a state this parser could not read,
# which is the one failure mode a payload-only parser cannot report honestly.
#
# Anchored on the URL PATTERN and not on a class: Medium's class names are
# build hashes (`ag ah ai aj ak al`), and the 12-hex story id in the href is
# a contract with search engines.
SELECTORS: Dict[str, str] = {
    # Any link to a story. The id pattern is checked in Python, because a CSS
    # attribute selector cannot express "twelve hex characters".
    "item_link": "a[href]",
    # The modern card. The legacy archive renderer uses `.streamItem`
    # instead — 128 of them on one day — so both are here and the widening
    # walk in `_card_of` covers a page that uses neither.
    "item_card": "article, .streamItem",
    # A card's title and Medium's own preview line under it.
    "title": "h2, h3.graf--title, .graf--title",
    "subtitle": "h3, .graf--subtitle",
    # The author link on a card. `/@handle` with NO story id in it — the
    # story link also starts `/@handle`, and matching that one first gives an
    # author named after the story on every row while looking like it worked.
    "author_link": 'a[href^="/@"], a[href*="/@"]',
}

# How many story links mean "this page has rendered". Must be > 1: waiting
# for one match resolves on the site's own nav links long before a feed
# paints (§5).
MIN_CARD_MATCHES = 2

# Deliberately empty. Medium publishes no `link[rel=next]`, no numbered
# anchors and no next control in any mode — the archive walk builds its own
# addresses from the date and `page_url` above, which is §7's second layer.
NEXT_PAGE_SELECTOR = ""


def _story_links(soup: BeautifulSoup) -> List[Any]:
    out = []
    for a in soup.select(SELECTORS["item_link"]):
        href = a.get("href") or ""
        if post_id_from_url(href):
            out.append(a)
    return out


def count_cards(html: Optional[str]) -> int:
    """Distinct stories linked in the rendered document.

    DISTINCT, not links: a Medium card links its story twice (image and
    title), so counting links reports a two-card page as four and a readiness
    wait for four matches resolves on two (§4).
    """
    if not html:
        return 0
    soup = BeautifulSoup(html, "html.parser")
    return len({post_id_from_url(a.get("href") or "")
                for a in _story_links(soup)})


# ===========================================================================
# The two payloads
# ===========================================================================
_APOLLO_RE = re.compile(
    r"window\.__APOLLO_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S)
_OBVINIT_RE = re.compile(r'window\["obvInit"\]\((\{.*?\})\)\s*//\s*\]\]>', re.S)

# `window["obvInit"](...)` is a JAVASCRIPT object literal, not JSON, and the
# difference bites on exactly one construct: Medium escapes `>` as `\x3e` so
# that a `</script>` inside a string cannot close the script tag it is in.
# `\xHH` is legal JavaScript and is NOT legal JSON, so `json.loads` rejects
# the whole 970 KB payload over one character.
#
# Found by running four consecutive archive days rather than one: three
# parsed and the fourth — a story whose Thai image alt-text contained
# `text -\x3e input_ids` — did not. The DOM fallback caught it and returned
# 120 rows with titles and authors and NOTHING else, which is the shape of
# this failure: not a crash, not an empty run, just a whole page quietly
# demoted to the thinnest of the four read paths (§15).
#
# Rewritten to the JSON spelling of the same character rather than decoded,
# so string contents are untouched. The lookbehind counts backslashes: an
# even number means the `\x` is itself escaped and must be left alone.
_JS_HEX_ESCAPE = re.compile(r"(?<!\\)((?:\\\\)*)\\x([0-9a-fA-F]{2})")


def _js_to_json(text: str) -> str:
    return _JS_HEX_ESCAPE.sub(lambda m: "%s\\u00%s" % (m.group(1), m.group(2)),
                              text)


def apollo_state(html: Optional[str]) -> Dict[str, Any]:
    """The modern page state, or an empty dict.

    Returns {} rather than raising on malformed JSON: a truncated state is a
    page that arrived half-written, and the DOM path can still read it. The
    warning is logged so it is not silent.
    """
    if not html:
        return {}
    match = _APOLLO_RE.search(html)
    if not match:
        return {}
    try:
        state = json.loads(match.group(1))
    except ValueError as exc:
        logger.warning("__APOLLO_STATE__ present but unparseable: %s", exc)
        return {}
    return state if isinstance(state, dict) else {}


def obvinit_payload(html: Optional[str]) -> Dict[str, Any]:
    """The legacy archive payload, or an empty dict."""
    if not html:
        return {}
    match = _OBVINIT_RE.search(html)
    if not match:
        return {}
    try:
        data = json.loads(_js_to_json(match.group(1)))
    except ValueError as exc:
        logger.warning('window["obvInit"] present but unparseable: %s', exc)
        return {}
    return data if isinstance(data, dict) else {}


def jsonld_blocks(html: Optional[str]) -> List[Any]:
    if not html:
        return []
    out = []
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select('script[type="application/ld+json"]'):
        text = node.string or node.get_text() or ""
        try:
            out.append(json.loads(text))
        except ValueError:
            continue
    return out


def archive_years(html: Optional[str]) -> List[str]:
    """Which years this tag has stories in, from the site's own index.

    `archiveIndex.yearlyBuckets` on a day-archive page. The site publishing
    its own index is why an archive walk can be planned rather than guessed
    (§7) — though note the walk still stops on data, because a year having
    stories says nothing about any particular day in it.
    """
    index = obvinit_payload(html).get("archiveIndex") or {}
    buckets = index.get("yearlyBuckets") or []
    return [b.get("year") for b in buckets
            if isinstance(b, dict) and b.get("hasStories") and b.get("year")]


# ---------------------------------------------------------------------------
# Small conversions
# ---------------------------------------------------------------------------
def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _epoch_ms_to_iso(value: Any) -> Optional[str]:
    """Medium's timestamps, both spellings.

    The modern state ships an int; the legacy payload ships the SAME number
    as a string ('1789022678474'). A converter that only accepted the int
    left every archive row's `published_at` null while the feed rows looked
    fine — the difference is invisible until both are in one output.
    """
    number = _as_int(value)
    if not number or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number / 1000.0,
                                      tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _clean(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip()
    return text or None


def _image_url(image_id: Any) -> Optional[str]:
    """Medium's CDN address for a preview image id.

    Both payloads give an id (`1*hU4EhSyfi2pFclipM9YeSg.png`) rather than a
    URL. This is the address the site's own <img> tags resolve them to.
    """
    if not isinstance(image_id, str) or not image_id.strip():
        return None
    return "https://miro.medium.com/v2/resize:fit:1200/" + image_id.strip()


# ===========================================================================
# Rows from the modern state
# ===========================================================================
def _deref(state: Dict[str, Any], node: Any) -> Any:
    """Follow one Apollo `__ref` pointer."""
    if isinstance(node, dict) and "__ref" in node:
        return state.get(node["__ref"]) or {}
    return node if isinstance(node, dict) else {}


def _is_stub(node: Dict[str, Any]) -> bool:
    """A reference with no data behind it.

    A publication page's state is 167 of these — `{__typename, id}` and
    nothing else. Building rows from them produces a sku and 26 nulls while
    the run reports success, which is this codebase's most common historical
    bug class arriving as data rather than as code.
    """
    return not any(k in node for k in ("title", "mediumUrl", "creator",
                                       "firstPublishedAt", "clapCount"))


def _author_from_apollo(state: Dict[str, Any],
                        node: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    user = _deref(state, node.get("creator"))
    if not user:
        return None, None, None
    username = _clean(user.get("username"))
    name = _clean(user.get("name"))
    url = None
    domain = ((user.get("customDomainState") or {}).get("live") or {}).get("domain")
    if isinstance(domain, str) and domain.strip():
        url = "https://" + domain.strip()
    elif username:
        url = "https://medium.com/@" + username
    return name, username, url


def _publication_from_apollo(state: Dict[str, Any],
                             node: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    coll = _deref(state, node.get("collection"))
    if not coll:
        return None, None
    name = _clean(coll.get("name"))
    slug = _clean(coll.get("slug"))
    domain = _clean(coll.get("domain"))
    if domain:
        return name, "https://" + domain
    if slug:
        return name, "https://medium.com/" + slug
    return name, None


def posts_from_apollo(html: Optional[str], base_url: str = "") -> Dict[str, dict]:
    """Every story the modern state describes, keyed by post id."""
    state = apollo_state(html)
    if not state:
        return {}
    rows: Dict[str, dict] = {}
    for key, node in state.items():
        if not isinstance(node, dict) or node.get("__typename") != "Post":
            continue
        post_id = _clean(node.get("id")) or (key.split(":", 1)[1]
                                             if ":" in key else None)
        if not post_id or _is_stub(node):
            continue
        name, username, author_url = _author_from_apollo(state, node)
        pub_name, pub_url = _publication_from_apollo(state, node)
        preview = node.get("extendedPreviewContent") or {}
        url = _clean(node.get("mediumUrl")) or post_url_for(post_id)
        tags = []
        for ref in node.get("tags") or []:
            tag = _deref(state, ref)
            slug = _clean(tag.get("normalizedTagSlug")) or _clean(tag.get("id"))
            if slug:
                tags.append(slug)
        canonical = _clean(node.get("canonicalUrl"))
        rows[post_id] = {
            "sku": post_id,
            "url": normalize_url(url),
            "title": _clean(node.get("title")),
            "subtitle": _clean(preview.get("subtitle")),
            "canonical_url": canonical if canonical and canonical != url else None,
            "author": name,
            "author_username": username,
            "author_url": author_url,
            "publication": pub_name,
            "publication_url": pub_url,
            "published_at": _epoch_ms_to_iso(node.get("firstPublishedAt")),
            "updated_at": _epoch_ms_to_iso(node.get("latestPublishedAt")),
            "claps": _as_int(node.get("clapCount")),
            "responses": _as_int((node.get("postResponses") or {}).get("count")),
            "reading_time_min": _as_float(node.get("readingTime")),
            "word_count": _as_int(node.get("wordCount")),
            "is_paywalled": node.get("isLocked") if isinstance(node.get("isLocked"), bool) else None,
            "is_series": node.get("isSeries") if isinstance(node.get("isSeries"), bool) else None,
            "language": _clean(node.get("detectedLanguage")),
            "tags": tags or None,
            "preview_image_url": _image_url((node.get("previewImage") or {}).get("id")),
            "content": _content_from_apollo(node),
            "data_source": "apollo",
        }
    return rows


_PARAGRAPH_JOIN = "\n\n"


def _content_from_apollo(node: Dict[str, Any]) -> Optional[str]:
    """A story's body, where the page is a post page and carries one.

    Medium ships the body as `Paragraph` nodes under a `content(...)` key
    whose name embeds its own arguments — `content({"postMeteringOptions":…})`
    — so it is found by prefix rather than by an exact key. A paywalled story
    yields only the free preview; `is_paywalled` on the same row says so.
    """
    body = None
    for key, value in node.items():
        if key == "content" or key.startswith("content("):
            body = value
            break
    if not isinstance(body, dict):
        return None
    paragraphs = (body.get("bodyModel") or {}).get("paragraphs")
    if not isinstance(paragraphs, list):
        return None
    texts = [_clean(p.get("text")) for p in paragraphs if isinstance(p, dict)]
    joined = _PARAGRAPH_JOIN.join(t for t in texts if t)
    return joined or None


def resolve_apollo_paragraphs(html: Optional[str]) -> Optional[str]:
    """The body of a post page, joined from the state's Paragraph nodes.

    Kept separate from `posts_from_apollo` because Medium normalizes the
    paragraphs OUT of the post node and into their own `Paragraph:{id}`
    entries — 70 of them on one story — so the body has to be reassembled
    from the store rather than read off the post.
    """
    state = apollo_state(html)
    if not state:
        return None
    post_key = next((k for k, v in state.items()
                     if isinstance(v, dict) and v.get("__typename") == "Post"
                     and not _is_stub(v)), None)
    if not post_key:
        return None
    node = state[post_key]
    body = None
    for key, value in node.items():
        if key == "content" or key.startswith("content("):
            body = value
            break
    if not isinstance(body, dict):
        return None
    model = _deref(state, body.get("bodyModel"))
    refs = model.get("paragraphs") if isinstance(model, dict) else None
    if not isinstance(refs, list):
        return None
    texts = []
    for ref in refs:
        para = _deref(state, ref)
        text = _clean(para.get("text"))
        if text:
            texts.append(text)
    joined = _PARAGRAPH_JOIN.join(texts)
    return joined or None


# ===========================================================================
# Rows from the legacy archive payload
# ===========================================================================
def posts_from_obvinit(html: Optional[str], base_url: str = "") -> Dict[str, dict]:
    """Every story the legacy day-archive payload describes, keyed by id.

    Richest of the four paths: 84 fields per post, and the only one carrying
    reading time, word count and language on a listing row.
    """
    data = obvinit_payload(html)
    refs = data.get("references") or {}
    posts = refs.get("Post") or {}
    users = refs.get("User") or {}
    colls = refs.get("Collection") or {}
    if not isinstance(posts, dict):
        return {}

    # `streamItems` is the ORDER Medium rendered them in. Without it the rows
    # come out in dict order, which is neither the site's order nor stable.
    order: List[str] = []
    for item in data.get("streamItems") or []:
        if isinstance(item, dict):
            post_id = (item.get("postPreview") or {}).get("postId")
            if post_id and post_id not in order:
                order.append(post_id)
    for post_id in posts:
        if post_id not in order:
            order.append(post_id)

    rows: Dict[str, dict] = {}
    for post_id in order:
        node = posts.get(post_id)
        if not isinstance(node, dict):
            continue
        virtuals = node.get("virtuals") or {}
        user = users.get(node.get("creatorId")) or {}
        coll = colls.get(node.get("homeCollectionId") or "") or {}
        username = _clean(user.get("username"))
        slug = _clean(node.get("uniqueSlug")) or post_id

        # `mediumUrl` is the empty string on 128 of 128 posts here, so the
        # address is rebuilt. An author with a subdomain serves the story
        # from it; everyone else from medium.com/@handle. A story published
        # INTO a publication is served from the publication's address, which
        # is why the collection is checked first.
        coll_domain = _clean(coll.get("domain"))
        if coll_domain:
            url = "https://%s/%s" % (coll_domain, slug)
        elif coll.get("slug"):
            url = "https://medium.com/%s/%s" % (_clean(coll.get("slug")), slug)
        elif username:
            url = "https://medium.com/@%s/%s" % (username, slug)
        else:
            url = post_url_for(post_id)

        author_url = ("https://medium.com/@" + username) if username else None
        pub_url = None
        if coll_domain:
            pub_url = "https://" + coll_domain
        elif coll.get("slug"):
            pub_url = "https://medium.com/" + _clean(coll.get("slug"))

        tags = [_clean(t.get("slug")) for t in (virtuals.get("tags") or [])
                if isinstance(t, dict) and t.get("slug")]
        canonical = _clean(node.get("canonicalUrl")) or _clean(node.get("importedUrl"))
        locked = node.get("isSubscriptionLocked")

        rows[post_id] = {
            "sku": post_id,
            "url": normalize_url(url),
            "title": _clean(node.get("title")),
            "subtitle": _clean(virtuals.get("subtitle")),
            "canonical_url": canonical if canonical and canonical != url else None,
            "author": _clean(user.get("name")),
            "author_username": username,
            "author_url": author_url,
            "publication": _clean(coll.get("name")),
            "publication_url": pub_url,
            "published_at": _epoch_ms_to_iso(node.get("firstPublishedAt")),
            "updated_at": _epoch_ms_to_iso(node.get("latestPublishedAt")),
            # `totalClapCount`, NOT the `recommends` beside it — see the
            # module docstring. 248 against 25 on the same story.
            "claps": _as_int(virtuals.get("totalClapCount")),
            "responses": _as_int(virtuals.get("responsesCreatedCount")),
            "reading_time_min": _as_float(virtuals.get("readingTime")),
            "word_count": _as_int(virtuals.get("wordCount")),
            "is_paywalled": locked if isinstance(locked, bool) else None,
            "is_series": node.get("isSeries") if isinstance(node.get("isSeries"), bool) else None,
            "language": _clean(node.get("detectedLanguage")),
            "tags": tags or None,
            "preview_image_url": _image_url((virtuals.get("previewImage") or {}).get("imageId")),
            "content": None,
            "data_source": "obvinit",
        }
    return rows


# ===========================================================================
# Rows from JSON-LD
# ===========================================================================
def _jsonld_entities(blocks: Iterable[Any]) -> List[dict]:
    out: List[dict] = []
    for block in blocks:
        items = block if isinstance(block, list) else [block]
        for item in items:
            if not isinstance(item, dict):
                continue
            # `@graph` is legal schema.org and carries the products on some
            # sites while `itemListElement` carries them on others; reading
            # only one reports an empty category for an unread format (§4).
            for key in ("mainEntity", "itemListElement", "@graph"):
                value = item.get(key)
                if isinstance(value, list):
                    out.extend(e for e in value if isinstance(e, dict))
            if item.get("@type") in ("SocialMediaPosting", "Article",
                                     "BlogPosting", "NewsArticle"):
                out.append(item)
    return out


def _jsonld_image(value: Any) -> Optional[str]:
    """All four legal shapes of schema.org `image` (§4)."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        return value.get("url") or value.get("contentUrl") or None
    if isinstance(value, list):
        for item in value:
            found = _jsonld_image(item)
            if found:
                return found
    return None


def posts_from_jsonld(html: Optional[str], base_url: str = "") -> Dict[str, dict]:
    """Stories from the tag feed's structured data.

    A fallback, and it carries the trap this whole file is careful about:
    `url` and `@id` here are the CANONICAL address, external for a
    cross-posted story. The Medium address is rebuilt from `identifier`,
    which is the post id.
    """
    rows: Dict[str, dict] = {}
    for entity in _jsonld_entities(jsonld_blocks(html)):
        post_id = _clean(entity.get("identifier")) or post_id_from_url(
            entity.get("url") or entity.get("@id") or "")
        if not post_id:
            continue
        author = entity.get("author") or entity.get("creator") or {}
        if not isinstance(author, dict):
            author = {}
        canonical = _clean(entity.get("url")) or _clean(entity.get("@id"))
        free = entity.get("isAccessibleForFree")
        rows[post_id] = {
            "sku": post_id,
            "url": post_url_for(post_id),
            "title": _clean(entity.get("headline")) or _clean(entity.get("name")),
            "subtitle": _clean(entity.get("description")),
            "canonical_url": canonical,
            "author": _clean(author.get("name")),
            "author_username": _clean(author.get("identifier")),
            "author_url": _clean(author.get("url")),
            "publication": None,
            "publication_url": None,
            "published_at": _clean(entity.get("datePublished")),
            "updated_at": _clean(entity.get("dateModified")),
            "claps": None,
            "responses": None,
            "reading_time_min": None,
            "word_count": None,
            "is_paywalled": (not free) if isinstance(free, bool) else None,
            "is_series": None,
            "language": None,
            "tags": None,
            "preview_image_url": _jsonld_image(entity.get("image")),
            "content": None,
            "data_source": "jsonld",
        }
    return rows


# ===========================================================================
# Rows from the rendered cards
# ===========================================================================
_WIDEN_CAP = 8


def _card_of(anchor: Any) -> Any:
    """The smallest ancestor covering exactly ONE story.

    Widening stops when a second DISTINCT story id comes into scope, not when
    a second LINK does: a Medium card links its story twice, so a walk that
    stopped at "more than one story link" would never leave the anchor and
    every row would come back with nothing but a url. Capped so a malformed
    document cannot walk to <body> and hand one card its neighbours' values
    (§4).
    """
    node = anchor
    best = anchor
    for _ in range(_WIDEN_CAP):
        parent = getattr(node, "parent", None)
        if parent is None or getattr(parent, "name", None) in (None, "body",
                                                               "html",
                                                               "[document]"):
            break
        ids = {post_id_from_url(a.get("href") or "")
               for a in parent.select("a[href]")}
        ids.discard(None)
        if len(ids) > 1:
            break
        best = parent
        node = parent
    return best


def _text(node: Any) -> Optional[str]:
    if node is None:
        return None
    return _clean(node.get_text(" ", strip=True))


def _in_card(anchor: Any) -> bool:
    """Whether this link sits inside one of the site's story cards.

    The guard that stops the DOM path inventing rows out of page chrome. A
    publication's "About" tab is itself a Medium post and its nav link
    carries a 12-hex id, so an id-shaped href alone is NOT evidence of a
    story — reading one produced a row titled "About" pointing at
    `medium.com/p/…` with every other column null (§4's junk-link case).
    """
    node = getattr(anchor, "parent", None)
    for _ in range(_WIDEN_CAP + 4):
        if node is None or getattr(node, "name", None) in (None, "[document]"):
            return False
        if node.name == "article":
            return True
        classes = node.get("class") or []
        if any(c in ("streamItem", "postArticle") for c in classes):
            return True
        node = getattr(node, "parent", None)
    return False


def posts_from_dom(html: Optional[str], base_url: str = "") -> Dict[str, dict]:
    """Stories from the rendered cards. The last path, and the thinnest.

    Deliberately reads only what a card actually prints. It does NOT guess at
    claps or dates from card text: Medium renders those inconsistently
    between the two renderers, and a wrong number in a populated column is
    worse than a null (§8).

    Only links INSIDE a card are read. A page whose cards have not painted
    contributes nothing here rather than contributing its navigation — which
    is the honest answer, and `detect_page_state` calls such a page `shell`.
    """
    if not html:
        return {}
    soup = BeautifulSoup(html, "html.parser")
    rows: Dict[str, dict] = {}
    for anchor in _story_links(soup):
        href = anchor.get("href") or ""
        post_id = post_id_from_url(href)
        if not post_id or post_id in rows:
            continue
        if not _in_card(anchor):
            continue
        url = href if href.startswith("http") else _absolute(base_url, href)
        card = _card_of(anchor)
        title = None
        for selector in ("h2", "h3.graf--title", ".graf--title"):
            title = _text(card.select_one(selector))
            if title:
                break
        title = title or _clean(anchor.get("aria-label")) or _text(anchor)
        subtitle = _text(card.select_one("h3"))
        if subtitle == title:
            subtitle = None
        author = author_username = author_url = None
        for link in card.select('a[href*="/@"]'):
            link_href = link.get("href") or ""
            if post_id_from_url(link_href):
                continue          # the story link, which also starts /@handle
            handle = re.search(r"/@([^/?#]+)", link_href)
            text = _text(link)
            if handle and text:
                author, author_username = text, handle.group(1)
                author_url = "https://medium.com/@" + handle.group(1)
                break
        rows[post_id] = {
            "sku": post_id,
            "url": normalize_url(url),
            "title": title,
            "subtitle": subtitle,
            "canonical_url": None,
            "author": author,
            "author_username": author_username,
            "author_url": author_url,
            "publication": None,
            "publication_url": None,
            "published_at": None,
            "updated_at": None,
            "claps": None,
            "responses": None,
            "reading_time_min": None,
            "word_count": None,
            "is_paywalled": None,
            "is_series": None,
            "language": None,
            "tags": None,
            "preview_image_url": None,
            "content": None,
            "data_source": "dom",
        }
    return rows


def _absolute(base_url: str, href: str) -> str:
    if href.startswith("http"):
        return href
    parts = urlsplit(base_url or "https://medium.com")
    root = "%s://%s" % (parts.scheme or "https", parts.netloc or "medium.com")
    return root + (href if href.startswith("/") else "/" + href)


# ===========================================================================
# Page state (§8, §17, §18)
# ===========================================================================
# Every marker below was COUNTED on a page known to be good before it was
# allowed in. `challenge-platform` is the one that did not make it: it
# appears exactly twice on every page Medium serves, refused and served
# alike, and adding it would have made every successful run report a
# challenge.
CHALLENGE_MARKERS: Tuple[str, ...] = (
    "just a moment",      # 1 on the challenge page, 0 on all six good ones
    "cf_chl_opt",         # 7 / 0
    "_cf_chl",            # 10 / 0
)

# Vendor challenges, for naming one where it names itself. Medium is fronted
# by Cloudflare and nothing else appeared in thirteen captures, so this set is
# short by measurement rather than by omission.
#
# `g-recaptcha` and `recaptcha` are DELIBERATELY ABSENT, and this is the
# reason: Medium ships reCAPTCHA markup for its own sign-in widget on every
# page it serves — counted 3, 0, 4 and 3 occurrences of `g-recaptcha` and 15,
# 4, 16 and 17 of `recaptcha` on four pages known to be good, against 0 on
# the challenge page and 0 on the WAF block. The family's usual reCAPTCHA
# marker is a false positive on this site, and including it made a perfectly
# good 364 KB tag feed report "challenge: recaptcha". A marker that matches
# every page is worse than no marker (§18); both entries below were counted
# at 0 on all four good pages before being allowed in.
BOT_CHALLENGE_MARKERS: Tuple[str, ...] = (
    "cf-turnstile",                 # 0 on good pages, 1 on the challenge
    "challenges.cloudflare.com",    # 0 on good pages, 6 on the challenge
)

# Cloudflare's WAF refusal, which is a different page from the challenge: no
# turnstile, nothing to solve, 5 KB. `curl` and every headless browser got
# this one; a headful browser from the same address got HTTP 200.
BLOCK_MARKERS: Tuple[str, ...] = (
    "cf-error-details",
    "you have been blocked",
    "attention required!",
)

# Medium's own asset hosts. A served page is built out of them; an
# interstitial, a WordPress site that used to be a publication, and
# Chromium's own network-error page are not (§8, §18).
#
# All three hosts are needed, not one: the legacy archive renderer ships ZERO
# `cdn-client.medium.com` references, so anchoring on that alone would report
# every 128-story archive page as blocked.
#
#   counted on six good pages:   4-65 cdn-client, 2-125 miro, 1-5 glyph
#   counted on the challenge page and the WAF block page:   0, 0, 0
#   counted on towardsdatascience.com (WordPress now):      0, 0, 0
_ASSET_MARKER = re.compile(
    r"cdn-client\.medium\.com|miro\.medium\.com|glyph\.medium\.com")
_ASSET_MIN_MATCHES = 3

# The 2Captcha Scraping Browser API ships an auto-solve extension that
# injects its own turnstile hunter into every page it loads, so `cf-turnstile`
# appears in the markup of a perfectly good listing page fetched over
# --cdp-endpoint. This repo's marker set CAN match one, which is the
# condition CLAUDE.md §8 sets for adding the filter — the siblings whose sets
# cannot were checked and deliberately left alone.
_EXTENSION_SCRIPT_RE = re.compile(
    r"<script[^>]+src=[\"'](?:chrome|moz)-extension://[^\"']*[\"'][^>]*>"
    r"(?:</script>)?", re.I)


def strip_extension_scripts(html: Optional[str]) -> str:
    return _EXTENSION_SCRIPT_RE.sub("", html or "")


def served_by_medium(html: Optional[str]) -> bool:
    if not html:
        return False
    return len(_ASSET_MARKER.findall(html)) >= _ASSET_MIN_MATCHES


def is_challenge_page(html: Optional[str]) -> bool:
    lowered = strip_extension_scripts(html).lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


def is_block_page(html: Optional[str]) -> bool:
    lowered = (html or "").lower()
    return any(marker in lowered for marker in BLOCK_MARKERS)


def detect_bot_challenge(html: Optional[str], url: str = "") -> Optional[str]:
    """Which vendor's challenge is rendered here, or None.

    Only ever REFINES a verdict the page-state policy has already reached: it
    is not consulted for a page that classified as `content` or `empty`,
    because a detection on a page whose stories are already parsed guards
    nothing, and one on a correct empty answer is simply wrong (§18).
    """
    lowered = strip_extension_scripts(html).lower()
    if any(marker in lowered for marker in BOT_CHALLENGE_MARKERS):
        return "cloudflare"
    if is_challenge_page(html):
        return "cloudflare"
    return None


def detect_block_marker(html: Optional[str]) -> Optional[str]:
    if is_block_page(html):
        return "cloudflare-waf"
    if is_challenge_page(html):
        return "cloudflare"
    if html and not served_by_medium(html):
        return "not-served-by-medium"
    return None


# Medium's own empty answers, in the copy it actually prints. A POSITIVE
# signal only: a feed with no stories and none of this copy is a page still
# painting, and the two want opposite responses (§18).
NO_RESULTS_MARKERS: Tuple[str, ...] = (
    "no stories",
    "there are no stories",
    "out of nothing, something",   # Medium's own 404 page copy
    "page not found",
    "we couldn't find that page",
)


def is_no_results(html: Optional[str]) -> bool:
    lowered = (html or "").lower()
    return any(marker in lowered for marker in NO_RESULTS_MARKERS)


def detect_page_state(html: Optional[str], status: Optional[int] = None,
                      url: str = "") -> str:
    """Which of five states this response is.

    Ordered by how much each signal PROVES, not by what is cheap to check
    (§17). `status` is positional and second, matching `page_flow.classify`;
    two engines in a sibling repo passed it as a keyword and both crashed on
    their first fetch, invisibly to every offline check.

    The five:

        content    stories are in one of the payloads or in the DOM
        empty      Medium's own "no stories" copy
        challenge  Cloudflare's interstitial — transient, and re-rolls in a
                   FRESH context rather than on a reload (measured: 9 of 27
                   first attempts, every one cleared next try)
        blocked    the WAF refusal, or a page that is not Medium's at all
        shell      Medium's, served, and nothing has painted or shipped yet
    """
    if html is None:
        return "blocked"

    # 1. Unambiguous positive: one of MEDIUM'S OWN payloads describes
    #    stories. Nothing else on the web ships `window["obvInit"]` or an
    #    Apollo store keyed `Post:{12-hex}`, so this proves both that the
    #    page is Medium's and that it has content.
    if posts_from_obvinit(html, url) or posts_from_apollo(html, url):
        return "content"

    # 1b. Rendered cards — but only on a page built out of Medium's assets.
    #     `<article>` elements and schema.org `Article` blocks are generic:
    #     towardsdatascience.com, a former publication now running WordPress,
    #     serves 25 of the first and 2 of the second, and without this gate
    #     classified as `content` and would have put WordPress rows into a
    #     Medium run.
    if served_by_medium(html) and count_cards(html) > 0:
        return "content"

    # 2. Medium's own empty answer. Before the status check and before any
    #    threshold, because it is a marker only the real page can carry.
    if is_no_results(html):
        return "empty"

    # 3. The WAF refusal — named, and distinct from the challenge because
    #    there is nothing on it to solve and paying for a solve would be
    #    money spent on a page that has no widget.
    if is_block_page(html):
        return "blocked"

    # 4. The interstitial. Before the status check because it says WHICH
    #    vendor, and before the asset threshold because a challenge page is
    #    not built out of Medium's assets and would read as a generic block.
    if is_challenge_page(html):
        return "challenge"

    # 5. A refusal with no page to read.
    if status is not None and (status in (401, 403, 429) or status >= 500):
        return "blocked"

    # 6. A vendor challenge rendered inside an otherwise ordinary page.
    if detect_bot_challenge(html):
        return "challenge"

    # 7. Not built out of Medium's assets: a browser error page, somebody
    #    else's interstitial, or a publication that left Medium. This is the
    #    one that catches Chromium's own network-error page, which carries
    #    the site's hostname in its title and passes any text marker.
    if not served_by_medium(html):
        return "blocked"

    # 8. Ours, served, not painted.
    return "shell"


def redirected_away(requested_url: str, final_url: str) -> Optional[str]:
    """Whether the site answered a DIFFERENT listing than the one asked for.

    A tag day with no stories does not 404: it redirects up to the month
    view, which is the modern renderer holding a different set of stories.
    HTTP 200 and a page full of rows, for a day that has none — measured on
    `/tag/日本語/archive/2026/09/10`, which landed on `/archive/2026/09`.
    Silently keeping those rows would attribute another period's stories to
    the requested day.
    """
    if not requested_url or not final_url:
        return None
    want = urlsplit(normalize_url(requested_url)).path
    got = urlsplit(normalize_url(final_url)).path
    if want == got:
        return None
    return "requested %s, the site answered %s" % (want, got)


# ===========================================================================
# Assembling rows
# ===========================================================================
# Which payload wins where two describe the same story. Richest first: the
# legacy payload has 84 fields, the modern one 21-56, JSON-LD 17 and the DOM
# whatever the card printed.
_MERGE_ORDER = ("obvinit", "apollo", "jsonld", "dom")

_EXTRACTORS = {
    "obvinit": posts_from_obvinit,
    "apollo": posts_from_apollo,
    "jsonld": posts_from_jsonld,
    "dom": posts_from_dom,
}


def _merge(values: Dict[str, dict]) -> Tuple[dict, str]:
    """One row out of however many views named this story.

    A later view only FILLS a null; it never overwrites a value an earlier,
    richer view already set. `data_source` records every view that
    contributed, joined with '+', so a diff between two runs that read the
    same story through different views reports `source_changed` rather than
    inventing a change (§8).
    """
    merged: dict = {}
    contributors: List[str] = []
    for name in _MERGE_ORDER:
        row = values.get(name)
        if not row:
            continue
        used = False
        for key, value in row.items():
            if key == "data_source":
                continue
            if value is None:
                continue
            if merged.get(key) is None:
                merged[key] = value
                used = True
        if used or not contributors:
            contributors.append(name)
    return merged, "+".join(contributors)


def parse_posts(html: str, url: str, page: int = 1,
                mode: str = "") -> List[Post]:
    """Every story this response describes, as rows, in the site's order."""
    mode = mode or listing_kind(url)
    source = source_of(url)

    views: Dict[str, Dict[str, dict]] = {}
    for name in _MERGE_ORDER:
        try:
            views[name] = _EXTRACTORS[name](html, url) or {}
        except Exception as exc:                      # pragma: no cover
            # One unreadable view must not cost the other three. Logged
            # rather than swallowed: a path that silently returns {} on error
            # is this codebase's most common historical bug class (§8).
            logger.warning("%s extraction failed on %s: %s", name, url, exc)
            views[name] = {}

    # Order: the richest view that actually produced rows sets it, because
    # that is the one that read the site's own ordering.
    ordered_ids: List[str] = []
    for name in _MERGE_ORDER:
        for post_id in views.get(name, {}):
            if post_id not in ordered_ids:
                ordered_ids.append(post_id)

    # In post mode a page describes ONE story plus whatever it recommends
    # beside it — five rows came back for a single-story URL before this. The
    # id in the URL is what was asked for, and the rest are the site's
    # suggestions rather than the run's result.
    if mode == "post":
        wanted = post_id_from_url(url)
        if wanted:
            if wanted in ordered_ids:
                ordered_ids = [wanted]
            else:
                # The page did not describe the story its own URL names.
                # Returning the recommendations instead would answer a
                # different question than the one asked.
                logger.warning("post %s is not in this page's payload — "
                               "returning no rows rather than its %d "
                               "recommendations", wanted, len(ordered_ids))
                ordered_ids = []

    # The body is reassembled from the state's Paragraph nodes, which Medium
    # normalizes out of the post node itself.
    body = resolve_apollo_paragraphs(html) if mode == "post" else None

    rows: List[Post] = []
    for position, post_id in enumerate(ordered_ids, start=1):
        values, provenance = _merge({name: views.get(name, {}).get(post_id)
                                     for name in _MERGE_ORDER})
        if not values.get("url"):
            continue
        if body and mode == "post" and not values.get("content"):
            values["content"] = body
        content = values.get("content")
        rows.append(Post(
            source=source,
            url=values.get("url") or "",
            sku=post_id,
            title=values.get("title"),
            subtitle=values.get("subtitle"),
            canonical_url=values.get("canonical_url"),
            author=values.get("author"),
            author_username=values.get("author_username"),
            author_url=values.get("author_url"),
            publication=values.get("publication"),
            publication_url=values.get("publication_url"),
            published_at=values.get("published_at"),
            updated_at=values.get("updated_at"),
            claps=values.get("claps"),
            responses=values.get("responses"),
            reading_time_min=values.get("reading_time_min"),
            word_count=values.get("word_count"),
            is_paywalled=values.get("is_paywalled"),
            is_series=values.get("is_series"),
            language=values.get("language"),
            tags=values.get("tags"),
            preview_image_url=values.get("preview_image_url"),
            content=content,
            content_chars=len(content) if content else None,
            data_source=provenance,
            page=page,
            position=position,
        ))
    _log_coverage(rows, url, mode)
    return rows


# A tag feed's payload carries 21 fields per post and no reading time at all,
# so warning about a null one there would be warning about the site. These
# are the modes whose payload DOES carry it, and where a null means something
# went wrong.
READING_TIME_EXPECTED_MODES = ("archive", "author", "post")
COVERAGE_WARN = 0.90


def _log_coverage(rows: Sequence[Post], url: str, mode: str) -> None:
    if not rows:
        return
    total = len(rows)
    titled = sum(1 for r in rows if r.title)
    if titled / total < COVERAGE_WARN:
        logger.warning("only %d of %d rows carry a title on %s — the page may "
                       "have been read through the DOM fallback alone",
                       titled, total, url)
    if mode in READING_TIME_EXPECTED_MODES:
        timed = sum(1 for r in rows if r.reading_time_min is not None)
        if timed / total < COVERAGE_WARN:
            logger.warning("only %d of %d rows carry a reading time on %s, "
                           "where this mode's payload normally does",
                           timed, total, url)
    sources = sorted({r.data_source for r in rows if r.data_source})
    logger.info("%d rows from %s via %s", total, url, ", ".join(sources))
