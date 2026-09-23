"""
output_writer.py
-----------------
Shared row models + JSON/CSV writers used by all three scrapers.

Four modes, one row shape
-------------------------
    --mode tag        /tag/{slug}                       a tag's story feed
    --mode archive    /tag/{slug}/archive/{y}/{m}/{d}   one day of that tag
    --mode author     /@{username}                      one writer's stories
    --mode post       /p/{id}                           one story, with body

All four yield the SAME class, because they are four views OF the same
thing. A story is the only leaf Medium publishes: a tag, a day of an archive
and an author page are three ways of selecting stories, and a post page is
one story with its body attached. So there is one dataclass here (a sibling
repo needs a second for reviews; this one does not), and `diff_runs.py` can
compare a tag run against an archive run on the columns both populate.

The row is `Post` and not `Product`
-----------------------------------
Every other repo in this family names its row `Product` and keeps the
commerce columns even where they are null, because on a shop they are null
for a reason worth recording. Medium is not a shop: it has no price, no
currency, no discount, no stock and no brand, and there is no measurement to
write down beside those names because there is nothing on the page to
measure. Six columns null on every row of every run of every mode is exactly
what §9 says must not exist, so they are not here.

What IS kept, byte-identical and in order, is the family prefix — `source`,
`scraped_at`, `url`, `sku`, `title` — so one column name works across the
family and a consumer reading six of these repos reads the same first five
columns in the same order (§9).

Two family columns are absent for measured reasons rather than definitional
ones, and those measurements belong here:

    rating          Medium publishes no rating on a story. It publishes
                    claps, which have their own column and are a count of
                    taps rather than a score out of five; writing 248 into a
                    column the rest of the family fills with 4.4 would make
                    the family's own schema lie.
    review_count    Its nearest equivalent is `responses`, which is named
                    for what Medium calls it.

One column that WOULD have been here is not, and the measurement is the
reason: `is_member_only` as distinct from `is_paywalled`. Medium's payloads
carry `isLocked`, `isSubscriptionLocked`, `isMarkedPaywallOnly` and
`lockedPostSource`, and on the 128 stories of one day archive the first two
agreed on all 128. One column, not four, until a capture shows them
disagreeing.

Everything below is row-class-agnostic: pass `row_cls` so an empty CSV still
gets the right header for the mode that produced it.
"""
import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# The host a row came from. It genuinely varies on Medium: a story lives on
# `medium.com`, on its author's subdomain (`donttakemyword.medium.com`) or on
# a publication's custom domain (`betterprogramming.pub`), and all three are
# the same platform serving the same story. This column says which address
# actually answered.
# `product_parser.source_of` fills it from the URL; this is the fallback for
# a row built without one.
SOURCE_DEFAULT = "medium.com"


