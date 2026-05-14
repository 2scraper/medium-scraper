"""
Medium.com Scraper — Playwright
================================
Scrapes articles by tag, author profiles, and publications.

GitHub : https://github.com/2scraper/medium-scraper
License: MIT

Usage examples
--------------
# Scrape articles by tag
python medium_playwright.py --mode tag --target python --limit 50 --output articles.json

# Scrape author profile + their posts
python medium_playwright.py --mode author --target @towards-data-science --limit 30

# Scrape a publication
python medium_playwright.py --mode publication --target towards-ai --limit 40

# Scrape a single post (full content)
python medium_playwright.py --mode post --target "https://medium.com/p/abc123"

# Use with 2captcha + proxy
python medium_playwright.py --mode tag --target python \
    --proxy "http://user:pass@gate.2prx.com:10000" \
    --apikey "YOUR_2CAPTCHA_KEY"

# Auth (for paywall articles)
python medium_playwright.py --mode tag --target python --auth \
    --session-file session.json
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
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit("playwright not installed. Run: pip install playwright && playwright install chromium")

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
GRAPHQL_URL = "https://medium.com/_/graphql"
DEFAULT_LIMIT = 25
SCROLL_PAUSE = 2.5
REQUEST_TIMEOUT = 30_000   # ms


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def save_json(data: list, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[{ts()}] Saved {len(data)} records → {path}")


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
    """Convert '4.2K' → 4200."""
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
# GraphQL interceptor
# ---------------------------------------------------------------------------
class GraphQLInterceptor:
    """Intercepts Medium's internal GraphQL responses."""

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
            "preview_text": clean_text(
                (post.get("extendedPreviewContent") or {}).get("bodyModel", {}).get("paragraphs", [{}])[0].get("text", "")
                if isinstance((post.get("extendedPreviewContent") or {}).get("bodyModel", {}).get("paragraphs"), list) and
                (post.get("extendedPreviewContent") or {}).get("bodyModel", {}).get("paragraphs")
                else ""
            ),
        }

    def process_response(self, body: str) -> None:
        try:
            data = json.loads(body)
        except Exception:
            return

        # Handle array of operations
        if isinstance(data, list):
            for item in data:
                self.process_response(json.dumps(item))
            return

        data = data.get("data", data)

        # Tag feed
        for key in ("tagFeed", "tagFeedItems", "topicFeedItems"):
            feed = data.get(key)
            if feed:
                items = feed.get("items", [])
                for item in items:
                    post = item.get("post") or item.get("feedItem", {}).get("post")
                    if post:
                        self.posts.append(self._extract_post(post))

        # Author posts
        for key in ("userResult", "user"):
            user = data.get(key)
            if user:
                self.author_info = {
                    "name": user.get("name", ""),
                    "username": user.get("username", ""),
                    "bio": user.get("bio", ""),
                    "followers": user.get("socialStats", {}).get("followerCount", 0),
                    "following": user.get("socialStats", {}).get("followingCount", 0),
                    "url": f"https://medium.com/@{user.get('username', '')}",
                }
                posts_conn = user.get("profileStreamConnection") or user.get("userPostConnection") or {}
                for edge in posts_conn.get("edges", []):
                    post = (edge.get("node") or {}).get("post") or edge.get("node")
                    if post and post.get("id"):
                        self.posts.append(self._extract_post(post))

        # Publication
        for key in ("collection", "publicationResult"):
            pub = data.get(key)
            if pub:
                self.publication_info = {
                    "name": pub.get("name", ""),
                    "description": pub.get("description", ""),
                    "followers": pub.get("subscriberCount", 0),
                    "url": pub.get("domain") or f"https://medium.com/{pub.get('slug', '')}",
                }
                posts_conn = pub.get("postStream") or pub.get("publicationPostConnection") or {}
                for edge in posts_conn.get("edges", []):
                    post = (edge.get("node") or {}).get("post") or edge.get("node")
                    if post and post.get("id"):
                        self.posts.append(self._extract_post(post))


