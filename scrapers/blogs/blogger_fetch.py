#!/usr/bin/env python3
"""Blogger scraper — enter a keyword, get posts from across all Blogger blogs.

Uses DuckDuckGo to find blogspot.com posts for the keyword, then scrapes
each post's content using BeautifulSoup + Blogger's JSON feed API.

Usage:
    python scrapers/blogger_fetch.py AI
    python scrapers/blogger_fetch.py "climate change" --limit 10
    python scrapers/blogger_fetch.py Morocco --output results.json
"""

import argparse
import json
import random
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*",
}

session = requests.Session()
session.headers.update(HEADERS)


# Popular active Blogger blogs used as fallback discovery
POPULAR_BLOGS = [
    "techstoriesindia.blogspot.com",
    "googleblog.blogspot.com",
    "thenextweb.blogspot.com",
    "worldnewsdailyreport.blogspot.com",
    "afriquejoural.blogspot.com",
    "moroccolatesttravel.blogspot.com",
    "traveltomodern.blogspot.com",
    "africatravelblog.blogspot.com",
    "scienceblogs.blogspot.com",
    "climateblog.blogspot.com",
    "techcrunchblog.blogspot.com",
    "aiandtech.blogspot.com",
    "healthylivingblog.blogspot.com",
    "politicsdailyblog.blogspot.com",
    "sportnewsdaily.blogspot.com",
]


# ── Step 1: find blogspot.com post URLs ───────────────────────────────────


def find_post_urls(keyword: str, limit: int, api_key: str = None, cx: str = None) -> list[str]:
    """
    Two-step discovery:
    1. Attempt Google Custom Search API for highly accurate results.
    2. Fallback to Google News RSS + Popular Blog endpoints.
    """
    urls = []
    seen = set()

    # --- Strategy 1: Google Custom Search API (Highly Accurate) ---
    if api_key and cx:
        print(f"[Search Engine] Querying Google Custom Search API for '{keyword}'...", file=sys.stderr)
        try:
            from googleapiclient.discovery import build
            service = build("customsearch", "v1", developerKey=api_key, cache_discovery=False)
            
            for start_index in range(1, min(limit + 1, 101), 10):
                req_limit = min(10, limit - len(urls))
                if req_limit <= 0:
                    break
                    
                res = service.cse().list(
                    q=f'site:blogspot.com {keyword}',
                    cx=cx,
                    start=start_index,
                    num=req_limit,
                ).execute()
                
                items = res.get("items", [])
                if not items:
                    break
                    
                for item in items:
                    href = item.get("link")
                    if href and "blogspot.com" in href:
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
            print("[Search] Falling back to generic RSS checking...", file=sys.stderr)

    # --- Strategy 2: Google News RSS → resolve redirects ---
    rss_url = (
        f"https://news.google.com/rss/search"
        f"?q={urllib.parse.quote(keyword)}"
        f"&hl=en-US&gl=US&ceid=US:en"
    )
    print(f"[Google News RSS] {rss_url}", file=sys.stderr)

    try:
        r = session.get(rss_url, timeout=15)
        root = ET.fromstring(r.text)
        items = root.findall(".//item")
        print(
            f"[Google News] Found {len(items)} news items, resolving redirects...",
            file=sys.stderr,
        )

        for item in items:
            if len(urls) >= limit:
                break
            link_el = item.find("link")
            link = link_el.text or (link_el.tail if link_el is not None else None)
            if not link:
                guid_el = item.find("guid")
                link = guid_el.text if guid_el is not None else None
            if not link:
                continue
            real = _resolve_url(link)
            if real and "blogspot.com" in real and real not in seen:
                seen.add(real)
                urls.append(real.split("?")[0])
    except Exception as e:
        print(f"[Google News] Error: {e}", file=sys.stderr)

    # --- Strategy 3: search popular blogs' JSON feeds ---
    if len(urls) < limit:
        print(
            f"[Blogger JSON feeds] Searching curated blogs for '{keyword}'...",
            file=sys.stderr,
        )
        for blog in POPULAR_BLOGS:
            if len(urls) >= limit:
                break
            feed_posts = get_blog_json_feed(f"https://{blog}", keyword)
            for p in feed_posts:
                purl = p.get("url", "")
                if purl and purl not in seen:
                    seen.add(purl)
                    urls.append(purl)

    print(f"[Search] Total found: {len(urls)} post URL(s)", file=sys.stderr)
    return urls[:limit]


def _resolve_url(url: str) -> str | None:
    """Follow redirects to get the final URL."""
    try:
        r = session.get(url, timeout=8, allow_redirects=True)
        return r.url
    except Exception:
        return None


# ── Step 2: try to get full post via Blogger JSON feed ────────────────────────