@dataclass
class Post:
    # --- the family prefix, byte-identical and in order across the family ---
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # The story's address on Medium.
    #
    # NOT the canonical URL. Medium publishes a canonical that for an
    # imported or cross-posted story points at ANOTHER SITE — 4 of the 20
    # entries in one tag feed's JSON-LD pointed at habr.com and dev.to. A
    # parser that trusts the canonical writes habr.com into a Medium row
    # while every other column still looks right (FINDINGS.md §5).
    # `canonical_url` below carries it where it differs, which is a genuinely
    # useful column and not a substitute for this one.
    url: str = ""
    # Medium's own post id — the 12-hex suffix every story URL ends with, and
    # the `id` on every node of both payloads. Stable across a story being
    # moved into a publication, renamed, or re-slugged, which none of the
    # URLs are.
    sku: Optional[str] = None
    title: Optional[str] = None

    # --- the story -------------------------------------------------------
    # Medium's own one-line preview. `virtuals.subtitle` on an archive row,
    # `extendedPreviewContent.subtitle` on a feed row; the same sentence.
    subtitle: Optional[str] = None
    # Where the canonical differs from `url` — an imported story's original
    # home. Null when Medium is the canonical, which is the common case.
    canonical_url: Optional[str] = None

    # --- who wrote it ----------------------------------------------------
    author: Optional[str] = None
    author_username: Optional[str] = None
    author_url: Optional[str] = None
    # The publication the story was published in, where it was published in
    # one. A story published to an author's own profile has none, and null
    # here means exactly that rather than a read that failed.
    publication: Optional[str] = None
    publication_url: Optional[str] = None

    # --- when ------------------------------------------------------------
    # ISO 8601 UTC. Medium ships epoch milliseconds — as an int in the modern
    # payload and as a STRING in the legacy one, which is why the converter
    # accepts both.
    published_at: Optional[str] = None
    updated_at: Optional[str] = None

    # --- what the site counts --------------------------------------------
    # Claps, from `clapCount` on a feed row and `virtuals.totalClapCount` on
    # an archive row.
    #
    # NOT `virtuals.recommends`, which sits beside it, is populated on 128 of
    # 128 archive posts, and is the retired pre-2017 recommend count: 25
    # against a `totalClapCount` of 248 on the same story. Reading it would
    # have filled this column completely and wrongly, which is the failure
    # §10 exists to catch.
    claps: Optional[int] = None
    responses: Optional[int] = None
    # Minutes, as Medium computes them — a float, not a rounded "15 min read".
    #
    # Null on a tag-feed row and populated on an archive, author or post row.
    # That is the site: the feed's payload carries 21 fields per post and has
    # no reading time in it at all. `data_source` is the column that says
    # which view a row came from, and therefore why this is null.
    reading_time_min: Optional[float] = None
    word_count: Optional[int] = None

    # --- what kind of story ----------------------------------------------
    # Behind Medium's paywall. `isLocked` on a feed row,
    # `isSubscriptionLocked` on an archive row; 35 of 128 on one day.
    is_paywalled: Optional[bool] = None
    is_series: Optional[bool] = None
    # Medium's own language detection, not a guess from the text. Two values
    # on one English-tag day archive (123 `en`, 5 `id`), so it is a real
    # column even on a single-language tag.
    language: Optional[str] = None
    tags: Optional[List[str]] = None
    preview_image_url: Optional[str] = None

    # --- the body, in post mode only --------------------------------------
    # The full story text, joined from the paragraph nodes Medium ships in
    # the page state. Null on every listing row by design: a listing payload
    # carries a subtitle and no body, and fetching 128 stories to fill a
    # column nobody asked for is not what `--mode archive` is for.
    # A paywalled story yields only its free preview; `is_paywalled` says so.
    content: Optional[str] = None
    content_chars: Optional[int] = None

    # --- provenance -------------------------------------------------------
    # WHICH of the page's views built this row (never a guess presented as a
    # fact). `diff_runs.py` reports a difference that comes with a
    # `data_source` difference as `source_changed` rather than as a change.
    #
    #   obvinit        the legacy archive payload — 84 fields per post
    #   apollo         the modern page state — 21 to 56 fields
    #   obvinit+dom    /  apollo+dom, where the DOM confirmed or filled a value
    #   jsonld         the tag feed's structured data
    #   dom            the rendered card only
    data_source: Optional[str] = None
    # The fetch this row came from, and its position within it. Unique as a
    # pair across a run; `smoke_test.py` asserts it, because `position`
    # restarts at 1 on every page and the column is worthless without `page`
    # beside it (§18).
    page: Optional[int] = None
    position: Optional[int] = None


# Every mode yields the same class: a Medium row is a story whichever view
# named it, and the columns a given view cannot fill are null with
# `data_source` saying why.
ROW_CLASS_BY_MODE = {"tag": Post, "archive": Post, "author": Post, "post": Post}