# ---------------------------------------------------------------------------
# Browser builder
# ---------------------------------------------------------------------------
async def build_browser(playwright, proxy: str | None, headed: bool):
    launch_args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-http2",
    ]

    proxy_settings = None
    if proxy:
        # Support http://user:pass@host:port
        proxy_settings = {"server": proxy}
        if "@" in proxy:
            creds = proxy.split("@")[0].split("//")[-1]
            if ":" in creds:
                user, password = creds.split(":", 1)
                host_part = proxy.split("@")[1]
                proxy_settings = {
                    "server": f"http://{host_part}",
                    "username": user,
                    "password": password,
                }

    browser = await playwright.chromium.launch(
        headless=not headed,
        args=launch_args,
        proxy=proxy_settings,
    )

    context = await browser.new_context(
        viewport={"width": 1440, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        timezone_id="America/New_York",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
        },
    )

    # Minimal stealth: disable webdriver flag only
    await context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

    return browser, context


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
async def load_session(context, session_file: str) -> bool:
    if os.path.exists(session_file):
        with open(session_file) as f:
            storage = json.load(f)
        await context.add_cookies(storage.get("cookies", []))
        print(f"[{ts()}] Session loaded from {session_file}")
        return True
    return False


async def save_session(context, session_file: str) -> None:
    cookies = await context.cookies()
    with open(session_file, "w") as f:
        json.dump({"cookies": cookies}, f, indent=2)
    print(f"[{ts()}] Session saved → {session_file}")


async def do_login(page, args, debug: bool) -> None:
    """Interactive or cookie-based login."""
    print(f"[{ts()}] Opening login page...")
    await page.goto("https://medium.com/m/signin", wait_until="networkidle", timeout=REQUEST_TIMEOUT)
    await page.wait_for_timeout(2000)

    if debug:
        Path("debug").mkdir(exist_ok=True)
        await page.screenshot(path="debug/login_page.png")

    # Medium uses Google/email OAuth — instruct user to complete manually
    print("\n" + "=" * 60)
    print("  MANUAL LOGIN REQUIRED")
    print("  Complete the login in the browser window, then press ENTER.")
    print("=" * 60 + "\n")
    input("Press ENTER after login is complete...")
    await page.wait_for_timeout(2000)


# ---------------------------------------------------------------------------
# CAPTCHA solver
# ---------------------------------------------------------------------------
async def solve_captcha_if_needed(page, apikey: str | None, debug: bool) -> bool:
    """Detects and solves reCAPTCHA / hCaptcha if present."""
    if not apikey or not HAS_2CAPTCHA:
        return False

    solver = TwoCaptcha(apikey)

    # reCAPTCHA
    sitekey_el = await page.query_selector("[data-sitekey]")
    if sitekey_el:
        sitekey = await sitekey_el.get_attribute("data-sitekey")
        print(f"[{ts()}] reCAPTCHA detected, solving via 2captcha...")
        try:
            result = solver.recaptcha(sitekey=sitekey, url=page.url)
            token = result.get("code", "")
            await page.evaluate(
                f'document.getElementById("g-recaptcha-response").value = "{token}";'
            )
            print(f"[{ts()}] CAPTCHA solved.")
            return True
        except Exception as e:
            print(f"[{ts()}] CAPTCHA solve error: {e}")

    # hCaptcha
    hcap_el = await page.query_selector("[data-hcaptcha-sitekey], .h-captcha[data-sitekey]")
    if hcap_el:
        sitekey = await hcap_el.get_attribute("data-sitekey") or await hcap_el.get_attribute("data-hcaptcha-sitekey")
        print(f"[{ts()}] hCaptcha detected, solving via 2captcha...")
        try:
            result = solver.hcaptcha(sitekey=sitekey, url=page.url)
            token = result.get("code", "")
            await page.evaluate(
                f'document.querySelector("[name=h-captcha-response]").value = "{token}";'
            )
            print(f"[{ts()}] hCaptcha solved.")
            return True
        except Exception as e:
            print(f"[{ts()}] hCaptcha solve error: {e}")

    return False


