"""
Medium.com Scraper — Selenium
==============================
Scrapes articles by tag, author profiles, and publications.

GitHub : https://github.com/2scraper/medium-scraper
License: MIT

Usage examples
--------------
# Scrape articles by tag
python medium_selenium.py --mode tag --target python --limit 50

# Scrape author profile
python medium_selenium.py --mode author --target @towards-data-science --limit 30

# Scrape publication
python medium_selenium.py --mode publication --target towards-ai --limit 40

# Single post
python medium_selenium.py --mode post --target "https://medium.com/p/abc123"

# With proxy + 2captcha
python medium_selenium.py --mode tag --target python \
    --proxy "http://user:pass@gate.2prx.com:10000" \
    --apikey "YOUR_2CAPTCHA_KEY"

# Auth (for paywall)
python medium_selenium.py --mode tag --target python --auth --session-file session.json
"""

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
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
except ImportError:
    sys.exit("selenium not installed. Run: pip install selenium webdriver-manager")

try:
    from webdriver_manager.chrome import ChromeDriverManager
    HAS_WDM = True
except ImportError:
    HAS_WDM = False

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
SCROLL_PAUSE = 3.0
WAIT_TIMEOUT = 30


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


def wait_for_page_ready(driver, timeout: int = WAIT_TIMEOUT) -> None:
    """Wait for JS to finish loading (Selenium has no networkidle)."""
    end = time.time() + timeout
    while time.time() < end:
        state = driver.execute_script("return document.readyState")
        if state == "complete":
            break
        time.sleep(0.5)
    time.sleep(1.5)


# ---------------------------------------------------------------------------
# GraphQL response interceptor via Chrome DevTools Protocol (CDP)
# ---------------------------------------------------------------------------
class SeleniumInterceptor:
    """Collects GraphQL responses via performance log."""

    def __init__(self, driver):
        self.driver = driver
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

    def _process_body(self, body: str) -> None:
        try:
            data = json.loads(body)
        except Exception:
            return

        if isinstance(data, list):
            for item in data:
                self._process_body(json.dumps(item))
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

    def harvest_logs(self) -> None:
        """Read Chrome performance logs and extract GraphQL responses."""
        try:
            logs = self.driver.get_log("performance")
        except Exception:
            return

        for entry in logs:
            try:
                msg = json.loads(entry["message"])["message"]
                if msg.get("method") == "Network.responseReceived":
                    url = msg["params"]["response"]["url"]
                    if "graphql" in url.lower() or "/_/" in url:
                        req_id = msg["params"]["requestId"]
                        try:
                            body = self.driver.execute_cdp_cmd(
                                "Network.getResponseBody", {"requestId": req_id}
                            ).get("body", "")
                            if body:
                                self._process_body(body)
                        except Exception:
                            pass
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Driver builder
# ---------------------------------------------------------------------------
def build_driver(proxy: str | None, headed: bool) -> webdriver.Chrome:
    opts = Options()
    if not headed:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-http2")
    opts.add_argument("--window-size=1440,900")
    opts.add_argument("--lang=en-US")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    # Enable performance logging for network interception
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    opts.add_argument("--enable-logging")

    if proxy:
        # Selenium doesn't support authenticated proxies natively via args
        # Use extension or pass creds separately
        if "@" in proxy:
            # Strip credentials — use unauth'd proxy for Selenium, or use extension
            host_part = proxy.split("@")[-1]
            opts.add_argument(f"--proxy-server={host_part}")
        else:
            opts.add_argument(f"--proxy-server={proxy}")

    if HAS_WDM:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=opts)
    else:
        driver = webdriver.Chrome(options=opts)

    # Stealth: remove webdriver flag
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    })

    driver.set_page_load_timeout(WAIT_TIMEOUT)
    return driver


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------
def load_session(driver, session_file: str) -> bool:
    if not os.path.exists(session_file):
        return False
    try:
        driver.get(BASE_URL)
        wait_for_page_ready(driver)
        with open(session_file) as f:
            session = json.load(f)
        for cookie in session.get("cookies", []):
            try:
                driver.add_cookie(cookie)
            except Exception:
                pass
        print(f"[{ts()}] Session loaded from {session_file}")
        return True
    except Exception as e:
        print(f"[{ts()}] Session load error: {e}")
        return False


def save_session(driver, session_file: str) -> None:
    cookies = driver.get_cookies()
    with open(session_file, "w") as f:
        json.dump({"cookies": cookies}, f, indent=2)
    print(f"[{ts()}] Session saved → {session_file}")


def do_login(driver, session_file: str, debug: bool) -> None:
    driver.get("https://medium.com/m/signin")
    wait_for_page_ready(driver)

    if debug:
        Path("debug").mkdir(exist_ok=True)
        driver.save_screenshot("debug/login_page.png")

    print("\n" + "=" * 60)
    print("  MANUAL LOGIN REQUIRED")
    print("  Complete login in the browser window, then press ENTER.")
    print("=" * 60 + "\n")
    input("Press ENTER after login is complete...")
    time.sleep(2)


