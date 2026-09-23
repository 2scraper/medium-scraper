# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but three things are **not**
masked: raw HTML dumps, the Scraper API's `x-debug` response header, and your
shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

Medium changing its markup is the normal way this stops working, and it has
its own issue template. The detail that saves the most time is WHICH read
path broke, because this repo has four and they fail differently.

**The two payloads.** Medium serves the same catalogue through two
renderers, and the URL decides which one answers:

- `window.__APOLLO_STATE__` — the modern site: tag feeds, author pages and
  story pages. Normalized `Post:{id}` nodes joined by `__ref` pointers to
  `User` and `Collection`.
- `window["obvInit"]({…})` — the legacy renderer, and the only one that
  answers a tag's DAY archive: up to 128 stories in one 2.3 MB response, 84
  fields each, and no Apollo state on that page at all.

If either payload moves, a run does not necessarily fail. It degrades to the
page's JSON-LD or to the rendered cards, and the row count, the titles and
the authors can stay healthy while `claps`, `reading_time_min`, `word_count`
and `language` empty out. The `data_source` column is what shows it: every
row reads `jsonld` or `dom` where it used to read `apollo` or `obvinit`.

**The two fallbacks** — the tag feed's JSON-LD, and the rendered cards
(`article` on the modern site, `.streamItem` on the legacy archive, with the
author read from a `/@handle` link that carries no story id).

So if you are reporting a change, the `data_source` breakdown of a run is
the number to include.

`--dump-html PATH` writes the exact bytes the parser was given, on success as
well as failure, and a run that finds nothing writes a dump and a screenshot
next to the output on its own.

## Before this repository goes public

One item cannot be undone later, so it belongs on a checklist rather than in
someone's head. **A commit on top cannot reach what a published tag and a
merged PR's refs already hold** — those stay attached to the PR and cannot be
deleted from it. Afterwards, only a fresh repository removes anything.

```bash
python3 .github/ci_checks.py --history-check
```

That applies the same credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. It is deliberately not part of
`--all` and not run by CI: it shells out to git once per object, and a dirty
history needs a decision, not a red check on every push.

Then the rest of the presentation, in the order that matters:

1. `python3 smoke_test.py` green, and the canary dispatched at least once —
   including its WARNING branch, which is what runs when a bare GitHub
   runner's datacentre address is refused and no `MEDIUM_PROXY` secret is set.
   This canary needs no secret to do real work: every number in the README
   was taken from a datacentre address with no key and no proxy. Since
   Medium's challenge tracks the address's recent request rate, a shared
   runner is the worst case for it — which is why a block there is a warning
   rather than a failure until you set `MEDIUM_PROXY`, after which it is a
   failure, because then it means something.
2. The repo description, homepage and topics set (see the family notes on
   what those should say).
3. Only then the row in the org profile README — and check it with an
   ANONYMOUS request rather than your own logged-in browser. A row pointing
   at a private repo is a 404 for every visitor, which costs more trust than
   the missing row.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions with inline HTML/JSON fixtures — no pytest, no
conftest, no fixtures directory. Copy the nearest existing check and edit it.

These properties exist because they were once absent, or because they cost
this repo or a sibling real time. The suite pins them, so a PR that breaks
one will fail rather than silently regress:

- **`sku` is Medium's own 12-hex post id.** It survives a story being
  renamed, re-slugged or moved into a publication; none of the URLs do.
- **`url` is the story's address on Medium, not the canonical.** A
  cross-posted story's canonical points at another site entirely — 4 of 20
  JSON-LD entries on one tag feed pointed at habr.com and dev.to. It is kept
  in `canonical_url` instead.
- **`virtuals.recommends` is not the clap count.** It is the retired
  pre-2017 recommend count — 25 beside a `totalClapCount` of 248 on the same
  story — and reading it fills the column completely and wrongly.
- **A publication home page is refused.** Its `Post` nodes are
  `{__typename, id}` and nothing else, so rows built from them would carry a
  sku and 26 nulls while the run reported success.
- **Only the day archive has per-page addresses.** `?page=2` on a tag feed is
  ignored and the feed returns its first stories again, so `--pages` and
  `--concurrency` do something only in `--mode archive`, and are refused
  with the reason elsewhere.
- **A day with no stories redirects up to its month**, which is the other
  renderer holding a different set of stories. The walk compares the landed
  URL with the requested one and stops rather than attributing one period's
  stories to another.
- **A marker that matches every good page is not a marker.**
  `challenge-platform` appears on every page Medium serves, and so does its
  reCAPTCHA Enterprise markup (3-4 `g-recaptcha` references on pages known to
  be good, 0 on the challenge). Neither is in this repo's marker set, and the
  suite asserts they stay out.
- **A challenge that survives its retries is BLOCKED, not empty.** Otherwise
  Cloudflare's interstitial is parsed as a feed and reported as exit 4 ("ran
  fine, found nothing").
- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` zero stories, `5` remote API error, `6` partial. A
  pipeline branches on these.

Two more that are about the fixtures rather than the code:

- **A fixture is CUT from a real capture and proven to parse identically**,
  column for column, by `make_fixtures.py`. Never hand-written.
- **The PEOPLE in a capture are replaced before it is committed.** Every
  real author name and handle becomes a pseudonym; story ids, counts,
  timestamps and every piece of markup Medium generates stay verbatim. The
  suite asserts both halves — that no real name survives, and that the
  structure did.

There is also a naming check: certain phrases are banned repo-wide and the suite
fails naming them. If it trips, read the message — the phrase is wrong for a
reason, not merely unfashionable.

### Style

- **Match the file you are editing.** No formatter is enforced.
- **Comments explain *why*.** What the code does is visible; why it does it that
  way, especially where the obvious version is wrong, is not.
- **A timeout on every remote call.** Every browser library used here has needed
  an explicit timeout its own API does not provide, and each has needed its own
  route out of the runtime — reporting a timeout is not the same as exiting on
  one. If you add a call to a remote browser or API, bound it.
- **Fail loudly.** A function that returns an empty list on error, or logs
  success without checking that the thing it wanted actually happened, is the
  single most common bug class in this codebase's history. A selector that
  matches the *wrong* element is worse than one that matches nothing, because
  the second one tells you.

### If your change needs a live run

Most do not — the suite covers the parser, the writers, the captcha classifier
and the CLI contract against inline fixtures. If yours genuinely needs
medium.com, say in the PR what you ran, which URL and mode, from which exit,
and what you got — including the coverage lines the run prints and the
`data_source` breakdown. Row counts differ by tag, by day and by mode, so a
bare "worked for me" is not reproducible.

**Run more than the primary engine.** "Mirror them exactly" is a design rule,
not a verification: in a sibling repo the first live run of the pyppeteer
engine crashed on its FIRST fetch on a signature mismatch that four separate offline checks and 400
green assertions had not caught.

## Scope

This repo scrapes **public pages** on Medium: tag feeds, a tag's day
archives, author pages and stories, exactly as an anonymous visitor is served
them.
Out of scope: anything behind a login, anything that submits a form, and
anything that defeats a protection rather than passing it the way an ordinary
browser does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