# ---------------------------------------------------------------------------
# Scroll & intercept helper
# ---------------------------------------------------------------------------
async def scroll_and_collect(page, interceptor: GraphQLInterceptor, limit: int, debug: bool) -> None:
    """Scroll the page to trigger lazy loading until we hit the limit."""
    collected = 0
    scroll_attempts = 0
    max_scroll = limit * 2 + 10  # generous cap

    while len(interceptor.posts) < limit and scroll_attempts < max_scroll:
        prev = len(interceptor.posts)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(int(SCROLL_PAUSE * 1000))
        scroll_attempts += 1
        new = len(interceptor.posts)
        if new == prev:
            # No new posts after scroll — try one more time
            await page.wait_for_timeout(2000)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)
            if len(interceptor.posts) == prev:
                break  # End of feed
        print(f"[{ts()}] Collected {len(interceptor.posts)} posts so far...", end="\r")

    print()


# ---------------------------------------------------------------------------
# Global deduplication
# ---------------------------------------------------------------------------
def dedup_posts(posts: list[dict]) -> list[dict]:
    """Remove duplicate posts. Prioritize records with more data."""
    seen: dict[str, dict] = {}
    for p in posts:
        key = p.get("title", "") or p.get("url", "") or p.get("id", "")
        if not key:
            continue
        if key not in seen:
            seen[key] = p
        else:
            # Keep the record with more filled fields
            existing = seen[key]
            if sum(1 for v in p.values() if v) > sum(1 for v in existing.values() if v):
                seen[key] = p
    return list(seen.values())


# ---------------------------------------------------------------------------
# Parse __NEXT_DATA__ from HTML (Medium uses this on some page types)
# ---------------------------------------------------------------------------
def parse_next_data(html: str) -> list[dict]:
    posts = []
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', html, re.DOTALL)
    if not match:
        return posts
    try:
        data = json.loads(match.group(1))
        # Walk the props tree looking for post-like objects
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
# Parse Apollo state from HTML
# ---------------------------------------------------------------------------
def parse_apollo_state(html: str) -> list[dict]:
    """Fallback: extract post data from __APOLLO_STATE__ embedded JSON."""
    posts = []
    match = re.search(r'window\.__APOLLO_STATE__\s*=\s*(\{.+?\});\s*</script>', html, re.DOTALL)
    if not match:
        return posts
    try:
        state = json.loads(match.group(1))
        for key, val in state.items():
            if key.startswith("Post:") and isinstance(val, dict) and val.get("title"):
                creator_ref = val.get("creator") or {}
                author_key = creator_ref.get("__ref", "")
                author = state.get(author_key, {})
                posts.append({
                    "id": val.get("id", ""),
                    "title": val.get("title", ""),
                    "subtitle": val.get("subtitle", ""),
                    "url": "https://medium.com/p/" + val.get("id", ""),
                    "author_name": author.get("name", ""),
                    "author_username": author.get("username", ""),
                    "author_url": f"https://medium.com/@{author.get('username', '')}",
                    "claps": val.get("clapCount", 0),
                    "responses": 0,
                    "reading_time": val.get("readingTime", 0),
                    "published_at": val.get("firstPublishedAt", ""),
                    "tags": [],
                    "is_paywalled": val.get("isPaywalled", False),
                    "preview_text": "",
                })
    except Exception:
        pass
    return posts