# Kept under the family's name so that code shared with the siblings — and
# anything a user wrote against one of them — keeps importing successfully.
# This repo has exactly one row class, so the alias is the same object rather
# than a second definition that could drift.
Product = Post

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku` and
# to hand to diff_runs.py. All four qualify: `sku` is Medium's post id and a
# feed names each story once.
UNIQUE_BY_SKU_MODES = ("tag", "archive", "author", "post")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating next-page link then re-parses a page without
    duplicating its rows into the final output. This site needs that more
    than its siblings do: a scroll batch re-parses the WHOLE feed, cards
    already read included, so every batch after the first arrives mostly
    duplicate by design. A batch that drops all of its rows is the signal
    that the feed is exhausted, which is §7's data-based terminating
    condition and the only one available here.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.

    All three of this repo's modes are one row per `sku`, so `key` is never
    overridden here — the parameter exists because the rest of the family
    shares this function and one of them needs it.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
#
# On this site this code specifically does NOT cover the three ways to get a
# real page with no products on it: a `/p/<slug>` discovery hub, which
# answers 200 with banners and carousels and no stories; a tag whose feed
# matches nothing ("Oops, produk nggak ditemukan"); and one page past the
# end of a category listing. All three are EXIT_NO_PRODUCTS — the request
# was served exactly as asked and simply has no products on it. Reporting
# any of them as blocked would send a user hunting for a proxy problem that
# does not exist.
#
# What EXIT_BLOCKED means here is unusually literal: this site refuses a
# address it has scored NOTHING at all. No status code, no interstitial, no
# vendor marker — the HTTP/2 stream is reset and the run sees a connection
# error rather than a page.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


# Exit code for a run that never GOT its pages: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering with
# something that is not the page that was asked for.
#
# Distinct from EXIT_NO_PRODUCTS because those are opposite facts. Exit 4 is
# a statement about the CATALOGUE — "we asked, and the answer was nothing" —
# so handing it to a run that never reached the site tells a pipeline the
# listing is empty when nothing was read at all.
#
# 5 rather than a new number, and 5 rather than EXIT_PARTIAL:
#
#   * this family's contract already reserves 5 for a transport failure
#     (scraper_api_client has used it for a remote API error since it was
#     written), so this needs no new code and no per-repo table for a caller
#     driving more than one of these scrapers;
#   * EXIT_PARTIAL (6) means "some rows were gathered and the output is
#     incomplete". A run holding nothing writes no output at all, so a
#     consumer that reads the file on a 6 finds either nothing or the
#     PREVIOUS run's good data, which `save` deliberately does not
#     overwrite. Exit 5 promises no file.
#
# Deliberately NOT applied when rows WERE gathered: a timeout on page 7 of
# 10 is a partial run (exit 6, output written), which is already right. This
# decides only what a run holding nothing reports.
EXIT_FETCH_FAILED = 5


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "tag", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are both recorded, and on this site BOTH of them
    genuinely vary. `mode`, because a tag row and an archive row populate
    different columns: reading time and word count come from a payload the
    tag feed does not carry at all (21 fields per post against the archive's
    84), so diffing one against the other would report both as having
    appeared from nowhere. `source`, because a Medium story is served from
    `medium.com`, from its author's subdomain or from a publication's custom
    domain, and two runs that landed on different hosts describe the same
    catalogue through different addresses. diff_runs.py refuses a pair whose
    modes differ.

    `extra` carries facts about the run that are not about any single row.
    This repo puts the scroll trace there, plus `rows_new_per_batch`, and —
    in archive mode — the DAYS a run covered and any day that redirected.
    A redirect is the thing worth recording: a tag day with no stories does
    not 404, it redirects up to the month view, which is a different
    renderer holding a different set of stories (FINDINGS.md §4).

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `shop_rating` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 products -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the correct output.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 products — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected outcome.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" beside it, and on
# this site the ordering between them is not a preference — it is the only
# thing that works.
#
# Medium publishes no `link[rel=next]`, no pagination control and no numbered
# anchors anywhere, in any mode. Three of the four modes extend their feed
# over an XHR when the reader nears the bottom, and there is no URL that
# addresses batch 2 of them.
#
# The fourth is the exception the whole repo is built around: a tag's DAY
# archive, `/tag/{slug}/archive/{yyyy}/{mm}/{dd}`, is a real address holding
# a whole day of stories, and walking it backwards is how a run gets volume.
# Its terminating condition is still data and not a selector — a day whose
# stories are all already in `seen`, or a day the site redirected away from,
# ends the walk.
#
# "no_new_products" is therefore the data-side termination condition, and on
# this site it is the ONLY one: an infinite scroll has no last page to
# recognise. "pagination_exhausted" is kept for the family's shape and is set
# when a scroll produced nothing new AND nothing behind it was refused — see
# `page_flow.advance_feed`, which is careful to keep those two apart, because
# a refused batch reported as an exhausted listing is how a throttled run
# says "complete".
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted",
                         "no_new_products")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "tag", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    # Completeness is decided by the reason AND by the evidence. A named
    # list of stop reasons cannot cover a failure recorded somewhere else,
    # and `pages_failed` is somewhere else: a run whose loop ended for a
    # COMPLETE reason while individual pages failed reported exit 0 and
    # `status: complete` with a non-empty `pages_failed` in the same
    # sidecar — a file that contradicts itself, and a pipeline branching
    # on `status` reading a short run as a whole one.
    #
    # Found by a third-party audit of a sibling repo and measured across
    # the family by CALLING each `finish_run` rather than grepping for the
    # fix: 28 of 32 repos behaved this way. Same shape as the exit-code
    # unification this file already carries — a rule keyed on a list of
    # names has a hole for every name nobody added to it.
    complete = stop_reason in COMPLETE_STOP_REASONS and not pages_failed
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are genuinely different things and a pipeline branches on
        # them (§8: blocked is not empty is not partial):
        #
        #   blocked          something stood between the run and the content
        #   did not complete we never reached the site — a dead proxy, a
        #                    load timeout, a refused batch
        #   completed        we asked, and the site's reply was nothing
        #
        # The middle one used to fall through to EXIT_NO_PRODUCTS, and that
        # was measured rather than reasoned about in a sibling repo: an
        # unreachable proxy produced exit 4 — "ran fine, found nothing" — on
        # a feed with hundreds of rows, while the sidecar beside it said
        # `status: failed`, `pages_completed: 0`. A consumer branching on the
        # exit code, which is what this family says exit codes are for, would
        # have recorded an empty catalogue.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Failed run: 0 of {pages_requested} page(s) were "
                  f"fetched ({stop_reason}). This is NOT an empty result — "
                  f"nothing was read from the site at all.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
