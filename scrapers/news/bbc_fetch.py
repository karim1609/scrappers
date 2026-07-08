#!/usr/bin/env python3
"""BBC News scraper — enter a topic or keyword, get full articles as JSON.

Usage:
    python scrapers/bbc_fetch.py Morocco
    python scrapers/bbc_fetch.py Morocco --limit 20
    python scrapers/bbc_fetch.py Morocco --output results.json
    python scrapers/bbc_fetch.py technology          # built-in RSS topic

Built-in RSS topics (instant, no browser):
    top, world, uk, business, politics, health,
    education, science, technology, entertainment, sport
"""

import argparse
import json
import random
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

BASE_UK = "https://www.bbc.co.uk"
BASE_COM = "https://www.bbc.com"

RSS_TOPICS = {
    "top": "https://feeds.bbci.co.uk/news/rss.xml",
    "world": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "uk": "https://feeds.bbci.co.uk/news/uk/rss.xml",
    "business": "https://feeds.bbci.co.uk/news/business/rss.xml",
    "politics": "https://feeds.bbci.co.uk/news/politics/rss.xml",
    "health": "https://feeds.bbci.co.uk/news/health/rss.xml",
    "education": "https://feeds.bbci.co.uk/news/education/rss.xml",
    "science": "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
    "technology": "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "entertainment": "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml",
    "sport": "https://feeds.bbci.co.uk/sport/rss.xml",
}

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


# ── Step 1: collect article URLs ───────────────────────────────────────────────


def get_article_urls(keyword: str, limit: int) -> tuple[list[str], str]:
    """Return (list_of_article_urls, strategy_name)."""

    # Built-in RSS topic
    if keyword.lower() in RSS_TOPICS:
        urls = _urls_from_rss(RSS_TOPICS[keyword.lower()], limit)
        return urls, "rss_topic"

    # Search page → find topic RSS → fallback to search pages
    urls = _urls_from_search(keyword, limit)
    return urls, "search"


def _urls_from_rss(rss_url: str, limit: int) -> list[str]:
    print(f"[RSS] {rss_url}", file=sys.stderr)
    try:
        r = session.get(rss_url, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.text)
    except Exception as e:
        print(f"RSS error: {e}", file=sys.stderr)
        return []

    ns = {"media": "http://search.yahoo.com/mrss/"}
    urls = []
    for item in root.findall(".//item"):
        link = _xml_text(item, "link") or _xml_text(item, "guid") or ""
        link = link.split("?")[0]
        if link and "bbc" in link and "/videos/" not in link:
            urls.append(link)
        if len(urls) >= limit:
            break
    return urls


def _urls_from_search(keyword: str, limit: int) -> list[str]:
    """
    1. Render BBC search page with Playwright.
    2. Find topic RSS link → fetch RSS for clean article URLs.
    3. If no topic → scrape search result pages directly.
    """
    search_url = f"{BASE_UK}/search?q={urllib.parse.quote(keyword)}&filter=news"
    print(f"[Search] {search_url}", file=sys.stderr)

    html = _render_with_playwright(search_url)
    if not html:
        return []

    # --- Try topic RSS first (most reliable) ---
    topic_id = _find_topic_id(html)
    if topic_id:
        rss_url = f"https://feeds.bbci.co.uk/news/topics/{topic_id}/rss.xml"
        urls = _urls_from_rss(rss_url, limit)
        if urls:
            return urls

    # --- Fallback: collect article links from search result pages ---
    print("[Search] Scraping search result pages...", file=sys.stderr)
    urls = _article_links_from_html(html)

    # Paginate if we need more
    page = 2
    while len(urls) < limit and page <= 10:
        paged_url = (
            f"{BASE_UK}/search?q={urllib.parse.quote(keyword)}&filter=news&page={page}"
        )
        paged_html = _render_with_playwright(paged_url)
        if not paged_html:
            break
        new = _article_links_from_html(paged_html)
        if not new:
            break
        for u in new:
            if u not in urls:
                urls.append(u)
        page += 1

    return urls[:limit]


