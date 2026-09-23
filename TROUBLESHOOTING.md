# Troubleshooting

Every number here is measured, with the date it was measured on. If a section
does not name a number, it is telling you what to look at rather than what to
expect.

---

## "curl gets HTTP 403 but the scraper works" — and the other way round

The two are the same fact seen from opposite sides, and it is the most
useful thing to know about this site.

What Medium's Cloudflare **hard-blocks** is the `HeadlessChrome` token in the
User-Agent. Measured 2026-09-16 from one datacentre address (a netcup host in
Nuremberg), all within a few minutes of each other:

| what sent the request | WAF-blocked | served |
|---|---|---|
| `curl` | yes | — |
| headless Chromium, **default UA** | **11 of 11** | 0 |
| headless Chromium, ordinary Chrome UA | **0 of 11** | 6 of 8 in a paced run |
| headful Chromium, ordinary Chrome UA | 0 of 8 | **8 of 8** |

These engines build the User-Agent from the browser's own version and never
send that token, which is why they get data from an address `curl` gets
nothing from. If you are testing this site by hand and getting 403 on
everything, that is almost certainly why.

It also means **pyppeteer needs `--chromium-path`**: its bundled Chromium is
build 117.0.5938.0, and because the UA follows the browser's real version the
engine truthfully announces `Chrome/117`, which was refused 3 times out of 3.
Point it at an installed Chrome and the same engine returned 53 rows on the
first attempt.

---

## "It returns HTTP 403 / `Just a moment...` / exit 3"

Two different pages, and only one of them is worth doing anything about.
Check which one you got — `--dump-html` writes exactly what the parser saw:

| page | size | what it says | what to do |
|---|---|---|---|
| the **WAF refusal** | ~5 KB | "Sorry, you have been blocked" | see the section above; usually the UA, otherwise the address's rate |
| the **managed challenge** | ~28 KB | "Just a moment…", `cType: 'managed'` | retry in a fresh context — which `--retries` already does |

The managed challenge is **transient and it clears**: 9 of 27 first attempts
got it and all 9 were served in full on the next attempt in a **fresh browser
context**. Waiting inside the same context never cleared one, so a reload is
the wrong response and `--retries 3 --retry-delay 30` is the right one — the
engines relaunch the context between attempts for exactly this reason.

**Do not buy a captcha solve for either.** A managed challenge carries no
sitekey at all — measured 0 `data-sitekey` attributes and 0 iframes, with
`cType: 'managed'` in Cloudflare's own config. There is nothing to hand
2Captcha, so this scraper never spends a solve on a block and nothing is ever
charged for one.

**What does exhaust an address is volume.** A day archive is 2.3 MB, and one
address went from serving a whole day to the hard WAF refusal inside three of
them. `--delay 20` or more is the cheap lever on an archive walk;
`--proxy-file` is the one that scales.

---

## "The reading time / word count / language column is empty"

Because you ran `--mode tag`, and that is the site rather than a failed read.

Medium serves the same catalogue through two renderers with different field
sets:

| column | `--mode tag` | `--mode archive` | `--mode author` | `--mode post` |
|---|---|---|---|---|
| `claps`, `responses` | yes | yes | yes | yes |
| `reading_time_min` | **no** | yes | yes | yes |
| `word_count` | **no** | yes | **no** | yes |
| `language` | **no** | yes | **no** | yes |
| `content` | no | no | no | yes |

A tag feed's payload carries 21 fields per story; a day archive's carries 84.
The `data_source` column on every row says which view built it — `obvinit` is
the rich one.

If you want those columns, use a day archive:

```bash
python3 playwright_scraper.py \
  --url "https://medium.com/tag/python/archive/2026/09/10" --pages 5
```

---

## "`?page=2` returns the same stories"

Because it is **ignored**, not because it failed. Medium publishes no
per-page address for a tag feed, an author page or a story page — the feed
extends over a POST to `medium.com/_/graphql`, and from a refused address all
11 of those came back 403 while the page itself came back 200.

`page_url()` returns `None` for those modes rather than building a URL that
would look like it worked: a run that trusted `?page=2` would add no story it
had not already seen, conclude the listing was exhausted, and report a
**complete** run holding one page.

The one mode with real addresses is the day archive, and `--pages N` there
walks N days backwards.

---

## "It says it is not walking further days"

A tag day with no stories does **not** 404. It redirects up to its month
view, which is the other renderer holding a different set of stories — HTTP
200 and a page full of rows, for a day that has none. Measured on
`/tag/日本語/archive/2026/09/10`, which landed on `/archive/2026/09`.

The scraper compares the landed URL against the requested one and stops the
walk with `stop_reason: pagination_redirected` rather than attributing one
period's stories to another. Pick a day the tag actually published on, or a
busier tag.

---

## "A publication URL is refused"

On purpose, and the refusal names the measurement:
`medium.com/better-programming` and `medium.com/data-science-collective` ship
167 and 51 `Post` nodes respectively, and every one of them is
`{__typename, id}` — no title, no author, no date. Rows built from that would
carry a sku and 26 nulls while the run reported success.

