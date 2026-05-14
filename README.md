# Medium.com Scraper

A free, open-source Python scraper for [Medium.com](https://medium.com) — extract articles by tag, author profiles, publications, and full post content. Supports Playwright, Selenium, and Pyppeteer. Built and maintained by [2scraper](https://github.com/2scraper).

## Features

- **4 scraping modes:** tag feeds, author profiles, publications, individual posts
- **3 browser engines:** Playwright (recommended), Selenium, Pyppeteer
- **GraphQL interception:** captures Medium's internal API responses for structured data
- **Multi-layer fallback:** GraphQL → `__APOLLO_STATE__` JSON → DOM extraction
- **Paywall support:** optional `--auth` flag with session cookie persistence
- **Anti-bot evasion:** stealth fingerprinting, residential proxy support
- **CAPTCHA solving:** built-in [2captcha.com](https://2captcha.com) integration
- **Output formats:** JSON and CSV
- **Debug mode:** saves screenshots and raw HTML for troubleshooting

## Scraped Fields

| Field | Description |
|-------|-------------|
| `id` | Medium post ID |
| `title` | Article title |
| `subtitle` | Article subtitle / preview |
| `url` | Direct link to post |
| `author_name` | Author display name |
| `author_username` | Author @handle |
| `author_url` | Link to author profile |
| `claps` | Total clap count |
| `responses` | Number of comments |
| `reading_time` | Estimated reading time (min) |
| `published_at` | Publication timestamp |
| `tags` | List of topic tags |
| `is_paywalled` | Whether the post is behind paywall |
| `preview_text` | Article preview / excerpt |
| `content` | Full article text (post mode only) |

## Installation

```bash
git clone https://github.com/2scraper/medium-scraper.git
cd medium-scraper
pip install -r requirements.txt

# Playwright only — install browser
playwright install chromium
```

## Quick Start

### Scrape articles by tag
```bash
python medium_playwright.py --mode tag --target python --limit 50
```

### Scrape an author's posts
```bash
python medium_playwright.py --mode author --target @towards-data-science --limit 30
```

### Scrape a publication
```bash
python medium_playwright.py --mode publication --target towards-ai --limit 40
```

### Scrape a single post (full content)
```bash
python medium_playwright.py --mode post --target "https://medium.com/p/abc123"
```

### Export to CSV
```bash
python medium_playwright.py --mode tag --target python --format csv
```

### Export both JSON and CSV
```bash
python medium_playwright.py --mode tag --target python --format both
```

## Options

```
--mode        tag | author | publication | post
--target      Tag name, @username, publication slug, or post URL
--limit       Max posts to collect (default: 25)
--output      Output file path (auto-named if omitted)
--format      json | csv | both (default: json)
--proxy       Proxy URL: http://user:pass@host:port
--apikey      2captcha.com API key
--auth        Enable auth for paywall access
--session-file  Cookie session file (default: session.json)
--headed      Run browser in visible mode
--debug       Save debug screenshots + HTML to debug/
```

## CAPTCHA Solving

Medium occasionally presents CAPTCHAs. This scraper integrates with [2captcha.com](https://2captcha.com) for automatic resolution:

```bash
# Pass API key via flag
python medium_playwright.py --mode tag --target python --apikey YOUR_KEY

# Or via environment variable
export APIKEY_2CAPTCHA=YOUR_KEY
python medium_playwright.py --mode tag --target python
```

2captcha supports reCAPTCHA v2/v3, hCaptcha, Cloudflare Turnstile, and 10+ other types.

## Proxy Usage

Medium blocks all datacenter IP ranges. Use residential proxies for reliable scraping:

```bash
python medium_playwright.py --mode tag --target python \
    --proxy "http://user:pass@gate.2prx.com:10000"
```

[2prx.com](https://2prx.com) offers residential and mobile proxies optimized for web scraping.

## Authentication (Paywall Access)

Medium allows ~3 free article reads per month. For unrestricted access:

```bash
# First run — opens browser for manual login
python medium_playwright.py --mode tag --target python --auth --headed

# Subsequent runs — reuses saved session
python medium_playwright.py --mode tag --target python --auth
```

Session cookies are stored in `session.json` (gitignored by default).

## Anti-Detect Browser

For large-scale scraping that requires advanced fingerprint management, consider using a dedicated **anti-detect browser** with unique browser profiles per session. This prevents cross-session fingerprint correlation and dramatically reduces detection rates.

→ [Learn more about anti-detect browser](https://2captcha.com/anti-detect-browser)

## Choosing a Scraper

| Engine | Pros | Cons |
|--------|------|------|
| **Playwright** | Fastest, best async support, networkidle | Requires `playwright install` |
| **Selenium** | Most widely used, easy setup | No native networkidle, slower |
| **Pyppeteer** | Node-Puppeteer-compatible API | Less maintained, occasional install issues |

## Debug Mode

```bash
python medium_playwright.py --mode tag --target python --debug --headed
```

Saves to `debug/`:
- `tag_python.png` — screenshot
- `tag_python.html` — raw page HTML

## Output Examples

### JSON (tag mode)
```json
[
  {
    "id": "abc123def456",
    "title": "Building Production-Grade ML Pipelines",
    "subtitle": "A practical guide to MLOps with Python",
    "url": "https://medium.com/p/abc123def456",
    "author_name": "Jane Smith",
    "author_username": "janesmith",
    "author_url": "https://medium.com/@janesmith",
    "claps": 1842,
    "responses": 34,
    "reading_time": 12,
    "published_at": "2024-03-15T09:00:00.000Z",
    "tags": ["python", "machine-learning", "mlops"],
    "is_paywalled": false,
    "preview_text": "When building ML systems that actually ship..."
  }
]
```

### JSON (author mode)
```json
{
  "author": {
    "name": "Towards Data Science",
    "username": "towards-data-science",
    "bio": "Your home for data science and AI.",
    "followers": 692000,
    "url": "https://medium.com/@towards-data-science"
  },
  "posts": [ ... ]
}
```

## License

MIT © [2scraper](https://github.com/2scraper)

---

**Related tools:**
- [2captcha.com](https://2captcha.com) — CAPTCHA solving API (reCAPTCHA, hCaptcha, Turnstile, and more)
- [2prx.com](https://2prx.com) — Residential & mobile proxies for scraping
- [Anti-detect browser](https://2captcha.com/anti-detect-browser) — Browser fingerprint management
