"""Cut the offline suite's fixtures out of real captures, and PROVE they
parse the same.

Its output is `fixtures_generated.json`, which `smoke_test.py` loads. This
script is shipped because two files point at it — `smoke_test.py`'s own
docstring and TROUBLESHOOTING.md — and an instruction pointing at a file that
does not exist is worse than no instruction.

WHAT YOU NEED TO RUN IT
-----------------------
Your own captures, in `../captures/` relative to the repo, named as `SOURCES`
below expects. They are deliberately NOT in the repository: one capture of a
day archive is 2.3 MB.

Take them with a real browser — `--dump-html` on any engine writes exactly
the bytes the parser was given, and on this site a real WINDOW is required
(a headless fetch gets a 5 KB block page). Take a DAY ARCHIVE as well as a
tag feed: the archive is served by Medium's legacy renderer and carries
`window["obvInit"]`, the tag feed by the modern one and carries
`__APOLLO_STATE__`, and those are the repo's two read paths.

WHAT IT ENFORCES, and why each rule is here
-------------------------------------------
  * every fixture is CUT from a real capture, never hand-written. The one
    thing in a sibling repo that WAS hand-written — a guess at the site's
    "nothing matched" copy — matched none of the real strings, and an empty
    result came back as `shell` and spent a 25-second readiness wait on an
    answer the site had already given;
  * each one is verified to parse IDENTICALLY to the untrimmed original for
    the stories it keeps — every column, not just a count;
  * the trimmed fixture must still CLASSIFY the same way, which is what
    catches a trim that dropped the site's own asset references and turned a
    good page into a `blocked` one.

WHAT IS NOT VERBATIM, and why
-----------------------------
Three things are rewritten before anything is written to disk, and every one
of them is a PERSON rather than the site:

    the author's display name    -> "Fixture Author N"
    the author's @handle         -> "fixture-author-N", consistently, so
                                    every URL built from it still lines up
    the story's subtitle and
    its body paragraphs          -> filler of the SAME LENGTH

Medium stories are bylined public writing, but republishing a named person's
prose in a scraper's test corpus is a separate act from the site showing it
on its own page (§10, where a sibling repo committed a real customer's
review, name and photo ids). The STRUCTURE is what the checks need — the
payload's keys, the URL shapes, the ids, the clap and response counts, the
reading times, the tags, the timestamps, the paywall flags — and all of that
survives untouched.

Story TITLES are kept. They are headlines rather than prose, they are what
the value assertions are about, and the slug Medium builds from them is half
of every story URL — scrubbing them would leave the URL checks testing
nothing.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict

import product_parser as P

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
CAPTURES = os.path.abspath(os.path.join(REPO_ROOT, "..", "captures"))
OUT_PATH = os.path.join(REPO_ROOT, "fixtures_generated.json")

# name -> (capture file, the URL it was taken from, how many stories to keep)
# `None` for the count means "keep the page as it is": the three refusal
# pages carry no stories and no person, so there is nothing to trim or scrub.
SOURCES = {
    "tag_feed":     ("tag_feed.html",
                     "https://medium.com/tag/python", 6),
    "archive_day":  ("arch_day.html",
                     "https://medium.com/tag/python/archive/2026/09/10", 6),
    "author":       ("author_profile.html",
                     "https://medium.com/@quincylarson", 4),
    "post":         ("post_free.html",
                     "https://medium.com/p/d373fe2c96b7", 1),
    "tag_nonlatin": ("tag_nonlatin.html",
                     "https://medium.com/tag/%E6%97%A5%E6%9C%AC%E8%AA%9E", 4),
    "challenge":    ("challenge_cloudflare.html",
                     "https://medium.com/@quincylarson", None),
    "blocked_waf":  ("blocked_waf403.html",
                     "https://medium.com/tag/python", None),
    "not_medium":   ("pub_customdomain.html",
                     "https://towardsdatascience.com/", None),
}

# Kept verbatim in every fixture, because `served_by_medium` needs at least
# three of them and a trim that dropped them would turn a good page into a
# `blocked` one — which is the exact regression this pipeline exists to
# catch.
_CF_SCRIPT_RE = re.compile(
    r"<script>window\.__CF\$cv\$params=.*?</script>", re.S)

ASSET_HEAD = (
    '<link rel="stylesheet" href="https://cdn-client.medium.com/lite/main.css">'
    '<link rel="preconnect" href="https://glyph.medium.com">'
    '<link rel="preconnect" href="https://miro.medium.com">'
    '<link rel="preconnect" href="https://cdn-client.medium.com">'
)


def _filler(text):
    """Same-length filler, so a length assertion still means something."""
    if not isinstance(text, str) or not text:
        return text
    words = ("fixture filler text for the offline suite ".split())
    out = []
    while len(" ".join(out)) < len(text):
        out.append(words[len(out) % len(words)])
    return " ".join(out)[:len(text)]


class _Names:
    """One stable pseudonym per real person, for the whole run."""

    def __init__(self):
        self._by_username = {}

    def handle(self, username):
        if not username:
            return username
        if username not in self._by_username:
            self._by_username[username] = "fixture-author-%d" % (
                len(self._by_username) + 1)
        return self._by_username[username]

    def name(self, username):
        return "Fixture Author %s" % self.handle(username).rsplit("-", 1)[1]

    def pairs(self):
        """Longest real handle first, so a handle that is a prefix of
        another is not half-replaced."""
        return sorted(self._by_username.items(),
                      key=lambda kv: -len(kv[0]))


# ---------------------------------------------------------------------------
# Trimming and scrubbing, per carrier
# ---------------------------------------------------------------------------
def _trim_obvinit(payload, keep, names):
    refs = payload.get("references") or {}
    posts = refs.get("Post") or {}
    users = refs.get("User") or {}
    colls = refs.get("Collection") or {}

    order = [i.get("postPreview", {}).get("postId")
             for i in payload.get("streamItems") or []]
    order = [i for i in order if i in posts][:keep]

    kept_posts, kept_users, kept_colls = {}, {}, {}
    for post_id in order:
        node = dict(posts[post_id])
        virtuals = dict(node.get("virtuals") or {})
        virtuals["subtitle"] = _filler(virtuals.get("subtitle"))
        node["virtuals"] = virtuals
        node.pop("previewContent", None)
        node.pop("previewContent2", None)
        node.pop("content", None)
        kept_posts[post_id] = node
        user = users.get(node.get("creatorId"))
        if user:
            user = dict(user)
            original = user.get("username")
            user["username"] = names.handle(original)
            user["name"] = names.name(original)
            user.pop("bio", None)
            kept_users[node["creatorId"]] = user
        coll_id = node.get("homeCollectionId")
        if coll_id and coll_id in colls:
            coll = dict(colls[coll_id])
            for key in ("description", "shortDescription", "sections",
                        "navItems", "header", "image", "logo", "favicon"):
                coll.pop(key, None)
            kept_colls[coll_id] = coll

    return {
        "references": {"Post": kept_posts, "User": kept_users,
                       "Collection": kept_colls},
        "paging": payload.get("paging") or {},
        "tag": payload.get("tag") or {},
        "archiveIndex": payload.get("archiveIndex") or {},
        "streamItems": [i for i in payload.get("streamItems") or []
                        if i.get("postPreview", {}).get("postId") in kept_posts],
    }


def _trim_apollo(state, keep, names):
    wanted, kept = [], {"ROOT_QUERY": state.get("ROOT_QUERY", {"__typename": "Query"})}
    for key, node in state.items():
        if isinstance(node, dict) and node.get("__typename") == "Post":
            if "title" in node:
                wanted.append(key)
    wanted = wanted[:keep]

    def pull(ref):
        if isinstance(ref, dict) and "__ref" in ref:
            target = state.get(ref["__ref"])
            if isinstance(target, dict):
                kept[ref["__ref"]] = _scrub_apollo_node(dict(target), names)
        return ref

    for key in wanted:
        node = _scrub_apollo_node(dict(state[key]), names)
        pull(node.get("creator"))
        pull(node.get("collection"))
        for ref in node.get("tags") or []:
            pull(ref)
        # A post page normalizes its body out into `Paragraph:{id}` entries
        # and points at them from an INLINE `bodyModel` — not through a
        # `__ref`, which an earlier version of this script assumed and which
        # silently produced a fixture with no body at all. Keep the first
        # dozen so the body-reassembly path has something real to read.
        for field, value in list(node.items()):
            if field != "content" and not field.startswith("content("):
                continue
            body = dict(value) if isinstance(value, dict) else {}
            model = body.get("bodyModel")
            if isinstance(model, dict) and "__ref" in model:
                target = state.get(model["__ref"])
                model = dict(target) if isinstance(target, dict) else {}
                inline = False
            elif isinstance(model, dict):
                model = dict(model)
                inline = True
            else:
                continue
            refs = [r for r in (model.get("paragraphs") or [])][:12]
            model["paragraphs"] = refs
            model["sections"] = (model.get("sections") or [])[:3]
            for pref in refs:
                para = state.get(pref.get("__ref")) if isinstance(pref, dict) else None
                if isinstance(para, dict):
                    para = dict(para)
                    para["text"] = _filler(para.get("text"))
                    kept[pref["__ref"]] = para
            if inline:
                body["bodyModel"] = model
                node[field] = body
            else:
                kept[value["bodyModel"]["__ref"]] = model
        kept[key] = node
    return kept


def _scrub_apollo_node(node, names):
    if node.get("__typename") == "User":
        original = node.get("username")
        node["username"] = names.handle(original)
        node["name"] = names.name(original)
        domain = ((node.get("customDomainState") or {}).get("live") or {}).get("domain")
        if domain:
            node = dict(node)
            node["customDomainState"] = {
                "__typename": "CustomDomainState",
                "live": {"__typename": "CustomDomain",
                         "domain": "%s.medium.com" % names.handle(original)}}
    if node.get("__typename") == "Post":
        preview = dict(node.get("extendedPreviewContent") or {})
        if preview:
            preview["subtitle"] = _filler(preview.get("subtitle"))
            node["extendedPreviewContent"] = preview
        url = node.get("mediumUrl")
        if isinstance(url, str) and url:
            # Rewrite the author's half of the address so it matches the
            # pseudonym. Everything the SITE generates — the slug, the id —
            # is left exactly as it was.
            node["mediumUrl"] = re.sub(
                r"//([^./]+)\.medium\.com/",
                lambda m: "//%s.medium.com/" % names.handle(m.group(1)), url)
            node["mediumUrl"] = re.sub(
                r"/@([^/]+)/",
                lambda m: "/@%s/" % names.handle(m.group(1)),
                node["mediumUrl"])
    return node


_ARTICLE_RE = re.compile(r"<article\b.*?</article>", re.S)


def _trim_cards(html, allowed_ids, names):
    """The rendered cards for the stories the fixture kept, and no others.

    Scoped to `allowed_ids` rather than to "the first N cards": a card for a
    story the payload no longer describes would add a DOM-only row, and the
    fixture would then parse MORE stories than the capture's first N — which
    is exactly what this pipeline caught on its first run.
    """
    out = []
    for card in _ARTICLE_RE.findall(html):
        ids = {P.post_id_from_url(m) for m in re.findall(r'href="([^"]+)"', card)}
        ids.discard(None)
        if not ids or not ids <= set(allowed_ids):
            continue
        # Medium hangs a sign-in link off every card whose href carries the
        # story's whole address URL-ENCODED — `%2F%40handle%2Fslug`. The
        # de-personalising pass cannot see a handle in there (the character
        # before it is the `0` of `%40`), so six real handles survived the
        # scrub inside these links while every other copy was replaced.
        # Nothing in the suite reads them, so the payload goes.
        card = re.sub(r'href="[^"]*/m/(?:signin|callback)[^"]*"',
                      'href="/m/signin"', card)
        out.append(card)
    return "".join(out)


def _trim_jsonld(html, allowed_ids):
    """The page's JSON-LD, narrowed to the stories the fixture kept.

    Kept rather than dropped because it is one of the parser's four read
    paths and the only source of `canonical_url` — dropping it left every
    canonical null in the fixture while the capture had them, which is the
    fixture testing less than the code does.
    """
    out = []
    for block in P.jsonld_blocks(html):
        if not isinstance(block, dict):
            continue
        entities = block.get("mainEntity")
        if not isinstance(entities, list):
            continue
        narrowed = dict(block)
        narrowed["mainEntity"] = [
            e for e in entities
            if isinstance(e, dict) and (
                e.get("identifier") in allowed_ids
                or P.post_id_from_url(e.get("url") or "") in allowed_ids)]
        if narrowed["mainEntity"]:
            out.append('<script type="application/ld+json">%s</script>'
                       % json.dumps(narrowed, ensure_ascii=False))
    return "".join(out)


_HEAD_RE = re.compile(r"<head\b.*?</head>", re.S)


def _build(name, html, url, keep, names):
    if name == "not_medium":
        # A former Medium publication now running WordPress. Kept for one
        # assertion — that a page with `<article>` elements and schema.org
        # `Article` blocks but NONE of Medium's asset hosts classifies as
        # blocked rather than as content — so what the fixture needs is the
        # head, two articles and the `wp-content` references. The rest is
        # 200 KB of somebody else's site, including their contributors'
        # bylines, and it is not kept.
        head = _HEAD_RE.search(html)
        articles = _ARTICLE_RE.findall(html)[:2]
        body = "".join(re.sub(r">([^<]{40,})<", "> \u2026 <", a)
                       for a in articles)
        return ("<!DOCTYPE html><html lang=\"en\">%s<body>%s</body></html>"
                % (head.group(0) if head else "<head></head>", body))
    if keep is None:
        # A refusal page. Cloudflare's own bytes, and the point of the
        # fixture is exactly those bytes. `depersonalise` still runs over it.
        return html

    obv = P.obvinit_payload(html)
    apollo = P.apollo_state(html)
    parts = ["<!DOCTYPE html><html lang=\"en\"><head>",
             "<title>fixture: %s</title>" % name, ASSET_HEAD, "</head><body>"]
    # Cloudflare's ordinary script injection, carried over VERBATIM. It is on
    # every page Medium serves — refused and served alike — which is exactly
    # why `challenge-platform` is not in any marker set, and the suite
    # asserts that fact against these fixtures. A fixture without it would
    # let the marker back in unnoticed (§18).
    cf = _CF_SCRIPT_RE.search(html)
    if cf:
        parts.append(cf.group(0))
    allowed = set()
    if obv:
        trimmed = _trim_obvinit(obv, keep, names)
        allowed |= set(trimmed["references"]["Post"])
        parts.append('<script>// <![CDATA[ window["obvInit"](%s) // ]]></script>'
                     % json.dumps(trimmed, ensure_ascii=False))
    if apollo:
        trimmed = _trim_apollo(apollo, keep, names)
        allowed |= {k.split(":", 1)[1] for k, v in trimmed.items()
                    if isinstance(v, dict) and v.get("__typename") == "Post"}
        parts.append("<script>window.__APOLLO_STATE__ = %s;</script>"
                     % json.dumps(trimmed, ensure_ascii=False))
    parts.append(_trim_jsonld(html, allowed))
    parts.append(_trim_cards(html, allowed, names))
    parts.append("</body></html>")
    return "".join(parts)


# Medium's own subdomains, which are hosts rather than people.
_ASSET_SUBDOMAINS = frozenset(("miro", "cdn-client", "glyph", "cdn", "www",
                               "images", "medium"))


def _discovered_handles(text):
    """Every author handle still spelled out in the finished bytes.

    A DISCOVERING pass rather than a list of known names. The payload trims
    register the handles they walk, and between them they registered every
    author the Apollo store and the legacy payload describe — and still
    missed eight, because a JSON-LD author block and a rendered card can name
    a writer whose node the trim never visited. Finding them by shape closes
    that whole class rather than one instance of it.
    """
    found = set()
    for handle in re.findall(r"/@([A-Za-z0-9_][\w.-]{0,60})", text):
        found.add(handle)
    for sub in re.findall(r"//([a-z0-9][a-z0-9-]{0,60})\.medium\.com", text):
        if sub not in _ASSET_SUBDOMAINS:
            found.add(sub)
    return {h for h in found if not h.startswith("fixture-author")}


def depersonalise(text, names):
    """Replace every real handle this run has seen, over finished bytes.

    A LAST pass, run over every fixture once ALL of them have been built.
    Every path above rewrites the handles it knows about and between them
    they still missed eleven: a handle also appears in a story's
    `uniqueSlug`-built URL, in a JSON-LD author block, in a rendered card's
    href, and — the one that made the ordering matter — inside Cloudflare's
    own challenge page, which echoes the path it refused.
    """
    # The boundary characters deliberately exclude `.` and `-`. A handle's
    # commonest home is the LEFT of a subdomain — `donttakemyword.medium.com`
    # — and a boundary class that treated `.` as part of a word refused to
    # match exactly there, leaving seven real handles in the file while the
    # replacement looked like it was working.
    for real, pseudonym in names.pairs():
        text = re.sub(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(real),
                      pseudonym, text)
    # Cloudflare's challenge script carries 32-hex session ids. They are not
    # credentials and they expired the moment the page was fetched, but a
    # 32-hex run in a public repo reads as a live key to every scanner that
    # looks — including this repo's own — and the checks need the SHAPE of
    # the script, not the value (§10).
    # Replaced with a NON-HEX run, not with zeros: a 32-character string of
    # zeros is still 32 hex characters and still trips the repo's own
    # credential grep, which is how the first version of this scrub passed
    # locally and failed the suite.
    text = re.sub(r"(?<![A-Za-z0-9])[0-9a-f]{24,}(?![A-Za-z0-9])",
                  lambda m: "scrubbed-" + "x" * (len(m.group(0)) - 9), text)

    # Then whatever the payload trims never saw. Longest first, so a handle
    # that is a prefix of another is not half-replaced.
    for handle in sorted(_discovered_handles(text), key=len, reverse=True):
        text = re.sub(
            r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(handle),
            names.handle(handle), text)
    return text


# ---------------------------------------------------------------------------
def _expected(html, url, keep, names):
    """What the FULL capture parses to, for the stories the fixture keeps,
    with the same scrub applied to the expectations."""
    rows = P.parse_posts(html, url, page=1)
    return rows[:keep] if keep else rows


def main() -> int:
    if not os.path.isdir(CAPTURES):
        print("No captures directory at %s.\n"
              "Take some with --dump-html first; see this file's docstring."
              % CAPTURES)
        return 2

    names = _Names()
    raw, fixtures, urls, failures = {}, {}, {}, []

    for name, (filename, url, keep) in SOURCES.items():
        path = os.path.join(CAPTURES, filename)
        if not os.path.exists(path):
            failures.append("%s: no capture at %s" % (name, path))
            continue
        with open(path, encoding="utf-8") as handle:
            original = handle.read()

        raw[name] = (original, url, keep)

    # Build every fixture first, so that the de-personalising pass at the end
    # knows every handle this run saw — including one that only appears in a
    # later capture. Verification then runs on the FINAL bytes, which is the
    # only version of it worth having.
    for name, (original, url, keep) in raw.items():
        fixtures[name] = depersonalise(_build(name, original, url, keep, names),
                                       names)
        # The URL a fixture was taken from is stored beside it, and an author
        # page's URL is that person's handle. Run it through the same pass:
        # the handle is already mapped by the time this line runs, so the
        # pseudonym matches the one inside the fixture.
        urls[name] = depersonalise(url, names)

    for name, (original, url, keep) in raw.items():
        fixture = fixtures[name]
        url = urls[name]

        # 1. It must classify the same way. This is what catches a trim that
        #    dropped the site's own asset references.
        want_state = P.detect_page_state(original, 200, url)
        got_state = P.detect_page_state(fixture, 200, url)
        if want_state != got_state:
            failures.append("%s: classifies as %s, the capture as %s"
                            % (name, got_state, want_state))

        if keep is None:
            print("%-14s %7d bytes  state=%s  (verbatim)"
                  % (name, len(fixture), got_state))
            continue

        # 2. Every column of every story it keeps must parse identically —
        #    apart from the fields deliberately scrubbed, which are compared
        #    by LENGTH so a truncation still fails.
        want = _expected(original, url, keep, names)
        got = P.parse_posts(fixture, url, page=1)
        if len(got) != len(want):
            failures.append("%s: fixture parses %d stor(ies), the capture's "
                            "first %d" % (name, len(got), len(want)))
        # Compared by NULL-NESS only, because the scrub or the trim changes
        # the value itself: the person's half of a URL, the filler text, and
        # a body cut to its first dozen paragraphs. Everything the site
        # generates — ids, titles, counts, times, tags, flags — is compared
        # exactly, which is where a parsing regression would show.
        scrubbed = {"author", "author_username", "author_url", "subtitle",
                    "url", "content", "content_chars", "canonical_url",
                    "preview_image_url", "publication_url", "data_source",
                    "position"}
        for a, b in zip(want, got):
            for field, value in asdict(a).items():
                if field in ("source", "scraped_at", "page"):
                    continue
                other = getattr(b, field)
                if field in scrubbed:
                    if (value is None) != (other is None):
                        failures.append("%s/%s: %s is %r in the capture and "
                                        "%r in the fixture"
                                        % (name, a.sku, field, value, other))
                    continue
                if value != other:
                    failures.append("%s/%s: %s is %r in the capture and %r "
                                    "in the fixture"
                                    % (name, a.sku, field, value, other))
        print("%-14s %7d bytes  state=%s  %d stor(ies)"
              % (name, len(fixture), got_state, len(got)))

    # 3. Nothing personal may survive into the file.
    blob = json.dumps(fixtures, ensure_ascii=False)
    for real, _ in names.pairs():
        if real and len(real) > 3 and real in blob:
            failures.append("the real handle %r survived into the fixtures"
                            % real)

    if failures:
        print("\n%d problem(s):" % len(failures))
        for f in failures[:40]:
            print("  - %s" % f)
        return 1

    fixtures["_URLS"] = urls
    with open(OUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(fixtures, handle, ensure_ascii=False, indent=1)
    print("\nWrote %s (%d fixtures, %d bytes)."
          % (OUT_PATH, len(SOURCES), os.path.getsize(OUT_PATH)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
