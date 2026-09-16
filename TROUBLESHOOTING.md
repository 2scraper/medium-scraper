# Troubleshooting

Every number here is measured, with the date it was measured on. If a section
does not name a number, it is telling you what to look at rather than what to
expect.

---

## "It returns HTTP 403 / `Just a moment...` / exit 3"

Cloudflare's **managed** challenge. It is the only refusal this site has been
observed serving, and two measured facts decide what to do about it.

Measured 2026-09-15, one DATACENTRE address (a netcup host in Nuremberg),
about forty-five fetches over three hours:

| window | fetches | served | challenged |
|---|---|---|---|
| first ~20 minutes | 14 | 8 | 6 |
| everything after | ~30 | **0** | **all of them** |

Three things follow:

- **A rested address recovers.** Early in that window the same URL that
  answered 403 answered 200 with the full feed on the next attempt about a
  minute later, from the same address and the same browser. So the first
  thing to try is `--retries 4 --retry-delay 40`.
- **A busy address does not, and it does not recover quickly.** After about
  thirty fetches every subsequent attempt was challenged, and a **45-minute
  rest did not clear it** — nine further attempts across the next twenty
  minutes were all refused. Nothing about that address changed except how
  much it had been fetching.
- **The score follows the ADDRESS, not the language site.** `medium.com`
  refused the same address at the same moment `medium.com` did, with three
  attempts each. Switching hosts is not a way around it.

So `--delay` is the cheapest lever, `--proxy-file` is the one that scales, and
"it worked an hour ago" is not evidence that it will work now.

**Do not buy a captcha solve for this.** A managed challenge carries no
sitekey — 0 `data-sitekey` attributes and 0 Turnstile iframes on the page
measured — so there is nothing for a solver to answer. This scraper never
attempts one on that state and never charges you.

If a run exits 3, the interstitial is saved beside your output as
`<out>_page<N>_debug.html` with a screenshot next to it.

> **Note for readers of the sibling repos in this family:** the thing that
> decides whether *this* site answers you is the address's recent request
> rate, not the browser build. Playwright's bundled Chromium was served the
> full feed. `playwright install chrome` is not the fix here.

---

## "A topic run has no upvotes, views or creation times"

Working as measured. A topic page's inlined GraphQL payload carries **0
answer objects** — its answers arrive over a later XHR — so every topic row is
built from the rendered card alone, and the card shows an anonymous reader no
counts at all.

Check the `data_source` column:

| value | what the row has |
|---|---|
| `dom+inline` | everything |
| `inline` | everything; the card did not render this one (a machine answer) |
| `dom` | title, author, credential, body **truncated to three lines**, date text |

**Point it at a question or a profile URL** if you want the counts. Same rows,
different view:

```bash
python3 playwright_scraper.py --url "https://medium.com/What-is-machine-learning-4" --pages 3
```

Every run logs its payload coverage per batch and over the merged run.

---

## "`?page=2` returns the same answers"

It does, and that is the trap this repo is built around. Medium ignores a page
parameter in every mode — it does not error, it just serves the feed's first
items again. A scraper built on it would find no new `sku`, conclude the
listing was exhausted and report a **complete** run holding one batch.

`--pages N` here means **N scroll batches**. Read `rows_new_per_batch` in the
sidecar rather than the raw row count: each batch re-parses the whole feed, so
most of what a later batch yields is a duplicate by construction.

---

## "`--concurrency 4` says it is refused"

Because batch 5 exists only inside the browser that scrolled through batches
1-4. There is no address to hand a second worker.

Run several feeds in parallel instead, one process each:

```bash
for t in Machine-Learning Physics Chemistry; do
  python3 playwright_scraper.py --url "https://medium.com/topic/$t" \
    --pages 3 --out "$t" &
done
wait
```

With `--cdp-endpoint` keep them on **different `pid`s** — a Scraping Browser
profile allows one live connection.

---

## "The run says `complete` but I expected more answers"

Two different things, and the sidecar tells them apart.

**`answers_available` is much larger than the row count.** Normal. A question
page renders twelve answers and reports 253; Medium extends it on scroll. That
number sits *beside* the row count rather than being subtracted from it,
precisely so it is not read as a gap.

**`stop_reason` is `no_new_products` after one batch, and the row count is a
handful.** The SESSION was served a short feed. Measured: four loads of one
question URL gave 5, 13, 12 and 259 answers, and in the short ones the scroll
reached the bottom of the document and Medium never fetched more. Raising
`--pages` does not help and the run says so. **Re-run it** — a fresh browser
re-rolls the variant.

**`stop_reason` is `no_new_products` on a profile.** Usually the real end: a
profile has as many answers as its author wrote.

**`stop_reason` is `next_batch_refused` and the status is `partial`
(exit 6).** The batch behind the scroll was refused by the site while the HTML
kept answering 200. That is rate limiting, not the end of the listing, and the
run says so rather than claiming completeness. Raise `--delay`.

---

## "A column is 100% populated and wrong"

The most expensive bug class in this family, so here is what to check first on
this site:

- **`title` is an answer body rather than a question.** Medium stores every
  string as a rich-text JSON document; if `render_rich_text` is handed the
  wrong node you get plausible prose in the wrong column. The question is the
  card's `.puppeteer_test_question_title`, or the page's `h1` on a question
  page.
