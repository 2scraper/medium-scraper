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
its own issue template. The detail that saves the most time is WHICH of the
two read paths broke, because this repo has two and they fail differently.

**Path 1 — the inline GraphQL payload.** Medium server-renders no content DOM
at all (measured: 0 occurrences of its own `q-box` class in the raw response
body of six captures), and what it does send is its own query results pushed
into the page:

```
window.ansFrontendGlobals.data.inlineQueryResults.results["<hash>"]
    .push("{\"data\":{…}}")
```

If that push pattern moves, a run does NOT fail. It quietly degrades to the
DOM-only path and every row comes back with a null `upvotes`, `views`,
`created_at`, `answer_id` and a body truncated to what the card rendered —
while the row count, the titles and the authors all stay healthy. The
`data_source` column is what shows it: every row reads `dom` where some used
to read `dom+inline`. The canary asserts exactly that on a question URL, and
every run logs its payload coverage per batch.

**Path 2 — the rendered cards.** Four of Medium's own hooks, all of them
semantic rather than build hashes:

1. **`a.answer_timestamp`** — the permalink under every answer, and THE
   anchor. If it moves, a run reports 0 rows and exit 4, which is loud.
2. **`.puppeteer_test_question_title`** — the question on a feed card. Absent
   on a question page by design; the page's `h1` is the fallback there.
3. **`.spacing_log_answer_content` / `.puppeteer_test_answer_content`** — the
   answer body, and the thing the card scope widens until it finds.
4. **`.spacing_log_answer_header`** — the block holding the author link, the
   credential and the date. The credential is read by REMOVING the author and
   timestamp links from a copy of it, so a change to either of those two
   shows up as a mangled credential rather than as a missing one.

A third thing can break without either path failing: the **join** between
them. When it breaks, the row count and the titles stay healthy while the
counts empty out — so if you are reporting a change, the `data_source`
breakdown of a run is the number to include.

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
   This canary needs no secret to do real work: eight of fourteen fetches
   were served in full with no key and no proxy, from a DATACENTRE address
   at that. What it has NOT been measured doing is getting past
   Cloudflare from a shared datacentre address, and since the challenge here
   tracks the address's recent request rate, a runner is the worst case for
   it. That is exactly why a block there is a warning rather than a failure —
   until you set `MEDIUM_PROXY`, after which it is a failure, because then it
   means something.
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

Ten properties in this repo exist because they were once absent, or because
they cost a sibling repo real time. Tests pin all ten, so a PR that breaks one
will fail rather than silently regress:

- **`sku` is the permalink, not the numeric id.** Medium publishes a numeric
  `aid` and it would be the tidier key — but a topic feed, a whole mode,
  carries no numeric id anywhere (measured 0 occurrences across two topic
  captures in two languages), and a key null for a third of the modes cannot
  be the column `diff_runs.py` joins on. `answer_id` carries the number where
  the payload reached the row.
- **The counts come from the payload and are NULL on a topic row.** An
  anonymous reader is shown no upvote or view count in the DOM at all — 0
  rendered nodes across five captures. `data_source` is the column that says
  whether a null means zero or means nobody looked, and `diff_runs.py`
  reports a count that moved together with `data_source` as
  `source_changed` rather than as a change.
- **The card's author link is not the first `/profile/` link in the card.**
  The first one is the avatar and has no text, so reading it gives an author
  of `""` on every row while every coverage check reads 100%.
- **The card scope stops at the answer BODY, not at the second id.** §4's
  "widen until you cover more than one item" never fires here: Medium wraps
  each card in a chain of single-child divs, and the ancestor sixteen levels
  up still holds one permalink while having absorbed the page footer
  (measured: a 161,882-byte scope on one page). The stop condition is the
  positive one.
- **A relative date is never parsed into a date.** The card prints "Aug 14",
  "2y", "3 años" — a display string in the page's own language, and "2y"
  cannot be resolved to a day. `date_text` keeps it verbatim; `created_at`
  comes from the payload's epoch-MICROSECOND timestamp or is null.
- **`created_at` is microseconds.** Read as seconds it lands in the year 57
  million and as milliseconds in 57705, and both parse without error.
- **A challenge that survives its retries is BLOCKED, not empty.** This
  differs from every sibling repo and was found by running the thing: with
  `blocked: False` in `STATE_POLICY` the first live run handed Cloudflare's
  6 KB interstitial to the parser and reported exit 4 ("ran fine, found
  nothing") on a topic holding hundreds of answers.
- **A marker that matches every good page is not a marker.** `cf-turnstile`,
  `challenges.cloudflare.com` and `recaptcha` each appear on EVERY page
  Medium serves — it wires Turnstile into every page and never renders it to
  an anonymous reader — and `recaptcha` appears zero times on the challenge
  page. None of the three is in this repo's marker set, and the suite asserts
  they stay out.
- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` zero rows, `5` remote API error, `6` partial. A
  pipeline branches on these. And a scroll that produced nothing new is
  `complete` only when nothing behind it was refused — otherwise it is
  `partial`, because a throttled run reporting "complete" is the failure
  §7 exists to prevent.

Two more that are about the fixtures rather than the code:

- **A fixture is CUT from a real capture and proven to parse identically**,
  column for column, by `make_fixtures.py`. Never hand-written.
- **The PEOPLE in a capture are replaced before it is committed.** Author
  names, profile slugs, credentials, answer bodies and the links an answer
  cited all become placeholders; question titles, ids, counts, timestamps and
  every piece of markup Medium generates stay verbatim. The suite asserts both
  halves — that no real name survives, and that the structure did.

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
medium.com, say in the PR what you ran, which URL and page kind, from
which exit, and what you got — including the price and image coverage
percentages the run prints, and the scroll trace from the sidecar. Note that
a run from a datacentre address gets NO RESPONSE AT ALL, so "it returned
nothing" from a VPS is not a finding. Product counts differ by category, by
URL and by how far the scroll got, so a bare "worked for me" is not
reproducible.

**Run more than the primary engine.** "Mirror them exactly" is a design rule,
not a verification: the first live run of the pyppeteer engine crashed on its
FIRST fetch on a signature mismatch that four separate offline checks and 400
green assertions had not caught.

Do not add anything that submits the registration form. This project
deliberately never does, and a captcha token proved valid by creating a real
account is not a result worth having.

## Scope

This repo scrapes **public pages** on Medium: category listings, search
listings and product pages, exactly as an anonymous visitor is served them.
Out of scope: anything behind a login, anything that submits a form, and
anything that defeats a protection rather than passing it the way an ordinary
browser does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