def _find_topic_id(html: str) -> str | None:
    """Extract BBC topic ID from any /news/topics/<id>/ link in the HTML."""
    for m in re.finditer(r"/news/topics/([a-z0-9]+)/", html):
        return m.group(1)
    return None


def _article_links_from_html(html: str) -> list[str]:
    """Extract article URLs (not videos) from rendered BBC HTML."""
    urls = []
    seen = set()
    for m in re.finditer(r'href=["\']([^"\']*(?:/articles/|/news/)[^"\']+)["\']', html):
        url = m.group(1).split("?")[0]
        if url.startswith("/"):
            url = BASE_UK + url
        if "bbc" not in url:
            continue
        if "/videos/" in url or "/live/" in url or "/topics/" in url:
            continue
        # Must look like a real article (ends with alphanumeric slug)
        if not re.search(r"/[a-z0-9-]{6,}$", url):
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _render_with_playwright(url: str) -> str | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Playwright not installed: pip install playwright && python -m playwright install chromium",
            file=sys.stderr,
        )
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
            page.wait_for_timeout(3000)
            html = page.content()
            browser.close()
        return html
    except Exception as e:
        print(f"Playwright error: {e}", file=sys.stderr)
        return None


# ── Step 2: scrape each article page ──────────────────────────────────────────


def scrape_article(url: str) -> dict:
    """Fetch one BBC article and return all metadata."""
    # Normalise domain
    url = url.replace("www.bbc.com/news", "www.bbc.co.uk/news")
    url = url.replace("www.bbc.com/sport", "www.bbc.co.uk/sport")

    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        return {"url": url, "error": str(e)}

    soup = BeautifulSoup(r.text, "html.parser")

    # Try __NEXT_DATA__ JSON first (cleanest source)
    nd = soup.find("script", id="__NEXT_DATA__")
    if nd:
        try:
            data = json.loads(nd.string)
            pp = data.get("props", {}).get("pageProps", {})
            article = (
                pp.get("article")
                or pp.get("data", {}).get("article")
                or pp.get("initialData", {}).get("data", {}).get("article")
            )
            if article:
                return _parse_next_data(article, url)
        except Exception:
            pass

    # HTML fallback
    return _parse_html(soup, url)


def _parse_next_data(article: dict, url: str) -> dict:
    def blocks_to_text(blocks):
        parts = []
        for b in blocks or []:
            t = b.get("type", "")
            if t in ("paragraph", "text"):
                parts.append(b.get("text") or b.get("model", {}).get("text", ""))
            elif t == "crosshead":
                parts.append("\n## " + (b.get("text") or ""))
            elif "model" in b:
                m = b["model"]
                if "blocks" in m:
                    parts.append(blocks_to_text(m["blocks"]))
                elif "text" in m:
                    parts.append(m["text"])
        return "\n\n".join(p for p in parts if p)

    content = article.get("content", {}).get("model", {}).get("blocks", [])
    body = blocks_to_text(content) or None

    authors = [
        c.get("name") or _deep(c, "contributor", "name")
        for c in article.get("contributors", [])
        if c.get("name") or _deep(c, "contributor", "name")
    ]

    tags = [t.get("label") or t.get("name") for t in article.get("topics", []) if t]

    thumbnail = (
        _deep(article, "image", "url")
        or _deep(article, "indexImage", "url")
        or _deep(article, "thumbnail", "url")
    )

    return {
        "title": article.get("headline") or article.get("title"),
        "body": body,
        "author": authors or None,
        "published": article.get("firstPublished") or article.get("publishedDate"),
        "modified": article.get("lastPublished") or article.get("modifiedDate"),
        "section": article.get("section") or _deep(article, "topic", "title"),
        "tags": [t for t in tags if t] or None,
        "thumbnail": thumbnail,
        "url": url,
        "source": "BBC News",
        "platform": "bbc",
    }