# ---------------------------------------------------------------------------
# DOM fallback extractor
# ---------------------------------------------------------------------------
async def dom_fallback(page) -> list[dict]:
    """
    DOM scraping tuned to Medium's confirmed HTML structure (from live debug HTML analysis).

    Confirmed structure per <article>:
    - Title: <h2> (always present)
    - Subtitle: <h3>
    - Post URL: <a href="/{pub-or-@user}/slug-hexhash?source=..."> — matches slug ending in 10+ hex chars
      Both /@user/slug-hash AND /publication/slug-hash forms exist
    - Author: <a href="/@username?source=..."> with short non-clap text
    - Claps: <a> whose innerText contains "clap icon" followed by a number
    - Post ID: extractable from bookmark link %2Fp%2F{id}
    """
    return await page.evaluate(r"""
        () => {
            // Matches any path ending in a slug with 10+ hex char hash
            // Covers: /@user/slug-1daa02a4d790  AND  /pub/slug-f4e352541695
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

                // Post URL: first link whose relative href matches slug-hexhash pattern
                let postUrl = '';
                let postId = '';
                for (const a of art.querySelectorAll('a[href]')) {
                    const href = a.getAttribute('href');
                    if (!href || href.includes('/m/signin')) continue;
                    if (POST_SLUG_RE.test(href)) {
                        postUrl = (href.startsWith('http') ? href : 'https://medium.com' + href).split('?')[0];
                        // Extract ID from end of slug (last hyphenated segment)
                        const m = href.match(/-([a-f0-9]{10,})(\?|$)/i);
                        if (m) postId = m[1];
                        break;
                    }
                }

                // Fallback: extract post ID from bookmark signin link (%2Fp%2F{id})
                if (!postId) {
                    for (const a of art.querySelectorAll('a[href]')) {
                        const m = (a.getAttribute('href') || '').match(/%2Fp%2F([a-f0-9]+)/);
                        if (m) { postId = m[1]; break; }
                    }
                }
                if (!postUrl && postId) postUrl = 'https://medium.com/p/' + postId;

                // Author: <a href="/@username?..."> with non-empty, non-clap text
                let authorName = '', authorUrl = '';
                for (const a of art.querySelectorAll('a[href]')) {
                    const href = a.getAttribute('href') || '';
                    const text = a.innerText.trim();
                    if (/^\/@[^/?]+/.test(href) && text && !text.toLowerCase().includes('clap') && text.length < 80) {
                        authorName = text;
                        authorUrl = 'https://medium.com' + href.split('?')[0];
                        break;
                    }
                }

                // Claps + responses: link text "A clap icon{N}A response icon{N}"
                let claps = 0, responses = 0;
                for (const a of art.querySelectorAll('a[href]')) {
                    const text = a.innerText.trim();
                    if (text.toLowerCase().includes('clap icon')) {
                        const cm = text.match(/clap icon([\d.,]+K?)/i);
                        if (cm) {
                            const raw = cm[1].replace(/,/g, '');
                            claps = raw.toUpperCase().includes('K')
                                ? Math.round(parseFloat(raw) * 1000)
                                : (parseInt(raw, 10) || 0);
                        }
                        const rm = text.match(/response icon(\d+)/i);
                        if (rm) responses = parseInt(rm[1], 10) || 0;
                        break;
                    }
                }

                cards.push({ id: postId, title, subtitle, url: postUrl,
                             author_name: authorName, author_url: authorUrl,
                             claps, responses, source: 'dom_fallback' });
            }
            return cards;
        }
    """)


# ---------------------------------------------------------------------------
# Post detail extractor
# ---------------------------------------------------------------------------
async def scrape_post_detail(page, url: str, debug: bool) -> dict:
    """Scrape a single post page for full content."""
    print(f"[{ts()}] Fetching post: {url}")
    await page.goto(url, wait_until="networkidle", timeout=REQUEST_TIMEOUT)
    await page.wait_for_timeout(2000)

    if debug:
        Path("debug").mkdir(exist_ok=True)
        slug = re.sub(r"[^a-z0-9]", "_", url.split("/")[-1])[:30]
        await page.screenshot(path=f"debug/post_{slug}.png")

    # Try JSON-LD first
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
                "modified_at": ld.get("dateModified", ""),
                "tags": ld.get("keywords", []),
                "image": ld.get("image", {}).get("url", "") if isinstance(ld.get("image"), dict) else ld.get("image", ""),
            })
        except Exception:
            pass

    # Full article text
    content = await page.evaluate("""
        () => {
            const article = document.querySelector('article');
            if (!article) return '';
            const paras = article.querySelectorAll('p, h1, h2, h3, h4, blockquote');
            return [...paras].map(p => p.innerText.trim()).filter(Boolean).join('\\n\\n');
        }
    """)
    post["content"] = content

    # Claps
    claps_text = await page.evaluate("""
        () => {
            const btn = document.querySelector('button[aria-label*="clap"]') ||
                        document.querySelector('[class*="clapButton"]') ||
                        document.querySelector('[data-testid="clapButton"]');
            return btn ? btn.innerText : '0';
        }
    """)
    post["claps"] = parse_claps(claps_text)

    return post


