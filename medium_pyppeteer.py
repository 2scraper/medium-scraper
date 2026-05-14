"""
Medium.com Scraper — Pyppeteer
================================
Scrapes articles by tag, author profiles, and publications.

GitHub : https://github.com/2scraper/medium-scraper
License: MIT

Usage examples
--------------
# Scrape articles by tag
python medium_pyppeteer.py --mode tag --target python --limit 50

# Scrape author profile
python medium_pyppeteer.py --mode author --target @towards-data-science --limit 30

# Scrape publication
python medium_pyppeteer.py --mode publication --target towards-ai --limit 40

# Single post
python medium_pyppeteer.py --mode post --target "https://medium.com/p/abc123"

# With proxy + 2captcha
python medium_pyppeteer.py --mode tag --target python \
    --proxy "http://gate.2prx.com:10000" \
    --proxy-user "user" --proxy-pass "pass" \
    --apikey "YOUR_2CAPTCHA_KEY"

# Auth (for paywall)
python medium_pyppeteer.py --mode tag --target python --auth --session-file session.json
"""

import asyncio
import argparse
import json
import csv
import os
import re
import sys
import time
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------
try:
    import pyppeteer
    from pyppeteer import launch
    from pyppeteer.errors import TimeoutError as PPTimeout
except ImportError:
    sys.exit("pyppeteer not installed. Run: pip install pyppeteer")

try:
    import twocaptcha
    from twocaptcha import TwoCaptcha
    HAS_2CAPTCHA = True
except ImportError:
    HAS_2CAPTCHA = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_URL = "https://medium.com"