# ---------------------------------------------------------------------------
# CAPTCHA
# ---------------------------------------------------------------------------
def solve_captcha_if_needed(driver, apikey: str, debug: bool) -> bool:
    if not apikey or not HAS_2CAPTCHA:
        return False

    solver = TwoCaptcha(apikey)

    try:
        sitekey_el = driver.find_element(By.CSS_SELECTOR, "[data-sitekey]")
        sitekey = sitekey_el.get_attribute("data-sitekey")
        print(f"[{ts()}] reCAPTCHA detected, solving via 2captcha...")
        result = solver.recaptcha(sitekey=sitekey, url=driver.current_url)
        token = result.get("code", "")
        driver.execute_script(
            f'document.getElementById("g-recaptcha-response").value = "{token}";'
        )
        return True
    except (NoSuchElementException, Exception):
        pass

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
                    "subtitle": val.get("subtitle", ""),
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
            existing = seen[key]
            if sum(1 for v in p.values() if v) > sum(1 for v in existing.values() if v):
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
def dom_fallback(driver) -> list[dict]:
    return driver.execute_script(r"""
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
    """) or []


# ---------------------------------------------------------------------------
# Scroll helper
# ---------------------------------------------------------------------------
def scroll_and_collect(driver, interceptor: SeleniumInterceptor, limit: int, debug: bool) -> None:
    scroll_attempts = 0
    max_scroll = limit * 2 + 10

    while len(interceptor.posts) < limit and scroll_attempts < max_scroll:
        prev = len(interceptor.posts)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(SCROLL_PAUSE)
        interceptor.harvest_logs()
        scroll_attempts += 1
        if len(interceptor.posts) == prev:
            time.sleep(2)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(2)
            interceptor.harvest_logs()
            if len(interceptor.posts) == prev:
                break
        print(f"[{ts()}] Collected {len(interceptor.posts)} posts...", end="\r")
    print()