def get_blog_json_feed(post_url: str, keyword: str) -> list[dict]:
    """
    Given a post URL like https://example.blogspot.com/2024/01/slug.html,
    construct the blog's JSON search feed and fetch matching posts.
    """
    m = re.match(r"(https?://[^/]+\.blogspot\.com)", post_url)
    if not m:
        return []

    blog_base = m.group(1)
    feed_url = (
        f"{blog_base}/feeds/posts/default"
        f"?q={urllib.parse.quote(keyword)}&alt=json&max-results=5"
    )

    try:
        r = session.get(feed_url, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    entries = data.get("feed", {}).get("entry", [])
    results = []
    for entry in entries:
        results.append(_normalize_feed_entry(entry, blog_base))
    return results


def _normalize_feed_entry(entry: dict, blog_base: str) -> dict:
    # Title
    title = entry.get("title", {}).get("$t") or None

    # Body
    content_obj = entry.get("content") or entry.get("summary") or {}
    body_html = content_obj.get("$t", "")
    body = _strip_html(body_html) or None

    # Author
    authors = [
        a.get("name", {}).get("$t")
        for a in entry.get("author", [])
        if a.get("name", {}).get("$t")
    ]

    # Published / updated
    published = entry.get("published", {}).get("$t")
    modified = entry.get("updated", {}).get("$t")

    # URL (rel=alternate)
    url = None
    for link in entry.get("link", []):
        if link.get("rel") == "alternate":
            url = link.get("href")
            break

    # Tags / labels
    tags = [c.get("term") for c in entry.get("category", []) if c.get("term")]

    # Thumbnail
    thumbnail = None
    media = entry.get("media$thumbnail")
    if media:
        thumbnail = media.get("url")
    if not thumbnail:
        # Try to extract first image from body HTML
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', body_html)
        if m:
            thumbnail = m.group(1)

    return {
        "title": title,
        "body": body,
        "author": authors or None,
        "published": published,
        "modified": modified,
        "tags": tags or None,
        "thumbnail": thumbnail,
        "url": url or blog_base,
        "blog_url": blog_base,
        "platform": "blogger",
    }


# ── Step 3: HTML scrape fallback for individual post pages ────────────────────


def scrape_post_html(url: str) -> dict | None:
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  [!] {url} → {e}", file=sys.stderr)
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    # Title
    title = None
    for sel in ["h1.post-title", "h3.post-title", ".entry-title", "h1", "h2.title"]:
        el = soup.select_one(sel)
        if el:
            title = el.get_text(strip=True)
            break

    # Body
    body = None
    for sel in [".post-body", ".entry-content", "#post-body", ".post_body"]:
        el = soup.select_one(sel)
        if el:
            for tag in el.select("script, style"):
                tag.decompose()
            body = el.get_text(separator="\n", strip=True) or None
            break

    # Author
    author = None
    for sel in [".post-author", ".author", ".fn", "[rel='author']"]:
        el = soup.select_one(sel)
        if el:
            author = el.get_text(strip=True)
            break

    # Date
    published = None
    for sel in [
        "abbr.published",
        ".post-timestamp a",
        ".date-header",
        "time[datetime]",
    ]:
        el = soup.select_one(sel)
        if el:
            published = el.get("datetime") or el.get("title") or el.get_text(strip=True)
            break

    # Labels / tags
    tags = [
        a.get_text(strip=True)
        for a in soup.select(".post-labels a, .label a, .labels a")
    ]

    # Thumbnail
    thumbnail = None
    og = soup.find("meta", property="og:image")
    if og:
        thumbnail = og.get("content")
    if not thumbnail:
        img = soup.select_one(".post-body img, .entry-content img")
        if img:
            thumbnail = img.get("src")

    # Blog name
    blog_name = None
    og_site = soup.find("meta", property="og:site_name")
    if og_site:
        blog_name = og_site.get("content")
    if not blog_name:
        el = soup.select_one(".header-title, #header h1, .blog-title")
        if el:
            blog_name = el.get_text(strip=True)

    blog_base = re.match(r"https?://[^/]+\.blogspot\.com", url)
    blog_url = blog_base.group(0) if blog_base else None

    return {
        "title": title,
        "body": body,
        "author": [author] if author else None,
        "published": published,
        "modified": None,
        "tags": tags or None,
        "thumbnail": thumbnail,
        "url": url,
        "blog_name": blog_name,
        "blog_url": blog_url,
        "platform": "blogger",
    }


# ── HTML stripping ─────────────────────────────────────────────────────────────


def _strip_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#?\w+;", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scrapers.base import BaseScraper, ScraperConfig, ScraperResult


class BloggerScraper(BaseScraper):
    platform = "blogger"
    items_key = "posts"

    def validate_config(self, config: ScraperConfig) -> None:
        if not config.keyword.strip():
            raise ValueError("keyword is required")

    def filter_strict(self, keyword: str, item: dict[str, Any]) -> bool:
        kw_lower = keyword.lower()
        title = (item.get("title") or "").lower()
        body = (item.get("body") or "").lower()
        return kw_lower in title or body.count(kw_lower) >= 2

    def scrape(self, config: ScraperConfig) -> ScraperResult:
        self.validate_config(config)
        api_key = config.extra.get("google_api_key")
        cx = config.extra.get("cx")
        url_limit = config.limit * 3 if config.strict else config.limit
        post_urls = find_post_urls(config.keyword, url_limit, api_key, cx)

        posts = []
        seen_urls = set()

        for url in post_urls:
            if len(posts) >= config.limit:
                break

            feed_posts = get_blog_json_feed(url, config.keyword)
            scraped_any = False
            if feed_posts:
                for post in feed_posts:
                    if config.strict and not self.filter_strict(config.keyword, post):
                        continue
                    post_url = post.get("url")
                    if post_url not in seen_urls:
                        seen_urls.add(post_url)
                        posts.append(self.normalize_item(post))
                        scraped_any = True
                    if len(posts) >= config.limit:
                        break

            if not feed_posts or not scraped_any:
                post = scrape_post_html(url)
                if post and (not config.strict or self.filter_strict(config.keyword, post)):
                    if url not in seen_urls:
                        seen_urls.add(url)
                        posts.append(self.normalize_item(post))

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
        description="Search all Blogger blogs by keyword. Returns full posts as JSON.",
    )
    parser.add_argument("keyword", help="Keyword or topic, e.g. AI or Morocco")
    parser.add_argument("--limit", type=int, default=10, help="Max posts (default: 10)")
    parser.add_argument("--output", help="Save JSON to file (default: print to stdout)")
    parser.add_argument("--strict", action="store_true", help="Ensure posts mention keyword explicitly.")
    parser.add_argument("--google-api-key", default="AIzaSyB-DQovimp0EF0_JYDXCXZAzJzotIVeVQw", help="Google API Developer Key")
    parser.add_argument("--cx", default=None, help="Google Custom Search Engine ID (CX)")
    args = parser.parse_args()

    print(f"\nSearching Blogger for: '{args.keyword}'\n", file=sys.stderr)

    # Step 1: find post URLs
    post_urls = find_post_urls(args.keyword, args.limit * 3 if args.strict else args.limit, args.google_api_key, args.cx)
    if not post_urls:
        print("No posts found.", file=sys.stderr)
        sys.exit(1)

    # Step 2: for each URL, try JSON feed first, then HTML scrape
    posts = []
    seen_urls = set()
    kw_lower = args.keyword.lower()

    for url in post_urls:
        if len(posts) >= args.limit:
            break

        print(f"  Fetching: {url}", file=sys.stderr)

        # Try blog JSON feed (gives richer metadata)
        feed_posts = get_blog_json_feed(url, args.keyword)
        
        scraped_any = False
        if feed_posts:
            for p in feed_posts:
                # Strict Check for JSON Output
                title = (p.get("title") or "").lower()
                body = (p.get("body") or "").lower()
                
                if args.strict and kw_lower not in title and body.count(kw_lower) < 2:
                    print(f"    -> Skipped: '{args.keyword}' not prominent enough.", file=sys.stderr)
                    continue
                    
                if p.get("url") not in seen_urls:
                    seen_urls.add(p.get("url"))
                    posts.append(p)
                    scraped_any = True
                    
                    if not args.output:
                        print(json.dumps(p, ensure_ascii=False))
                        sys.stdout.flush()
                        
                    if len(posts) >= args.limit:
                        break
        
        if not feed_posts or not scraped_any:
            # Fallback to HTML scraping of the specific post
            post = scrape_post_html(url)
            
            # Strict Check for HTML Output
            if post:
                title = (post.get("title") or "").lower()
                body = (post.get("body") or "").lower()
                
                if args.strict and kw_lower not in title and body.count(kw_lower) < 2:
                    print(f"    -> Skipped (Fallback): '{args.keyword}' not prominent enough.", file=sys.stderr)
                    continue

                if url not in seen_urls:
                    seen_urls.add(url)
                    posts.append(post)
                    
                    if not args.output:
                        print(json.dumps(post, ensure_ascii=False))
                        sys.stdout.flush()

        time.sleep(random.uniform(0.4, 0.9))

    if not posts:
        print("No posts could be scraped.", file=sys.stderr)
        sys.exit(1)

    print(f"\nCollected {len(posts)} post(s)\n", file=sys.stderr)

    if args.output:
        result = {
            "query": args.keyword,
            "platform": "blogger",
            "count": len(posts),
            "posts": posts,
        }
        output = json.dumps(result, ensure_ascii=False, indent=2)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Saved {len(posts)} posts → {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