- **`author` is empty on every row.** The FIRST `/profile/` link in a card is
  the avatar and has no text. The parser takes the first one that does.
- **`created_at` is in the year 57 million.** Medium's timestamps are epoch
  MICROSECONDS. Read as seconds or milliseconds they still parse.
- **`question_url` is one character off.** On a Space or Session subdomain the
  permalink is the question's path plus a per-answer suffix. The payload's own
  `question.url` is authoritative; without it the permalink stands, and that
  is documented rather than guessed at.

`--dump-html PATH` writes the exact bytes the parser was given, on success as
well as failure.

---

## "The tests pass but a live run is broken"

That is the normal shape of a site-side change, and it is why `canary.yml`
exists. To refresh the offline fixtures against the current site:

```bash
python3 playwright_scraper.py --url "..." --pages 2 --dump-html capture
# move the dumps into ../captures/ with the names make_fixtures.py expects
python3 make_fixtures.py
python3 smoke_test.py
```

`make_fixtures.py` refuses to write a fixture that does not parse identically
to its untrimmed original, so a bad trim fails loudly rather than pinning the
wrong behaviour. It also replaces the people in a capture — names, profile
slugs, credentials, answer bodies, cited links — before anything is written.

---

## "`--cdp-endpoint` says `profile_locked`"

Almost certainly something took the profile, and on the evidence gathered in
this family the likeliest candidate is a plain HTTP request to the endpoint —
including the "harmless" check you might reach for to see whether it is free.

Three profiles, measured on a sibling site 2026-09-14:

| what was done first | result |
|---|---|
| `GET /json/version` (answered `200`), then WebSocket | wedged — `profile_locked` on both, **never cleared in 40 min** |
| `GET /json/version` (answered `200`), then WebSocket | wedged — locked on the first WebSocket attempt, still locked after 4 min of silence |
| **WebSocket only, no HTTP at all** | **connected in ~3s**, and the pid was reusable by the next run |

So: **do not poll the HTTP endpoint to check whether a profile is free.** That
a GET claims the profile is not proven, but three for three is a strong enough
pattern to stop doing it — and there is no need to, because the connection you
actually want tells you the same thing in one step.

Close sessions cleanly and the pid stays reusable. If one is genuinely wedged,
nothing on the client side frees it — use a different `pid` or reset it from
the 2Captcha dashboard.

### What `--cdp-connect-timeout` is and is not for

The default is **150s**, up from the 30s this repo family shipped, because the
server's own give-up point was measured at 121s and a client that quits first
quits while the server is still working.

It is **not** a cure for `profile_locked` — one of the profiles above locked
instantly, with no timed-out connect anywhere in its history.

---

## "My proxy is SOCKS5 and the run dies at launch"

```
BrowserType.launch: Browser does not support socks5 proxy authentication
```

A Chromium limitation rather than anything this repo does: Chromium accepts an
**unauthenticated** SOCKS5 proxy (`socks5://host:port`) and refuses an
authenticated one outright. Selenium is worse — it cannot authenticate *any*
proxy.

Three ways out, best first:

1. **2Captcha's IP-whitelist mode.** Whitelist your address and ask
   `/proxy/generate_white_list_connections` for connections; you get one
   `host:port` per exit, which drops straight into `--proxy-file` and works
   in every engine including Selenium.

   One caveat, measured here on 2026-09-15 rather than assumed: a set of ten
   such connections spoke **SOCKS5 and still demanded a username and
   password** — offered no-auth alone they replied `0xFF`, "no acceptable
   method", and offered user/pass they took it and then rejected the
   account's own credentials. So "whitelisted" did not mean "no credentials"
   on that account, and the HTTP form of the same `host:port` did not answer
   at all. If yours behave the same way, the credentials are still needed and
   the whitelist only decides whether your source is allowed to ask.
2. **Ask for an HTTP endpoint instead.** `http://user:pass@host:port` works in
   Playwright and pyppeteer, which pass credentials through the driver's own
   fields rather than the command line.
3. **A local relay**, if you are stuck with a credentialled SOCKS5 string: a
   small unauthenticated HTTP `CONNECT` listener on `127.0.0.1` that dials the
   authenticated SOCKS5 upstream. This also keeps the credentials off the
   browser's command line, which is what this project's own rules want anyway.
   It is deliberately not shipped here.

---

## "Which language site should I use?"

The one that has the answers. They are twenty-four different catalogues: a
Spanish question lives on `medium.com` with its own slug and is not a
translation of anything on `www`.

`diff_runs.py` refuses to compare two of them for exactly that reason.

Two things about the host list are worth knowing:

- **Japanese is on `jp.medium.com`, not `ja.`** — the one host that is a
  country code where every other is a language code.
- **`help.medium.com`, `contests.medium.com` and the blogs are not language
  sites** and are refused with that as the message, rather than with "not a
  Medium site", which would send you looking for a typo.

A Space or a Medium Session lives on its own `{name}.medium.com` and is
supported: answers on those hosts appear in feeds and are read normally. Their
`language` column is null, because those hosts are not language sites.