# ---------------------------------------------------------------------------
# Mode: TAG
# ---------------------------------------------------------------------------
def scrape_tag(driver, interceptor: SeleniumInterceptor, target: str, limit: int, debug: bool) -> list[dict]:
    tag = target.lstrip("#").lower().replace(" ", "-")
    url = f"{BASE_URL}/tag/{tag}"
    print(f"[{ts()}] Scraping tag: {url}")

    driver.get(url)
    wait_for_page_ready(driver)
    interceptor.harvest_logs()

    if debug:
        Path("debug").mkdir(exist_ok=True)
        driver.save_screenshot(f"debug/tag_{tag}.png")
        with open(f"debug/tag_{tag}.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)

    scroll_and_collect(driver, interceptor, limit, debug)

    html = driver.page_source
    if not interceptor.posts:
        interceptor.posts = parse_next_data(html)
        if interceptor.posts:
            print(f"[{ts()}] __NEXT_DATA__ fallback: {len(interceptor.posts)} posts")
    if not interceptor.posts:
        interceptor.posts = parse_apollo_state(html)
        if interceptor.posts:
            print(f"[{ts()}] Apollo state fallback: {len(interceptor.posts)} posts")
    if not interceptor.posts:
        interceptor.posts = dom_fallback(driver)
        if interceptor.posts:
            print(f"[{ts()}] DOM fallback: {len(interceptor.posts)} posts")
    if not interceptor.posts:
        print(f"[{ts()}] WARNING: No posts found. Try --debug or use a residential --proxy.")

    result = dedup_posts(interceptor.posts)
    print(f"[{ts()}] After dedup: {len(result)} unique posts")
    return result[:limit]


# ---------------------------------------------------------------------------
# Mode: AUTHOR
# ---------------------------------------------------------------------------
def scrape_author(driver, interceptor: SeleniumInterceptor, target: str, limit: int, debug: bool) -> dict:
    username = target.lstrip("@")
    url = f"{BASE_URL}/@{username}"
    print(f"[{ts()}] Scraping author: {url}")

    driver.get(url)
    wait_for_page_ready(driver)
    interceptor.harvest_logs()

    if debug:
        Path("debug").mkdir(exist_ok=True)
        driver.save_screenshot(f"debug/author_{username}.png")

    if not interceptor.author_info:
        interceptor.author_info = driver.execute_script("""
            const nameEl = document.querySelector('h1, [data-testid="authorName"]');
            const bioEl = document.querySelector('[data-testid="userBio"], [class*="bio"]');
            return {
                name: nameEl ? nameEl.innerText.trim() : '',
                bio: bioEl ? bioEl.innerText.trim() : '',
                url: window.location.href
            };
        """) or {}

    scroll_and_collect(driver, interceptor, limit, debug)

    html = driver.page_source
    if not interceptor.posts:
        interceptor.posts = parse_next_data(html)
    if not interceptor.posts:
        interceptor.posts = parse_apollo_state(html)
    if not interceptor.posts:
        interceptor.posts = dom_fallback(driver)

    return {
        "author": interceptor.author_info,
        "posts": dedup_posts(interceptor.posts)[:limit],
    }


# ---------------------------------------------------------------------------
# Mode: PUBLICATION
# ---------------------------------------------------------------------------
def scrape_publication(driver, interceptor: SeleniumInterceptor, target: str, limit: int, debug: bool) -> dict:
    slug = target.lstrip("/").lower()
    url = f"{BASE_URL}/{slug}"
    print(f"[{ts()}] Scraping publication: {url}")

    driver.get(url)
    wait_for_page_ready(driver)
    interceptor.harvest_logs()

    if debug:
        Path("debug").mkdir(exist_ok=True)
        driver.save_screenshot(f"debug/publication_{slug}.png")

    if not interceptor.publication_info:
        interceptor.publication_info = driver.execute_script("""
            const nameEl = document.querySelector('h1, [data-testid="publicationName"]');
            const descEl = document.querySelector('[data-testid="publicationDescription"], [class*="description"]');
            return {
                name: nameEl ? nameEl.innerText.trim() : '',
                description: descEl ? descEl.innerText.trim() : '',
                url: window.location.href
            };
        """) or {}

    scroll_and_collect(driver, interceptor, limit, debug)

    html = driver.page_source
    if not interceptor.posts:
        interceptor.posts = parse_next_data(html)
    if not interceptor.posts:
        interceptor.posts = parse_apollo_state(html)
    if not interceptor.posts:
        interceptor.posts = dom_fallback(driver)

    return {
        "publication": interceptor.publication_info,
        "posts": dedup_posts(interceptor.posts)[:limit],
    }


# ---------------------------------------------------------------------------
# Mode: POST
# ---------------------------------------------------------------------------
def scrape_post_detail(driver, url: str, debug: bool) -> dict:
    print(f"[{ts()}] Fetching post: {url}")
    driver.get(url)
    wait_for_page_ready(driver)

    if debug:
        Path("debug").mkdir(exist_ok=True)
        slug = re.sub(r"[^a-z0-9]", "_", url.split("/")[-1])[:30]
        driver.save_screenshot(f"debug/post_{slug}.png")

    ld_json = driver.execute_script("""
        const el = document.querySelector('script[type="application/ld+json"]');
        return el ? el.textContent : null;
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

    content = driver.execute_script("""
        const article = document.querySelector('article');
        if (!article) return '';
        const paras = article.querySelectorAll('p, h1, h2, h3, h4, blockquote');
        return [...paras].map(p => p.innerText.trim()).filter(Boolean).join('\\n\\n');
    """) or ""
    post["content"] = content

    claps_text = driver.execute_script("""
        const btn = document.querySelector('button[aria-label*="clap"]') ||
                    document.querySelector('[class*="clapButton"]');
        return btn ? btn.innerText : '0';
    """) or "0"
    post["claps"] = parse_claps(claps_text)

    return post


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Medium.com scraper — Selenium",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--mode", choices=["tag", "author", "publication", "post"],
                        default="tag")
    parser.add_argument("--target", required=True)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--output", default="")
    parser.add_argument("--format", choices=["json", "csv", "both"], default="json")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--apikey", default=os.getenv("APIKEY_2CAPTCHA", ""))
    parser.add_argument("--auth", action="store_true")
    parser.add_argument("--session-file", default="session.json")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if not args.output:
        safe = re.sub(r"[^a-z0-9_]", "_", args.target.lower().lstrip("@"))[:30]
        args.output = f"medium_{args.mode}_{safe}"

    print(f"[{ts()}] Medium Scraper (Selenium) starting")
    print(f"[{ts()}] Mode: {args.mode} | Target: {args.target} | Limit: {args.limit}")

    driver = build_driver(args.proxy or None, args.headed)
    interceptor = SeleniumInterceptor(driver)

    try:
        # Auth flow
        if args.auth:
            session_exists = os.path.exists(args.session_file) and os.path.getsize(args.session_file) > 10
            if not session_exists:
                do_login(driver, args.session_file, args.debug)
                save_session(driver, args.session_file)
            else:
                load_session(driver, args.session_file)

        # Warmup
        try:
            driver.get(BASE_URL)
            wait_for_page_ready(driver)
        except Exception:
            pass

        # Dispatch
        if args.mode == "tag":
            result = scrape_tag(driver, interceptor, args.target, args.limit, args.debug)
        elif args.mode == "author":
            result = scrape_author(driver, interceptor, args.target, args.limit, args.debug)
        elif args.mode == "publication":
            result = scrape_publication(driver, interceptor, args.target, args.limit, args.debug)
        elif args.mode == "post":
            result = scrape_post_detail(driver, args.target, args.debug)

    finally:
        driver.quit()

    # Save
    records = result if isinstance(result, list) else ([result] if args.mode == "post" else result.get("posts", []))
    out_base = args.output.replace(".json", "").replace(".csv", "")

    if args.format in ("json", "both"):
        save_json(result if args.mode not in ("tag",) else records, out_base + ".json")
    if args.format in ("csv", "both"):
        save_csv(records, out_base + ".csv")

    print(f"[{ts()}] Done. Total posts: {len(records)}")


if __name__ == "__main__":
    main()
