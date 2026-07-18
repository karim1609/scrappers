#!/usr/bin/env python3
"""Substack scraper — enter a keyword, get posts from across all Substack newsletters.

Uses Playwright to render Substack search, then scrapes each post's
__NEXT_DATA__ JSON for full content and metadata.

Usage:
    python scrapers/substack_fetch.py AI
    python scrapers/substack_fetch.py "climate change" --limit 10
    python scrapers/substack_fetch.py Morocco --output results.json
"""

import argparse
import json
import random
import re
import sys
import time
import urllib.parse
from html.parser import HTMLParser

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

session = requests.Session()
session.headers.update(HEADERS)


# ── HTML stripping ─────────────────────────────────────────────────────────────


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = False
        if tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "blockquote"):
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def text(self):
        return re.sub(r"\n{3,}", "\n\n", "".join(self._parts)).strip()


def strip_html(html: str) -> str:
    if not html:
        return ""
    p = _TextExtractor()
    p.feed(html)
    return p.text()


# ── Step 1: collect post URLs from Substack search ────────────────────────────


def get_post_urls(keyword: str, limit: int, api_key: str = None, cx: str = None) -> list[str]:
    urls = []
    seen = set()

    # Attempt Google Custom Search if keys are present
    if api_key and cx:
        print(f"[Search] Querying Google Custom Search API for '{keyword}'...", file=sys.stderr)
        try:
            from googleapiclient.discovery import build
            service = build("customsearch", "v1", developerKey=api_key, cache_discovery=False)
            
            for start_index in range(1, min(limit + 1, 101), 10):
                req_limit = min(10, limit - len(urls))
                if req_limit <= 0:
                    break
                    
                res = service.cse().list(
                    q=f'site:substack.com/p/ {keyword}',
                    cx=cx,
                    start=start_index,
                    num=req_limit,
                ).execute()
                
                items = res.get("items", [])
                if not items:
                    break
                    
                for item in items:
                    href = item.get("link")
                    if href and "/p/" in href:
                        url = href.split("?")[0]
                        if url not in seen:
                            seen.add(url)
                            urls.append(url)
                            
                if len(urls) >= limit:
                    break
                    
                time.sleep(0.5)
                
            print(f"[Search] Found {len(urls)} post URL(s) via Google Custom Search", file=sys.stderr)
            return urls
            
        except Exception as e:
            print(f"[Search Engine API Error] {e}", file=sys.stderr)
            print("[Search] Falling back to Substack native search...", file=sys.stderr)

    # Fallback / Native Substack Search (Reliable via Playwright)
    search_url = f"https://substack.com/search?q={urllib.parse.quote(keyword)}&focused=posts"
    print(f"[Search] {search_url}", file=sys.stderr)
    
    html = _render_with_playwright(search_url, limit)
    if not html:
        return urls
        
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/p/" in href:
            url = href.split("?")[0]
            if not url.startswith("http"):
                url = urllib.parse.urljoin("https://substack.com", url)
            if url not in seen:
                seen.add(url)
                urls.append(url)
        if len(urls) >= limit:
            break

    print(f"[Search] Found {len(urls)} post URL(s)", file=sys.stderr)
    return urls