# ---------------------------------------------------------------------------
# Mode: TAG
# ---------------------------------------------------------------------------
async def scrape_tag(page, interceptor: GraphQLInterceptor, target: str, limit: int, debug: bool) -> list[dict]:
    tag = target.lstrip("#").lower().replace(" ", "-")
    url = f"{BASE_URL}/tag/{tag}"
    print(f"[{ts()}] Scraping tag: {url}")

    await page.goto(url, wait_until="networkidle", timeout=REQUEST_TIMEOUT)
    await page.wait_for_timeout(2000)

    if debug:
        Path("debug").mkdir(exist_ok=True)
        await page.screenshot(path=f"debug/tag_{tag}.png")
        with open(f"debug/tag_{tag}.html", "w", encoding="utf-8") as f:
            f.write(await page.content())

    await scroll_and_collect(page, interceptor, limit, debug)

    html = await page.content()

    # Fallback chain: __NEXT_DATA__ → __APOLLO_STATE__ → DOM
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
        print(f"[{ts()}] WARNING: No posts found. Try --debug to inspect the page, or use --proxy with a residential proxy.")

    result = dedup_posts(interceptor.posts)
    print(f"[{ts()}] After dedup: {len(result)} unique posts (from {len(interceptor.posts)} collected)")
    return result[:limit]


# ---------------------------------------------------------------------------
# Mode: AUTHOR
# ---------------------------------------------------------------------------
async def scrape_author(page, interceptor: GraphQLInterceptor, target: str, limit: int, debug: bool) -> dict:
    username = target.lstrip("@")
    url = f"{BASE_URL}/@{username}"
    print(f"[{ts()}] Scraping author: {url}")

    await page.goto(url, wait_until="networkidle", timeout=REQUEST_TIMEOUT)
    await page.wait_for_timeout(2000)

    if debug:
        Path("debug").mkdir(exist_ok=True)
        await page.screenshot(path=f"debug/author_{username}.png")
        with open(f"debug/author_{username}.html", "w", encoding="utf-8") as f:
            f.write(await page.content())

    # If interceptor didn't get author info, extract from DOM
    if not interceptor.author_info:
        interceptor.author_info = await page.evaluate("""
            () => {
                const nameEl = document.querySelector('h1, [data-testid="authorName"]');
                const bioEl = document.querySelector('[data-testid="userBio"], [class*="bio"]');
                const followEl = document.querySelector('[class*="follower"]');
                return {
                    name: nameEl ? nameEl.innerText.trim() : '',
                    bio: bioEl ? bioEl.innerText.trim() : '',
                    followers_text: followEl ? followEl.innerText.trim() : '',
                    url: window.location.href,
                };
            }
        """)

    await scroll_and_collect(page, interceptor, limit, debug)

    html = await page.content()
    if not interceptor.posts:
        interceptor.posts = parse_next_data(html)
    if not interceptor.posts:
        interceptor.posts = parse_apollo_state(html)
    if not interceptor.posts:
        interceptor.posts = await dom_fallback(page)

    return {
        "author": interceptor.author_info,
        "posts": dedup_posts(interceptor.posts)[:limit],
    }