Two things that do work:

```bash
# the stories a publication runs, through a tag it writes under
python3 playwright_scraper.py --url "https://medium.com/tag/programming/archive/2026/09/10"

# one story, by its id
python3 playwright_scraper.py --url "https://medium.com/p/d373fe2c96b7"
```

---

## "`--concurrency 4` says it is refused"

Because the URL you gave it has no per-page address. A tag feed, an author
page and a story page are one fetch each, and batch 2 exists only inside the
browser that scrolled through batch 1 — there is nothing to hand a second
worker.

It is accepted for **`--mode archive`**, where the day URLs are independent
addresses:

```bash
python3 playwright_scraper.py \
  --url "https://medium.com/tag/python/archive/2026/09/10" \
  --pages 6 --concurrency 3 --delay 25
```

Two caveats there:

- **The worker pool is implemented in the Playwright engine only.** Selenium
  and pyppeteer walk the same days one at a time and produce identical rows,
  exit codes and metadata; they say so rather than running one worker
  quietly.
- **Every worker is a real browser window.** Three workers is three windows
  and roughly 600 MB. On a headless machine pass `--headless`, which works
  here (see the first section) at a small measured cost.

Without a proxy pool, N workers all leave from ONE address — and an archive
walk already exhausts an address quickly. Pass `--proxy-file`, or keep the
concurrency low and the `--delay` high.

With `--cdp-endpoint` concurrency is refused outright: a Scraping Browser
profile allows one live connection, so workers would collide. Use several
`pid`s, one run each.

---

## "The run says `complete` but I expected more stories"

Four different things, and the sidecar tells them apart.

**`tag_total_posts` is enormous next to the row count.** Expected, and it is
not a gap. That figure is the size of the tag's WHOLE catalogue since 2003 —
257,497 for `python` — recorded beside the run rather than subtracted from
it. `page_gap` is `None` on this site for exactly this reason.

**`stop_reason` is `no_new_products` after one page, in tag, author or post
mode.** That is the whole mode. Those feeds are one fetch and the scroll adds
nothing, so `complete` is honest. Use `--mode archive` for volume.

**`stop_reason` is `pagination_redirected`.** The day you asked for has no
stories and the site redirected up to its month. See the section above.

**`status` is `partial` (exit 6).** A day in the walk was refused while
earlier ones succeeded. The sidecar names which pages failed BY NUMBER, so a
re-run can target them. That is rate limiting rather than the end of the
listing, and the run says so rather than claiming completeness — raise
`--delay`.

---

## "A column is 100% populated and wrong"

The most expensive bug class in this family, so here is what to check first
on this site. The one that was nearly shipped:

- **`claps` reading `virtuals.recommends` instead of
  `virtuals.totalClapCount`.** Both sit in the same object, both are
  populated on 128 of 128 archive stories, and the first is the retired
  pre-2017 recommend count — 25 against 248 on the same story. A coverage
  check says 100% either way. `smoke_test.py` pins the real number on a real
  fixture for exactly this reason.
- **`url` pointing at another site.** Medium's canonical for a cross-posted
  story is external — 4 of 20 on one tag feed pointed at `habr.com` and
  `dev.to`. `url` must be the Medium address and `canonical_url` the other
  one; if they are swapped, every other column still looks right.
- **`author` being the story's own title.** A card links its story from an
  `/@handle/...` href, which is also the shape of an author link. The parser
  skips any `/@` link that carries a story id for this reason.
- **`published_at` null on archive rows only.** The legacy payload ships the
  epoch as a STRING where the modern one ships an int. A converter that
  accepts only the int leaves that column null on a whole mode while the
  other modes look fine.

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
handles — with pseudonyms before anything is written.

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

## "Which host should I point it at?"

Any of them. Unlike several sites in this family, Medium is **one catalogue**
— `/tag/python` and `/tag/日本語` are the same host and the same index, and
the story's own language lands in the `language` column rather than in the
hostname. There is deliberately no `--language` flag, because it could only
disagree with the URL.

What does vary is which address SERVED a row, and `source` records it:

- `medium.com` — the site itself.
- `{username}.medium.com` — an author's own subdomain, which Medium mints
  automatically. Most stories on a tag feed are served from one of these.
- a publication's custom domain — `python.plainenglish.io` and several
  thousand others. These cannot be enumerated, so the scraper accepts any
  host and then CHECKS the page: one that carries neither payload and none of
  Medium's asset hosts is classified `blocked` rather than parsed.

One host is refused by name: **towardsdatascience.com**, which left Medium
and now runs on WordPress — HTTP 200, 25 `<article>` elements, two JSON-LD
blocks, zero Medium payload and 7 `wp-content` references. It is refused with
that reason rather than with "is not a Medium site", which would be false and
would send you looking for a typo.

`diff_runs.py` notes when two runs landed consistently on different hosts,
but does not refuse the pair: on this site that is usually a different URL
for the same catalogue rather than a different catalogue.
