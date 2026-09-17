# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/) as closely
as a CLI toolkit can. Read a PATCH as "fixes", not as "no flag moved": where a
default changes because a measurement said it should, that is called out at
the top of the release rather than left to be discovered from a bill or an
empty output file.

## [0.1.1] — 2026-09-16

> **Correction to v0.1.0.** That release's README said of Medium's two
> refusal pages: *"Neither is solvable … there is nothing to hand a captcha
> solver."* The first half is true — the WAF refusal carries no widget. The
> second is not: a Cloudflare **managed challenge** publishes no sitekey *in
> the markup*, but Cloudflare passes `sitekey`, `action`, `cData` and
> `chlPageData` to `turnstile.render()` once, those arguments can be captured
> with an init script, and 2Captcha solves the result as
> `TurnstileTaskProxyless`. The sentence told readers a 2Captcha key would
> not help them here, which is a claim about the product and was wrong. What
> is true is narrower and is now what the README says: **this repo does not
> implement that interception**, because the challenge here is transient and
> a retry in a fresh context cleared 9 of 9 measured.

### Fixed

- **README no longer claims a captcha cannot be solved.** The measurements
  are unchanged — 0 `data-sitekey` attributes and 0 iframes on the managed
  challenge, 9 of 27 first attempts challenged, all 9 served on the next
  attempt in a fresh context. Only the conclusion drawn from them changed,
  from "not solvable" to "not implemented here, and here is why that is the
  cheaper choice on this site".
- **The reCAPTCHA Enterprise paragraph** now says plainly that this repo does
  not implement `RecaptchaV2EnterpriseTaskProxyless`, rather than leaving a
  reader to infer that nothing could be done. Medium ships the loader on
  every page and has never rendered a challenge to answer.
- **`test_challenge_is_not_solvable` is renamed**
  `test_a_challenge_is_never_paid_for_here`. Its assertions are unchanged and
  still correct; the name asserted something about 2Captcha that the test
  never measured.

### Added

- **`test_captcha_capability_claims_match_the_code`** — a guard in both
  directions. It fails the build on a documented conclusion that a captcha
  cannot be solved, and equally on a README that claims this repo solves
  Turnstile when the task type or the `turnstile.render` interception is
  absent. Verified by reverting the README sentence: the check goes red.

## [0.1.0] — 2026-09-16

First release. A complete rewrite: the repository previously held three
standalone scripts with no shared row model, no run metadata, no exit-code
contract and no tests. Nothing of that survives except the licence.

### Added

- **Four modes, one row shape.** `--mode tag`, `--mode archive`,
  `--mode author`, `--mode post`, inferred from the URL. All four yield the
  `Post` dataclass with the family's `source / scraped_at / url / sku / title`
  prefix, in JSON and CSV with identical column order.
- **Both of Medium's renderers are read.** The modern `__APOLLO_STATE__` and
  the legacy `window["obvInit"]` that a tag's DAY ARCHIVE ships — 84 fields
  per story and up to 128 stories in one fetch, against 21 fields and 34
  stories on a tag feed. Two further fallbacks behind those: the page's
  JSON-LD and its rendered cards. The `data_source` column on every row says
  which view built it.
- **Addressable pagination for the one mode that has it.**
  `/tag/{slug}/archive/{yyyy}/{mm}/{dd}` is walked backwards a day at a time,
  and `--concurrency` runs those days across workers in the Playwright
  engine. Verified before it is planned: a day with no stories redirects up
  to its month view, which is a different renderer, and the walk stops rather
  than attributing one period's stories to another.
- **Four engines**: Playwright (primary), Selenium, pyppeteer and 2Captcha's
  Scraping Browser over CDP, agreeing on rows, exit codes and run metadata.
- **Run metadata** in `<out>.meta.json` — status, stop reason, which pages
  failed by number, the scroll trace, and the size of the tag's whole
  catalogue where Medium states it.
- `diff_runs.py`, which compares two runs by `sku` and refuses a pair whose
  modes differ or which are not both complete.
- An offline suite of over 700 checks with fixtures cut from real captures by
  `make_fixtures.py`, which proves each fixture parses identically to its
  untrimmed original — column for column — and replaces every real author
  handle with a pseudonym before anything is written to disk.

### Measured, and worth knowing before you run it

- **What Cloudflare hard-blocks here is the `HeadlessChrome` User-Agent
  token, not headless browsing.** A bare headless Chromium sending its
  default UA was refused 11 times out of 11 with a 5 KB WAF page; the same
  browser with an ordinary Chrome UA — which these engines build from the
  browser's own version — was never WAF-blocked. Headful remains the default
  on a narrower margin: 8/8 served against headless's 6/8.
- **A publication home page is refused, with the reason.** It carries 167 (or
  51) post IDS and no post data — no title, no author, no date. Rows built
  from it would hold a sku and 26 nulls while the run reported success.
- **A captcha solve buys nothing on this site.** Medium's refusal is
  Cloudflare's managed challenge, which publishes no sitekey and renders no
  iframe. Nothing is ever charged for it; the challenge is retried in a fresh
  browser context instead, which cleared all 9 of the 9 measured.
- **Both paid 2Captcha paths were run and are complete.** The Scraper API
  returned HTTP 200 on all four modes at $0.0005 a request, with rows
  identical to a local browser's — including the whole 2.3 MB archive-day
  payload in one request. The Scraping Browser returned 254 rows over two
  archive days, and **55 rows from an author page against a local browser's
  10**: that feed extends by scrolling, the scroll is refused from an
  ordinary address and is not refused from the Scraping Browser's exit.
- **`--fingerprint` works, and the five defects §16 names are all absent** —
  the user agent reaches the browser, the locale is derived by LANGUAGE
  (`jp` → `ja-JP`, not `jp-JP`), the timezone is applied, `--tags Windows`
  is accepted, and a 401 carries no key. One NEW defect of the same family
  was found and fixed: the on-disk fingerprint cache was keyed on the request
  parameters and not on the API key, so a bogus key returned a cached
  fingerprint and raised nothing. The same code is in every sibling repo and
  is unfixed there.
- **The Scraping Browser's auto-solve extension injects `cf-turnstile` into
  every page it serves** — counted 16 `chrome-extension://` references and 1
  `cf-turnstile` on a 403 KB page holding 60 stories. The extension-script
  filter keeps that classified as content rather than as a challenge; without
  it the run would have reported exit 3 on good data.
- **`reading_time_min`, `word_count` and `language` are null on a tag-feed
  row** and populated on an archive, author or story row, because a tag
  feed's payload does not contain them. That is the site, and `data_source`
  is the column that says so.
- **pyppeteer needs `--chromium-path`.** Its bundled Chromium is build
  117.0.5938.0, and since the User-Agent follows the browser's own version
  the engine truthfully announces `Chrome/117` — refused 3 times out of 3.
  Pointed at an installed Chrome it returned 53 rows on the first attempt.

[0.1.0]: https://github.com/2scraper/medium-scraper/releases/tag/v0.1.0