# ---------------------------------------------------------------------------
# Mode: PUBLICATION
# ---------------------------------------------------------------------------
async def scrape_publication(page, interceptor: GraphQLInterceptor, target: str, limit: int, debug: bool) -> dict:
    slug = target.lstrip("/").lower()
    url = f"{BASE_URL}/{slug}"
    print(f"[{ts()}] Scraping publication: {url}")

    await page.goto(url, wait_until="networkidle", timeout=REQUEST_TIMEOUT)
    await page.wait_for_timeout(2000)

    if debug:
        Path("debug").mkdir(exist_ok=True)
        await page.screenshot(path=f"debug/publication_{slug}.png")

    if not interceptor.publication_info:
        interceptor.publication_info = await page.evaluate("""
            () => {
                const nameEl = document.querySelector('h1, [data-testid="publicationName"]');
                const descEl = document.querySelector('[data-testid="publicationDescription"], [class*="description"]');
                return {
                    name: nameEl ? nameEl.innerText.trim() : '',
                    description: descEl ? descEl.innerText.trim() : '',
                    url: window.location.href,
                };
            }
        """)

    await scroll_and_collect(page, interceptor, limit, debug)

    html = await page.content()
    if not interceptor.posts:
        interceptor.posts = parse_next_data(html)
    if not interceptor.posts:
        interceptor.posts = parse_apollo_state(html)
    if not interceptor.posts:
        interceptor.posts = await dom_fallback(page)

    return {
        "publication": interceptor.publication_info,
        "posts": dedup_posts(interceptor.posts)[:limit],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    parser = argparse.ArgumentParser(
        description="Medium.com scraper — Playwright",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--mode", choices=["tag", "author", "publication", "post"],
                        default="tag", help="Scraping mode (default: tag)")
    parser.add_argument("--target", required=True,
                        help="Tag name, @username, publication slug, or post URL")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"Max posts to collect (default: {DEFAULT_LIMIT})")
    parser.add_argument("--output", default="",
                        help="Output file path (.json or .csv). Auto-named if omitted.")
    parser.add_argument("--format", choices=["json", "csv", "both"], default="json",
                        help="Output format (default: json)")
    parser.add_argument("--proxy",
                        help="Proxy URL, e.g. http://user:pass@gate.2prx.com:10000")
    parser.add_argument("--apikey", default=os.getenv("APIKEY_2CAPTCHA", ""),
                        help="2captcha.com API key (or set APIKEY_2CAPTCHA env var)")
    parser.add_argument("--auth", action="store_true",
                        help="Enable authentication (for paywall articles)")
    parser.add_argument("--session-file", default="session.json",
                        help="File to store/load session cookies (default: session.json)")
    parser.add_argument("--headed", action="store_true",
                        help="Run browser in headed (visible) mode")
    parser.add_argument("--debug", action="store_true",
                        help="Save screenshots and raw HTML to debug/")
    args = parser.parse_args()

    # Build output filename
    if not args.output:
        safe = re.sub(r"[^a-z0-9_]", "_", args.target.lower().lstrip("@"))[:30]
        args.output = f"medium_{args.mode}_{safe}"

    print(f"[{ts()}] Medium Scraper (Playwright) starting")
    print(f"[{ts()}] Mode: {args.mode} | Target: {args.target} | Limit: {args.limit}")

    interceptor = GraphQLInterceptor()

    async with async_playwright() as pw:
        browser, context = await build_browser(pw, args.proxy, args.headed)

        # Session management
        if args.auth:
            loaded = await load_session(context, args.session_file)

        page = await context.new_page()

        # Intercept GraphQL responses
        async def handle_response(response):
            if "/_/graphql" in response.url or "graphql" in response.url.lower():
                try:
                    body = await response.text()
                    interceptor.process_response(body)
                except Exception:
                    pass

        page.on("response", handle_response)

        # Login flow
        if args.auth:
            session_exists = os.path.exists(args.session_file) and os.path.getsize(args.session_file) > 10
            if not session_exists:
                await do_login(page, args, args.debug)
                await save_session(context, args.session_file)
            else:
                print(f"[{ts()}] Using existing session")

        # Warmup homepage to get cookies
        try:
            await page.goto(BASE_URL, wait_until="commit", timeout=REQUEST_TIMEOUT)
            await page.wait_for_timeout(1500)
        except Exception:
            pass

        # Dispatch
        result = None
        if args.mode == "tag":
            posts = await scrape_tag(page, interceptor, args.target, args.limit, args.debug)
            result = posts

        elif args.mode == "author":
            result = await scrape_author(page, interceptor, args.target, args.limit, args.debug)

        elif args.mode == "publication":
            result = await scrape_publication(page, interceptor, args.target, args.limit, args.debug)

        elif args.mode == "post":
            post = await scrape_post_detail(page, args.target, args.debug)
            result = post

        await browser.close()

    # Save
    if args.mode in ("tag",):
        records = result if isinstance(result, list) else []
    elif args.mode == "post":
        records = [result]
    else:
        records = result.get("posts", [])

    out_base = args.output.replace(".json", "").replace(".csv", "")

    if args.format in ("json", "both"):
        save_json(result if args.mode not in ("tag",) else records, out_base + ".json")
    if args.format in ("csv", "both"):
        save_csv(records, out_base + ".csv")

    print(f"[{ts()}] Done. Total posts: {len(records)}")


if __name__ == "__main__":
    asyncio.run(main())