def _render_with_playwright(url: str, limit: int, wait_ms: int = 4000) -> str | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed.", file=sys.stderr)
        return None

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(wait_ms)
            
            # Scroll dynamically based on requested limit
            max_scrolls = (limit // 8) + 10
            last_count = 0
            
            for scroll_idx in range(max_scrolls):
                link_loc = page.locator("a[href*='/p/']")
                count = link_loc.count()
                
                if count >= limit:
                    break
                    
                if count > 0:
                    link_loc.last.scroll_into_view_if_needed()
                else:
                    page.keyboard.press("End")
                    
                page.wait_for_timeout(2000)
                
                # If stuck, try jittering
                if count > 0 and count == last_count and scroll_idx > 3:
                    page.keyboard.press("PageUp")
                    page.wait_for_timeout(500)
                    page.keyboard.press("End")
                    page.wait_for_timeout(2000)
                    count2 = link_loc.count()
                    if count2 == count:
                        break
                last_count = count
                
            html = page.content()
            browser.close()
        return html
    except Exception as e:
        print(f"[Playwright] Error: {e}", file=sys.stderr)
        return None


# ── Step 2: scrape each post page ─────────────────────────────────────────────


def scrape_post(url: str) -> dict:
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        return {"url": url, "error": str(e)}

    soup = BeautifulSoup(r.text, "html.parser")

    # Try __NEXT_DATA__ first (most complete)
    nd = soup.find("script", id="__NEXT_DATA__")
    if nd:
        try:
            data = json.loads(nd.string)
            post = data.get("props", {}).get("pageProps", {}).get("post") or data.get(
                "props", {}
            ).get("pageProps", {}).get("initialPost")
            if post:
                return _parse_next_data(post, url)
        except Exception:
            pass

    # HTML fallback
    return _parse_html(soup, url)


def _parse_next_data(post: dict, url: str) -> dict:
    # Body HTML → plain text
    body_html = post.get("body_html") or post.get("bodyHtml") or ""
    body = strip_html(body_html) or None

    # Subtitle / description
    subtitle = post.get("subtitle") or post.get("description") or None

    # Author
    authors = []
    for a in post.get("publishedBylines", []) or [post.get("author")] or []:
        if isinstance(a, dict):
            name = a.get("name")
            if name:
                authors.append(name)
        elif isinstance(a, str):
            authors.append(a)

    # Newsletter name
    publication = (
        _deep(post, "publication", "name")
        or _deep(post, "pub", "name")
        or _deep(post, "newsletter", "name")
    )
    newsletter_url = _deep(post, "publication", "base_url") or _deep(
        post, "pub", "base_url"
    )

    # Tags
    tags = [t.get("name") for t in post.get("postTags", []) or [] if t.get("name")]

    return {
        "title": post.get("title"),
        "subtitle": subtitle,
        "body": body,
        "author": authors or None,
        "published": post.get("post_date") or post.get("publishedAt"),
        "likes": post.get("reactions", {}).get("❤")
        if isinstance(post.get("reactions"), dict)
        else post.get("like_count"),
        "comment_count": post.get("comment_count"),
        "tags": tags or None,
        "thumbnail": post.get("cover_image") or post.get("coverImage"),
        "newsletter_name": publication,
        "newsletter_url": newsletter_url,
        "url": post.get("canonical_url") or url,
        "platform": "substack",
    }


def _parse_html(soup: BeautifulSoup, url: str) -> dict:
    title = None
    for sel in ["h1.post-title", "h1", ".pencraft h1"]:
        el = soup.select_one(sel)
        if el:
            title = el.get_text(strip=True)
            break

    subtitle = None
    for sel in ["h3.subtitle", ".subtitle", "h2.subtitle"]:
        el = soup.select_one(sel)
        if el:
            subtitle = el.get_text(strip=True)
            break

    body = None
    for sel in [
        ".available-content",
        ".body.markup",
        "article .post-content",
        ".post-body",
    ]:
        el = soup.select_one(sel)
        if el:
            body = el.get_text(separator="\n", strip=True)
            break

    author = None
    for sel in [
        ".profile-hover-card-target",
        ".author-name",
        "[data-component='byline']",
    ]:
        el = soup.select_one(sel)
        if el:
            author = el.get_text(strip=True)
            break

    published = None
    t = soup.find("time")
    if t:
        published = t.get("datetime") or t.get_text(strip=True)

    thumbnail = None
    og = soup.find("meta", property="og:image")
    if og:
        thumbnail = og.get("content")

    newsletter_name = None
    og_site = soup.find("meta", property="og:site_name")
    if og_site:
        newsletter_name = og_site.get("content")

    return {
        "title": title,
        "subtitle": subtitle,
        "body": body,
        "author": [author] if author else None,
        "published": published,
        "likes": None,
        "comment_count": None,
        "tags": None,
        "thumbnail": thumbnail,
        "newsletter_name": newsletter_name,
        "newsletter_url": None,
        "url": url,
        "platform": "substack",
    }


# ── Helpers ────────────────────────────────────────────────────────────────────


def _deep(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scrapers.base import BaseScraper, ScraperConfig, ScraperResult


class SubstackScraper(BaseScraper):
    platform = "substack"
    items_key = "posts"

    def validate_config(self, config: ScraperConfig) -> None:
        if not config.keyword.strip():
            raise ValueError("keyword is required")

    def filter_strict(self, keyword: str, item: dict[str, Any]) -> bool:
        if "error" in item:
            return False
        kw_lower = keyword.lower()
        title = (item.get("title") or "").lower()
        subtitle = (item.get("subtitle") or "").lower()
        body = (item.get("body") or "").lower()
        return kw_lower in title or kw_lower in subtitle or body.count(kw_lower) >= 2

    def scrape(self, config: ScraperConfig) -> ScraperResult:
        self.validate_config(config)
        api_key = config.extra.get("google_api_key")
        cx = config.extra.get("cx")
        url_limit = config.limit * 3 if config.strict else config.limit
        post_urls = get_post_urls(config.keyword, url_limit, api_key, cx)

        posts = []
        for url in post_urls:
            post = scrape_post(url)
            if config.strict and not self.filter_strict(config.keyword, post):
                continue
            posts.append(self.normalize_item(post))
            if len(posts) >= config.limit:
                break
            time.sleep(random.uniform(0.4, 0.9))

        return ScraperResult(
            query=config.keyword,
            platform=self.platform,
            count=len(posts),
            items=posts,
        )


# ── Main ───────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Search all Substack newsletters by keyword. Returns full posts as JSON.",
    )
    parser.add_argument("keyword", help="Keyword or topic, e.g. AI or 'climate change'")
    parser.add_argument("--limit", type=int, default=10, help="Max posts (default: 10)")
    parser.add_argument("--output", help="Save JSON to file (default: print to stdout)")
    parser.add_argument("--strict", action="store_true", help="Only keep posts that mention the keyword in the title/subtitle, or multiple times in the body.")
    parser.add_argument("--google-api-key", default="AIzaSyB-DQovimp0EF0_JYDXCXZAzJzotIVeVQw", help="Google API Key")
    parser.add_argument("--cx", default=None, help="Google Custom Search Engine ID (CX)")
    args = parser.parse_args()

    print(f"\nSearching Substack for: '{args.keyword}'\n", file=sys.stderr)

    post_urls = get_post_urls(args.keyword, args.limit * 3 if args.strict else args.limit, args.google_api_key, args.cx)
    if not post_urls:
        print("No posts found.", file=sys.stderr)
        sys.exit(1)

    print(f"\nFetching posts (target limit {args.limit})...\n", file=sys.stderr)

    posts = []
    kw_lower = args.keyword.lower()
    
    for i, url in enumerate(post_urls, 1):
        print(f"  [Checking {i}/{len(post_urls)}] {url}", file=sys.stderr)
        post = scrape_post(url)
        
        # Strict Relevance Filtering
        if args.strict and "error" not in post:
            title = (post.get("title") or "").lower()
            subtitle = (post.get("subtitle") or "").lower()
            body = (post.get("body") or "").lower()
            
            # Substack native search is broad. Strict mode requires the keyword in the title, 
            # subtitle, or appearing at least twice in the actual body.
            if kw_lower not in title and kw_lower not in subtitle and body.count(kw_lower) < 2:
                print(f"    -> Skipped: '{args.keyword}' not prominent enough.", file=sys.stderr)
                continue

        posts.append(post)
        
        if not args.output:
            print(json.dumps(post, ensure_ascii=False))
            sys.stdout.flush()
            
        if len(posts) >= args.limit:
            print(f"\nReached target limit of {args.limit} strictly matched posts.", file=sys.stderr)
            break
            
        time.sleep(random.uniform(0.4, 0.9))

    if args.output:
        result = {
            "query": args.keyword,
            "platform": "substack",
            "count": len(posts),
            "posts": posts,
        }
        output = json.dumps(result, ensure_ascii=False, indent=2)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"\nSaved {len(posts)} posts → {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
