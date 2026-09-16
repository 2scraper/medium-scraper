# medium-scraper

[![release](https://img.shields.io/github/v/release/2scraper/medium-scraper?sort=semver)](https://github.com/2scraper/medium-scraper/releases)
[![tests](https://github.com/2scraper/medium-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/medium-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/medium-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/medium-scraper/actions/workflows/canary.yml)
[![python](https://img.shields.io/badge/python-3.9%20%E2%80%93%203.13-blue)](pyproject.toml)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20pyppeteer%20%7C%20Scraper%20API-lightgrey)](#engines)
[![runs without an account](https://img.shields.io/badge/runs%20without%20an%20account-yes-brightgreen)](#do-i-need-to-pay-for-anything)

A Medium story scraper. Reads a tag feed, a tag's day archive, an author's
profile or a single story, and writes JSON and CSV with one row per story.

Four engines — Playwright (primary), Selenium, pyppeteer, and 2Captcha's
Scraping Browser over CDP. All four produce the same rows, the same exit
codes and the same run metadata.

```bash
pip install -r requirements.txt -r requirements-playwright.txt
playwright install chromium

python3 playwright_scraper.py --url "https://medium.com/tag/python"
```

---

## The one thing to know before you start

**What Medium's Cloudflare hard-blocks is the `HeadlessChrome` token in the
User-Agent, not headless browsing.** Measured 2026-09-16 from one datacentre
address:

| what sent the request | result |
|---|---|
| `curl` | HTTP 403, 5 KB Cloudflare WAF block page |
| headless Chromium, **default UA** (carries `HeadlessChrome`) | HTTP 403, **11 of 11** |
| headless Chromium, ordinary Chrome UA | never WAF-blocked, **0 of 11** |
| headful Chromium, ordinary Chrome UA | served **8 of 8** |

These engines build their User-Agent from the browser's own version and never
send that token, which is why they get data from an address `curl` gets
nothing from. Headful is still the default, on the smaller margin above
(8/8 against 6/8 served in a paced interleaved run — the misses are
Cloudflare's transient managed challenge, not the WAF). Pass `--headless` on
a machine with no display.

---

## The four modes

| mode | URL | what one fetch returns | pages |
|---|---|---|---|
| `--mode tag` | `/tag/{slug}` | 53 stories | one |
| `--mode archive` | `/tag/{slug}/archive/{yyyy}/{mm}/{dd}` | **up to 128 stories** | one per day, walkable |
| `--mode author` | `/@{username}` | 10 stories | one |
| `--mode post` | `/p/{id}` or any story URL | 1 story, with its full text | one |

The mode is inferred from the URL. Passing one that disagrees is an error
rather than an override: the mode is a property of the path.

```bash
# a tag feed
python3 playwright_scraper.py --url "https://medium.com/tag/python"

# the volume path: five days of a tag, three workers
python3 playwright_scraper.py \
  --url "https://medium.com/tag/python/archive/2026/09/10" \
  --pages 5 --concurrency 3 --delay 20

# one writer
python3 playwright_scraper.py --url "https://medium.com/@quincylarson"

# one story, with its body
python3 playwright_scraper.py --url "https://medium.com/p/d373fe2c96b7" --format json
```

### Measured, 2026-09-16, no proxy and no API key

| run | rows | title + author | claps | reading time |
|---|---|---|---|---|
| `/tag/python` | 53 | 100% | 34 of 53 | 0 of 53 |
| `/tag/python/archive/2026/09/10` | 128 | 100% | 100% | 100% |
| the same, `--pages 2` | 195 | 100% | 100% | 100% |
| `/@quincylarson` | 10 | 100% | 100% | 100% |
| `/tag/日本語` | 42 | 100% | 29 of 42 | 0 of 42 |
| `/p/d373fe2c96b7` | 1 | 100% | 100% | 100% + an 18,352-character body |

---

## Where the data comes from, and why columns differ between modes

Medium serves the same catalogue through **two different renderers**, and the
URL decides which answers:

* the modern site ships `window.__APOLLO_STATE__` — tag feeds, author pages,
  story pages. 21 to 56 fields per story.
* a tag's **day archive** ships the legacy `window["obvInit"]({…})` and no
  Apollo state at all. **84 fields per story, 128 stories in one 2.3 MB
  response**, plus Medium's own index of which years the tag has stories in.

That is why `reading_time_min`, `word_count` and `language` are populated on
an archive, author or story row and **null on a tag-feed row**: a tag feed's
payload does not contain them. It is the site, not a failed read. The
`data_source` column on every row says which view built it — `obvinit`,
`apollo`, `jsonld`, `dom`, or a `+`-joined combination.

Two more read paths sit behind those: the page's JSON-LD, and the rendered
cards. Both are fallbacks and both are exercised by the offline suite.

### Columns

`source` `scraped_at` `url` `sku` `title` `subtitle` `canonical_url`
`author` `author_username` `author_url` `publication` `publication_url`
`published_at` `updated_at` `claps` `responses` `reading_time_min`
`word_count` `is_paywalled` `is_series` `language` `tags`
`preview_image_url` `content` `content_chars` `data_source` `page` `position`

`sku` is Medium's own 12-hex post id. It survives a story being renamed,
re-slugged or moved into a publication; none of the URLs do.

`url` is the story's address **on Medium**. It is deliberately not the
canonical: Medium's canonical for a cross-posted story points at another site
entirely — 4 of the 20 entries in one tag feed's JSON-LD pointed at
`habr.com` and `dev.to`. Where the canonical differs it is kept in its own
`canonical_url` column.

---

## Traps that look like bugs

**A publication home page is refused, on purpose.** `medium.com/better-programming`
and `medium.com/data-science-collective` ship 167 and 51 `Post` nodes
respectively, and every one of them is `{__typename, id}` — **no post data**:
no title, no author, no date. Rows built from that would carry a sku and 26
nulls while the run reported success. The scraper refuses the URL and names
the alternative: run `--mode archive` on a tag the publication writes under,
or `--mode post` on the story URLs.

**`?page=2` on a tag feed does not fail — it is ignored.** The feed comes
back with its first stories again. Only the day archive has real per-page
addresses, which is why `--pages` and `--concurrency` do something there and
are refused (with the reason) everywhere else.

**A day with no stories redirects up to its month.** It does not 404. The
month view is the other renderer holding a different set of stories, so the
scraper compares the landed URL against the requested one and stops the walk
rather than attributing one period's stories to another.

**Scrolling adds nothing from a refused address.** The feed extends over a
POST to `medium.com/_/graphql`, and all 11 of those came back 403 while the
page itself came back 200. A tag feed scrolled twelve times to a stable
height held exactly the 34 stories its first response shipped. Use
`--mode archive` for volume.

**An author page's rendered card count can drop to zero while you scroll.**
Medium replaces that feed's DOM. It costs a run nothing, because the rows
come from the page's payload rather than from the cards.

**towardsdatascience.com is not a Medium site any more.** It moved to
WordPress; its pages carry no Medium payload at all. The scraper refuses it
with that reason rather than with "is not a Medium site", which would be
false and would send you looking for a typo.

**Medium ships reCAPTCHA markup on every page it serves**, for its own
sign-in widget — 3 to 4 `g-recaptcha` occurrences on pages known to be good.
It is not a challenge and this scraper does not treat it as one.

---

## What Medium does when it refuses

Two different pages, and only one of them is worth retrying:

* **the WAF refusal** — HTTP 403, 5 KB, "Sorry, you have been blocked".
  Nothing to solve and nothing to wait for. This is what a `HeadlessChrome`
  User-Agent gets.
* **the managed challenge** — HTTP 403, 28 KB, "Just a moment…",
  `cType: 'managed'`. Transient: 9 of 27 first attempts got it and **all 9
  were served in full on the next attempt in a fresh browser context**.
  Waiting inside the same context never cleared one, so `--retries` opens a
  new context between attempts.

Neither is solvable. A managed challenge publishes **no sitekey** — 0
`data-sitekey` attributes and 0 iframes on the one measured — so there is
nothing to hand a captcha solver, and this scraper never spends a solve on
it. Nothing is ever charged for a block here.

---

## Do I need to pay for anything?

**No.** Every number on this page was taken with Playwright's own bundled
Chromium, from an ordinary datacentre address, with no 2Captcha key and no
proxy.

What the paid [2Captcha](https://2captcha.com) products buy is the thing that
actually limits a long run: **rate**. An archive walk pulls 2.3 MB per day
URL, and one address went from serving a whole day to the hard WAF refusal
inside three of them. `--delay` is the cheaper lever; `--proxy-file` is the
one that scales.

Captcha solving specifically buys nothing here — see above.

---

## Engines

| engine | install | notes |
|---|---|---|
| **Playwright** | `requirements-playwright.txt` | primary. The only engine with the `--concurrency` worker pool. |
| **Selenium** | `requirements-selenium.txt` | drives the installed Chrome. Cannot use `--cdp-endpoint` (chromedriver's `debuggerAddress` takes a bare `host:port` with nowhere for a password) and cannot authenticate a proxy. |
| **pyppeteer** | `requirements-puppeteer.txt` | **pass `--chromium-path`.** Its bundled Chromium is build 117.0.5938.0, two years old, and because the User-Agent is built from the browser's own version the engine truthfully announces `Chrome/117` — refused 3 times out of 3. Pointed at the installed Chrome it returned 53 rows first try. |
| **Scraper API** | `scraper_api_client.py` | 2Captcha's hosted browser over HTTP. |

**Install exactly one.** The three declare mutually unsatisfiable pins
(`pyee` <12 vs ≥13, `urllib3` <2.0 vs ≥2.6). Use a virtualenv per engine if
you need more than one.

`--concurrency` above 1 is implemented in the Playwright engine only, and
only for `--mode archive`, where the day URLs are independent addresses.
Selenium and pyppeteer walk the same days one at a time and produce
identical output and exit codes.

---

## Exit codes

| code | meaning |
|---|---|
| 0 | ok |
| 1 | crash |
| 2 | bad usage |
| 3 | blocked |
| 4 | zero stories |
| 5 | remote API error |
| 6 | partial run |

A run that finds nothing **writes nothing** — last night's good output is not
replaced with `[]`. Pass `--allow-empty` if an empty result is the expected
outcome.

Every run writes `<out>.meta.json` beside its output with `status`,
`stop_reason`, which pages failed by number, and the scroll trace. A failed
run writes no sidecar, so a `"failed"` sidecar never sits beside good data.

`diff_runs.py` compares two runs by `sku` and refuses to compare runs that
are not both complete, or that used different modes.

---

## Configuration

Credentials go in `.env` next to the scripts, never on a command line — a
secret in `argv` is readable by anything that can run `ps`.

```bash
cp .env.example .env
python3 env_config.py     # prints what was picked up, WITHOUT secrets
```

Precedence, highest first: an explicit flag → an exported environment
variable → `.env` → the default.

---

## Tests

```bash
python3 smoke_test.py     # offline, no engine library required
pytest                    # the same checks, wrapped
```

The fixtures are cut from real captures by `make_fixtures.py`, which proves
each one parses identically to its untrimmed original, column for column, and
replaces every real author handle with a pseudonym before anything is
written.

## Licence

MIT. See [LICENSE](LICENSE).