DEFAULT_LIMIT = 25
SCROLL_PAUSE = 2500  # ms
REQUEST_TIMEOUT = 30000  # ms


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def save_json(data, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    count = len(data) if isinstance(data, list) else len(data.get("posts", []))
    print(f"[{ts()}] Saved {count} records → {path}")


def save_csv(data: list, path: str) -> None:
    if not data:
        return
    keys = list(data[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(data)
    print(f"[{ts()}] Saved {len(data)} records → {path}")


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_claps(text: str) -> int:
    if not text:
        return 0
    text = text.strip().replace(",", "")
    if "K" in text.upper():
        return int(float(text.upper().replace("K", "")) * 1000)
    try:
        return int(text)
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# GraphQL Interceptor
# ---------------------------------------------------------------------------
class GraphQLInterceptor:
    def __init__(self):
        self.posts: list[dict] = []
        self.author_info: dict = {}
        self.publication_info: dict = {}

    def _extract_post(self, post: dict) -> dict:
        author = post.get("creator") or {}
        return {
            "id": post.get("id", ""),
            "title": post.get("title", ""),
            "subtitle": post.get("extendedPreviewContent", {}).get("subtitle", ""),
            "url": "https://medium.com/p/" + post.get("id", ""),
            "author_name": author.get("name", ""),
            "author_username": author.get("username", ""),
            "author_url": f"https://medium.com/@{author.get('username', '')}",
            "claps": post.get("clapCount", 0),
            "responses": post.get("postResponses", {}).get("count", 0),
            "reading_time": post.get("readingTime", 0),
            "published_at": post.get("firstPublishedAt", ""),
            "tags": [t.get("normalizedTagSlug", "") for t in (post.get("tags") or [])],
            "is_paywalled": post.get("isPaywalled", False),
            "preview_text": "",
        }

    def process_response(self, body: str) -> None:
        try:
            data = json.loads(body)
        except Exception:
            return

        if isinstance(data, list):
            for item in data:
                self.process_response(json.dumps(item))
            return

        data = data.get("data", data)

        for key in ("tagFeed", "tagFeedItems", "topicFeedItems"):
            feed = data.get(key)
            if feed:
                for item in feed.get("items", []):
                    post = item.get("post") or item.get("feedItem", {}).get("post")
                    if post:
                        self.posts.append(self._extract_post(post))

        for key in ("userResult", "user"):
            user = data.get(key)
            if user:
                self.author_info = {
                    "name": user.get("name", ""),
                    "username": user.get("username", ""),
                    "bio": user.get("bio", ""),
                    "followers": user.get("socialStats", {}).get("followerCount", 0),
                    "url": f"https://medium.com/@{user.get('username', '')}",
                }
                for conn_key in ("profileStreamConnection", "userPostConnection"):
                    conn = user.get(conn_key, {})
                    for edge in conn.get("edges", []):
                        post = (edge.get("node") or {}).get("post") or edge.get("node")
                        if post and post.get("id"):
                            self.posts.append(self._extract_post(post))

        for key in ("collection", "publicationResult"):
            pub = data.get(key)
            if pub:
                self.publication_info = {
                    "name": pub.get("name", ""),
                    "description": pub.get("description", ""),
                    "url": pub.get("domain") or f"https://medium.com/{pub.get('slug', '')}",
                }
                for conn_key in ("postStream", "publicationPostConnection"):
                    conn = pub.get(conn_key, {})
                    for edge in conn.get("edges", []):
                        post = (edge.get("node") or {}).get("post") or edge.get("node")
                        if post and post.get("id"):
                            self.posts.append(self._extract_post(post))


# ---------------------------------------------------------------------------
# Browser builder
# ---------------------------------------------------------------------------
async def build_browser(proxy: str | None, proxy_user: str | None, proxy_pass: str | None, headed: bool):
    launch_args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-http2",
        "--lang=en-US",
        "--window-size=1440,900",
    ]

    if proxy:
        launch_args.append(f"--proxy-server={proxy}")

    browser = await launch(
        headless=not headed,
        args=launch_args,
        ignoreHTTPSErrors=True,
        autoClose=False,
    )

    page = await browser.newPage()

    await page.setViewport({"width": 1440, "height": 900})
    await page.setUserAgent(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    await page.setExtraHTTPHeaders({"Accept-Language": "en-US,en;q=0.9"})

    # Stealth
    await page.evaluateOnNewDocument(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )

    # Proxy auth
    if proxy and proxy_user and proxy_pass:
        await page.authenticate({"username": proxy_user, "password": proxy_pass})

    return browser, page


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
async def load_session(page, session_file: str) -> bool:
    if not os.path.exists(session_file):
        return False
    try:
        with open(session_file) as f:
            session = json.load(f)
        await page.goto(BASE_URL, timeout=REQUEST_TIMEOUT)
        await page.waitForFunction("document.readyState === 'complete'", timeout=REQUEST_TIMEOUT)
        for cookie in session.get("cookies", []):
            try:
                await page.setCookie(cookie)
            except Exception:
                pass
        print(f"[{ts()}] Session loaded from {session_file}")
        return True
    except Exception as e:
        print(f"[{ts()}] Session load error: {e}")
        return False


async def save_session(page, session_file: str) -> None:
    cookies = await page.cookies()
    with open(session_file, "w") as f:
        json.dump({"cookies": cookies}, f, indent=2)
    print(f"[{ts()}] Session saved → {session_file}")


async def do_login(page, session_file: str, debug: bool) -> None:
    await page.goto("https://medium.com/m/signin", timeout=REQUEST_TIMEOUT)
    await page.waitForFunction("document.readyState === 'complete'", timeout=REQUEST_TIMEOUT)
    await asyncio.sleep(2)

    if debug:
        Path("debug").mkdir(exist_ok=True)
        await page.screenshot({"path": "debug/login_page.png"})

    print("\n" + "=" * 60)
    print("  MANUAL LOGIN REQUIRED")
    print("  Complete login in the browser window, then press ENTER.")
    print("=" * 60 + "\n")
    input("Press ENTER after login is complete...")
    await asyncio.sleep(2)


# ---------------------------------------------------------------------------
# CAPTCHA
# ---------------------------------------------------------------------------
async def solve_captcha_if_needed(page, apikey: str, debug: bool) -> bool:
    if not apikey or not HAS_2CAPTCHA:
        return False

    solver = TwoCaptcha(apikey)

    sitekey_el = await page.querySelector("[data-sitekey]")
    if sitekey_el:
        sitekey = await page.evaluate("el => el.getAttribute('data-sitekey')", sitekey_el)
        print(f"[{ts()}] reCAPTCHA detected, solving via 2captcha...")
        try:
            result = solver.recaptcha(sitekey=sitekey, url=page.url)
            token = result.get("code", "")
            await page.evaluate(
                f'document.getElementById("g-recaptcha-response").value = "{token}";'
            )
            return True
        except Exception as e:
            print(f"[{ts()}] CAPTCHA error: {e}")
    return False


# ---------------------------------------------------------------------------
# Apollo state parser
# ---------------------------------------------------------------------------
def parse_apollo_state(html: str) -> list[dict]:
    posts = []
    match = re.search(r'window\.__APOLLO_STATE__\s*=\s*(\{.+?\});\s*</script>', html, re.DOTALL)
    if not match:
        return posts
    try:
        state = json.loads(match.group(1))
        for key, val in state.items():
            if key.startswith("Post:") and isinstance(val, dict) and val.get("title"):
                creator_ref = (val.get("creator") or {}).get("__ref", "")
                author = state.get(creator_ref, {})
                posts.append({
                    "id": val.get("id", ""),
                    "title": val.get("title", ""),
                    "url": "https://medium.com/p/" + val.get("id", ""),
                    "author_name": author.get("name", ""),
                    "author_username": author.get("username", ""),
                    "claps": val.get("clapCount", 0),
                    "reading_time": val.get("readingTime", 0),
                    "published_at": val.get("firstPublishedAt", ""),
                    "is_paywalled": val.get("isPaywalled", False),
                    "tags": [],
                })
    except Exception:
        pass
    return posts


# ---------------------------------------------------------------------------
# Global deduplication
# ---------------------------------------------------------------------------
def dedup_posts(posts: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for p in posts:
        key = p.get("title", "") or p.get("url", "") or p.get("id", "")
        if not key:
            continue
        if key not in seen:
            seen[key] = p
        else:
            if sum(1 for v in p.values() if v) > sum(1 for v in seen[key].values() if v):
                seen[key] = p
    return list(seen.values())


# ---------------------------------------------------------------------------
# Parse __NEXT_DATA__
# ---------------------------------------------------------------------------
def parse_next_data(html: str) -> list[dict]:
    posts = []
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', html, re.DOTALL)
    if not match:
        return posts
    try:
        data = json.loads(match.group(1))
        def walk(obj, depth=0):
            if depth > 10 or not obj:
                return
            if isinstance(obj, dict):
                if obj.get("title") and obj.get("id") and isinstance(obj.get("clapCount", None), int):
                    author = obj.get("creator") or obj.get("author") or {}
                    posts.append({
                        "id": obj.get("id", ""),
                        "title": obj.get("title", ""),
                        "subtitle": obj.get("subtitle", ""),
                        "url": "https://medium.com/p/" + obj.get("id", ""),
                        "author_name": author.get("name", ""),
                        "author_username": author.get("username", ""),
                        "claps": obj.get("clapCount", 0),
                        "reading_time": obj.get("readingTime", 0),
                        "published_at": obj.get("firstPublishedAt", ""),
                        "is_paywalled": obj.get("isPaywalled", False),
                        "tags": [],
                        "source": "next_data",
                    })
                for v in obj.values():
                    walk(v, depth + 1)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item, depth + 1)
        walk(data)
    except Exception:
        pass
    return posts


# ---------------------------------------------------------------------------
# DOM fallback
# ---------------------------------------------------------------------------
async def dom_fallback(page) -> list[dict]:
    return await page.evaluate(r"""
        () => {
            const POST_SLUG_RE = /^\/[^?#]+\/[a-z0-9][a-z0-9-]*-([a-f0-9]{10,})(\?|$)/i;
            const seen = new Set();
            const cards = [];
            for (const art of document.querySelectorAll('article')) {
                const h2 = art.querySelector('h2');
                if (!h2) continue;
                const title = h2.innerText.trim();
                if (!title || seen.has(title)) continue;
                seen.add(title);
                const h3 = art.querySelector('h3');
                const subtitle = h3 ? h3.innerText.trim() : '';
                let postUrl = '', postId = '';
                for (const a of art.querySelectorAll('a[href]')) {
                    const href = a.getAttribute('href');
                    if (!href || href.includes('/m/signin')) continue;
                    if (POST_SLUG_RE.test(href)) {
                        postUrl = (href.startsWith('http') ? href : 'https://medium.com' + href).split('?')[0];
                        const m = href.match(/-([a-f0-9]{10,})(\?|$)/i);
                        if (m) postId = m[1];
                        break;
                    }
                }
                if (!postId) {
                    for (const a of art.querySelectorAll('a[href]')) {
                        const m = (a.getAttribute('href') || '').match(/%2Fp%2F([a-f0-9]+)/);
                        if (m) { postId = m[1]; break; }
                    }
                }
                if (!postUrl && postId) postUrl = 'https://medium.com/p/' + postId;
                let authorName = '', authorUrl = '';
                for (const a of art.querySelectorAll('a[href]')) {
                    const href = a.getAttribute('href') || '';
                    const text = a.innerText.trim();
                    if (/^\/@[^/?]+/.test(href) && text && !text.toLowerCase().includes('clap') && text.length < 80) {
                        authorName = text; authorUrl = 'https://medium.com' + href.split('?')[0]; break;
                    }
                }
                let claps = 0, responses = 0;
                for (const a of art.querySelectorAll('a[href]')) {
                    const text = a.innerText.trim();
                    if (text.toLowerCase().includes('clap icon')) {
                        const cm = text.match(/clap icon([\d.,]+K?)/i);
                        if (cm) { const raw = cm[1].replace(/,/g,''); claps = raw.toUpperCase().includes('K') ? Math.round(parseFloat(raw)*1000) : (parseInt(raw,10)||0); }
                        const rm = text.match(/response icon(\d+)/i);
                        if (rm) responses = parseInt(rm[1],10)||0;
                        break;
                    }
                }
                cards.push({id: postId, title, subtitle, url: postUrl, author_name: authorName, author_url: authorUrl, claps, responses, source: 'dom_fallback'});
            }
            return cards;
        }
    """) or []


# ---------------------------------------------------------------------------
# Scroll helper
# ---------------------------------------------------------------------------
async def scroll_and_collect(page, interceptor: GraphQLInterceptor, limit: int) -> None:
    scroll_attempts = 0
    max_scroll = limit * 2 + 10

    while len(interceptor.posts) < limit and scroll_attempts < max_scroll:
        prev = len(interceptor.posts)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(SCROLL_PAUSE / 1000)
        scroll_attempts += 1
        if len(interceptor.posts) == prev:
            await asyncio.sleep(2)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)
            if len(interceptor.posts) == prev:
                break
        print(f"[{ts()}] Collected {len(interceptor.posts)} posts...", end="\r")
    print()


# ---------------------------------------------------------------------------
# Navigation helper
# ---------------------------------------------------------------------------
async def goto_safe(page, url: str) -> None:
    try:
        await page.goto(url, {"waitUntil": "networkidle2", "timeout": REQUEST_TIMEOUT})
    except PPTimeout:
        pass  # networkidle2 may timeout on infinite scroll pages
    await asyncio.sleep(1.5)


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------
async def scrape_tag(page, interceptor, target, limit, debug) -> list[dict]:
    tag = target.lstrip("#").lower().replace(" ", "-")
    url = f"{BASE_URL}/tag/{tag}"
    print(f"[{ts()}] Scraping tag: {url}")
    await goto_safe(page, url)

    if debug:
        Path("debug").mkdir(exist_ok=True)
        await page.screenshot({"path": f"debug/tag_{tag}.png"})
        with open(f"debug/tag_{tag}.html", "w", encoding="utf-8") as f:
            f.write(await page.content())

    await scroll_and_collect(page, interceptor, limit)

    html = await page.content()
    if not interceptor.posts:
        interceptor.posts = parse_next_data(html)
        if interceptor.posts:
            print(f"[{ts()}] __NEXT_DATA__ fallback: {len(interceptor.posts)} posts")
    if not interceptor.posts:
        interceptor.posts = parse_apollo_state(html)
        if interceptor.posts:
            print(f"[{ts()}] Apollo state fallback: {len(interceptor.posts)} posts")
    if not interceptor.posts:
        interceptor.posts = await dom_fallback(page)
        if interceptor.posts:
            print(f"[{ts()}] DOM fallback: {len(interceptor.posts)} posts")
    if not interceptor.posts:
        print(f"[{ts()}] WARNING: No posts found. Try --debug or use a residential --proxy.")

    result = dedup_posts(interceptor.posts)
    print(f"[{ts()}] After dedup: {len(result)} unique posts")
    return result[:limit]


async def scrape_author(page, interceptor, target, limit, debug) -> dict:
    username = target.lstrip("@")
    url = f"{BASE_URL}/@{username}"
    print(f"[{ts()}] Scraping author: {url}")
    await goto_safe(page, url)

    if debug:
        Path("debug").mkdir(exist_ok=True)
        await page.screenshot({"path": f"debug/author_{username}.png"})

    if not interceptor.author_info:
        interceptor.author_info = await page.evaluate("""
            () => {
                const nameEl = document.querySelector('h1, [data-testid="authorName"]');
                const bioEl = document.querySelector('[data-testid="userBio"]');
                return {
                    name: nameEl ? nameEl.innerText.trim() : '',
                    bio: bioEl ? bioEl.innerText.trim() : '',
                    url: window.location.href
                };
            }
        """) or {}

    await scroll_and_collect(page, interceptor, limit)

    html = await page.content()
    if not interceptor.posts:
        interceptor.posts = parse_next_data(html)
    if not interceptor.posts:
        interceptor.posts = parse_apollo_state(html)
    if not interceptor.posts:
        interceptor.posts = await dom_fallback(page)

    return {"author": interceptor.author_info, "posts": dedup_posts(interceptor.posts)[:limit]}


async def scrape_publication(page, interceptor, target, limit, debug) -> dict:
    slug = target.lstrip("/").lower()
    url = f"{BASE_URL}/{slug}"
    print(f"[{ts()}] Scraping publication: {url}")
    await goto_safe(page, url)

    if debug:
        Path("debug").mkdir(exist_ok=True)
        await page.screenshot({"path": f"debug/publication_{slug}.png"})

    if not interceptor.publication_info:
        interceptor.publication_info = await page.evaluate("""
            () => {
                const nameEl = document.querySelector('h1');
                const descEl = document.querySelector('[class*="description"]');
                return {
                    name: nameEl ? nameEl.innerText.trim() : '',
                    description: descEl ? descEl.innerText.trim() : '',
                    url: window.location.href
                };
            }
        """) or {}

    await scroll_and_collect(page, interceptor, limit)

    html = await page.content()
    if not interceptor.posts:
        interceptor.posts = parse_next_data(html)
    if not interceptor.posts:
        interceptor.posts = parse_apollo_state(html)
    if not interceptor.posts:
        interceptor.posts = await dom_fallback(page)

    return {"publication": interceptor.publication_info, "posts": dedup_posts(interceptor.posts)[:limit]}


async def scrape_post_detail(page, url: str, debug: bool) -> dict:
    print(f"[{ts()}] Fetching post: {url}")
    await goto_safe(page, url)

    if debug:
        Path("debug").mkdir(exist_ok=True)
        slug = re.sub(r"[^a-z0-9]", "_", url.split("/")[-1])[:30]
        await page.screenshot({"path": f"debug/post_{slug}.png"})

    ld_json = await page.evaluate("""
        () => {
            const el = document.querySelector('script[type="application/ld+json"]');
            return el ? el.textContent : null;
        }
    """)

    post = {"url": url}
    if ld_json:
        try:
            ld = json.loads(ld_json)
            if isinstance(ld, list):
                ld = ld[0]
            post.update({
                "title": ld.get("headline", ""),
                "description": ld.get("description", ""),
                "author_name": (ld.get("author") or {}).get("name", ""),
                "published_at": ld.get("datePublished", ""),
                "tags": ld.get("keywords", []),
            })
        except Exception:
            pass

    content = await page.evaluate("""
        () => {
            const article = document.querySelector('article');
            if (!article) return '';
            const paras = article.querySelectorAll('p, h1, h2, h3, h4, blockquote');
            return [...paras].map(p => p.innerText.trim()).filter(Boolean).join('\\n\\n');
        }
    """) or ""
    post["content"] = content

    claps_text = await page.evaluate("""
        () => {
            const btn = document.querySelector('button[aria-label*="clap"]');
            return btn ? btn.innerText : '0';
        }
    """) or "0"
    post["claps"] = parse_claps(claps_text)

    return post


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    parser = argparse.ArgumentParser(
        description="Medium.com scraper — Pyppeteer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--mode", choices=["tag", "author", "publication", "post"], default="tag")
    parser.add_argument("--target", required=True)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--output", default="")
    parser.add_argument("--format", choices=["json", "csv", "both"], default="json")
    parser.add_argument("--proxy", default="", help="Proxy server, e.g. http://gate.2prx.com:10000")
    parser.add_argument("--proxy-user", default="")
    parser.add_argument("--proxy-pass", default="")
    parser.add_argument("--apikey", default=os.getenv("APIKEY_2CAPTCHA", ""))
    parser.add_argument("--auth", action="store_true")
    parser.add_argument("--session-file", default="session.json")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if not args.output:
        safe = re.sub(r"[^a-z0-9_]", "_", args.target.lower().lstrip("@"))[:30]
        args.output = f"medium_{args.mode}_{safe}"

    print(f"[{ts()}] Medium Scraper (Pyppeteer) starting")
    print(f"[{ts()}] Mode: {args.mode} | Target: {args.target} | Limit: {args.limit}")

    interceptor = GraphQLInterceptor()

    browser, page = await build_browser(
        args.proxy or None,
        args.proxy_user or None,
        args.proxy_pass or None,
        args.headed,
    )

    # Intercept responses
    async def on_response(response):
        if "/_/graphql" in response.url or "graphql" in response.url.lower():
            try:
                body = await response.text()
                interceptor.process_response(body)
            except Exception:
                pass

    page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

    try:
        # Auth
        if args.auth:
            session_exists = os.path.exists(args.session_file) and os.path.getsize(args.session_file) > 10
            if not session_exists:
                await do_login(page, args.session_file, args.debug)
                await save_session(page, args.session_file)
            else:
                await load_session(page, args.session_file)

        # Warmup
        try:
            await page.goto(BASE_URL, {"timeout": REQUEST_TIMEOUT})
            await asyncio.sleep(1.5)
        except Exception:
            pass

        # Dispatch
        if args.mode == "tag":
            result = await scrape_tag(page, interceptor, args.target, args.limit, args.debug)
        elif args.mode == "author":
            result = await scrape_author(page, interceptor, args.target, args.limit, args.debug)
        elif args.mode == "publication":
            result = await scrape_publication(page, interceptor, args.target, args.limit, args.debug)
        elif args.mode == "post":
            result = await scrape_post_detail(page, args.target, args.debug)

    finally:
        await browser.close()

    records = result if isinstance(result, list) else ([result] if args.mode == "post" else result.get("posts", []))
    out_base = args.output.replace(".json", "").replace(".csv", "")

    if args.format in ("json", "both"):
        save_json(result if args.mode not in ("tag",) else records, out_base + ".json")
    if args.format in ("csv", "both"):
        save_csv(records, out_base + ".csv")

    print(f"[{ts()}] Done. Total posts: {len(records)}")


if __name__ == "__main__":
    asyncio.run(main())
