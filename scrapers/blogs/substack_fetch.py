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


def get_post_urls(keyword: str, limit: int) -> list[str]:
    search_url = f"https://substack.com/search?q={urllib.parse.quote(keyword)}"
    print(f"[Search] {search_url}", file=sys.stderr)

    html = _render_with_playwright(search_url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    urls = []
    seen = set()

    # Substack post links: /p/<slug> on a subdomain, or substack.com/p/<slug>
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Match: https://newsletter.substack.com/p/slug or https://substack.com/p/slug
        if re.search(r"substack\.com/p/[a-z0-9_-]+", href):
            url = href.split("?")[0]
            if url not in seen:
                seen.add(url)
                urls.append(url)
        if len(urls) >= limit:
            break

    print(f"[Search] Found {len(urls)} post URL(s)", file=sys.stderr)
    return urls


def _render_with_playwright(url: str, wait_ms: int = 4000) -> str | None:
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
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(wait_ms)
            # Scroll to load lazy results
            for _ in range(3):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1500)
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


# ── Main ───────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Search all Substack newsletters by keyword. Returns full posts as JSON.",
    )
    parser.add_argument("keyword", help="Keyword or topic, e.g. AI or 'climate change'")
    parser.add_argument("--limit", type=int, default=10, help="Max posts (default: 10)")
    parser.add_argument("--output", help="Save JSON to file (default: print to stdout)")
    args = parser.parse_args()

    print(f"\nSearching Substack for: '{args.keyword}'\n", file=sys.stderr)

    post_urls = get_post_urls(args.keyword, args.limit)
    if not post_urls:
        print("No posts found.", file=sys.stderr)
        sys.exit(1)

    print(f"\nFetching {len(post_urls)} post(s)...\n", file=sys.stderr)

    posts = []
    for i, url in enumerate(post_urls, 1):
        print(f"  [{i}/{len(post_urls)}] {url}", file=sys.stderr)
        post = scrape_post(url)
        posts.append(post)
        
        if not args.output:
            print(json.dumps(post, ensure_ascii=False))
            sys.stdout.flush()
            
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