def _parse_html(soup: BeautifulSoup, url: str) -> dict:
    # Title
    title = None
    for sel in ["h1", "[data-component='headline-block'] h1"]:
        el = soup.select_one(sel)
        if el:
            title = el.get_text(strip=True)
            break

    # Body
    paras = []
    seen_p = set()
    for el in soup.select("[data-component='text-block'] p, article p"):
        t = el.get_text(strip=True)
        if t and len(t) > 20 and t not in seen_p:
            seen_p.add(t)
            paras.append(t)
    body = "\n\n".join(paras) or None

    # Author
    author = None
    for sel in ["[data-component='byline-block']", ".byline", "[class*='Byline']"]:
        el = soup.select_one(sel)
        if el:
            author = [el.get_text(strip=True)]
            break

    # Dates
    published = modified = None
    for i, t in enumerate(soup.find_all("time")):
        dt = t.get("datetime")
        if dt:
            if i == 0:
                published = dt
            else:
                modified = dt

    # Section
    section = None
    bc = soup.select("nav[aria-label*='breadcrumb'] a, [data-component='breadcrumb'] a")
    if bc:
        section = bc[-1].get_text(strip=True)

    # Tags
    tags = [
        a.get_text(strip=True)
        for a in soup.select("[data-component='tag-list-block'] a")
    ]

    # Thumbnail
    thumbnail = None
    for sel in [
        "[data-component='image-block'] img",
        "article img",
        "meta[property='og:image']",
    ]:
        el = soup.select_one(sel)
        if el:
            thumbnail = el.get("src") or el.get("content") or el.get("data-src")
            if thumbnail:
                break

    return {
        "title": title,
        "body": body,
        "author": author,
        "published": published,
        "modified": modified,
        "section": section,
        "tags": tags or None,
        "thumbnail": thumbnail,
        "url": url,
        "source": "BBC News",
        "platform": "bbc",
    }


# ── Helpers ────────────────────────────────────────────────────────────────────


def _xml_text(el, tag: str) -> str | None:
    c = el.find(tag)
    return c.text.strip() if c is not None and c.text else None


def _deep(d: dict, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


# ── Main ───────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Enter a topic or keyword — get full BBC articles with metadata.",
        epilog=f"Built-in RSS topics: {', '.join(RSS_TOPICS)}",
    )
    parser.add_argument("keyword", help="Topic or keyword, e.g. Morocco or technology")
    parser.add_argument(
        "--limit", type=int, default=10, help="Max articles (default: 10)"
    )
    parser.add_argument("--output", help="Save JSON to file (default: print to stdout)")
    args = parser.parse_args()

    print(f"\nSearching BBC for: '{args.keyword}' ...\n", file=sys.stderr)

    article_urls, strategy = get_article_urls(args.keyword, args.limit)

    if not article_urls:
        print("No articles found.", file=sys.stderr)
        sys.exit(1)

    print(
        f"Found {len(article_urls)} article(s) — fetching full content...\n",
        file=sys.stderr,
    )

    articles = []
    for i, url in enumerate(article_urls, 1):
        print(f"  [{i}/{len(article_urls)}] {url}", file=sys.stderr)
        article = scrape_article(url)
        articles.append(article)
        
        if not args.output:
            # Stream directly to stdout in real-time (JSON Lines format)
            print(json.dumps(article, ensure_ascii=False))
            sys.stdout.flush()
            
        time.sleep(random.uniform(0.4, 0.9))

    if args.output:
        result = {
            "query": args.keyword,
            "platform": "bbc",
            "strategy": strategy,
            "count": len(articles),
            "articles": articles,
        }
        output = json.dumps(result, ensure_ascii=False, indent=2)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"\nSaved {len(articles)} articles → {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
