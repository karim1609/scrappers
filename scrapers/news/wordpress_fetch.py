#!/usr/bin/env python3
"""WordPress scraper — enter a keyword, get posts from across all WordPress blogs.

Uses the WordPress.com public search API to find posts from any WordPress-hosted
blog. No site URL needed — just a keyword or topic.

Usage:
    python scrapers/wordpress_fetch.py AI
    python scrapers/wordpress_fetch.py "climate change" --limit 20
    python scrapers/wordpress_fetch.py Morocco --output results.json
"""

import argparse
import json
import re
import sys
import urllib.parse
from html.parser import HTMLParser

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# WordPress.com public search API — searches across ALL WordPress.com blogs
WP_SEARCH_API = "https://public-api.wordpress.com/rest/v1.1/read/search"

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
        raw = "".join(self._parts)
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def strip_html(html: str) -> str:
    if not html:
        return ""
    p = _TextExtractor()
    p.feed(html)
    return p.text()


# ── Fetch posts ────────────────────────────────────────────────────────────────


def fetch_posts(keyword: str, limit: int) -> list[dict]:
    """
    Search across all WordPress.com blogs using the public REST API.
    Paginates automatically until `limit` posts are collected.
    """
    posts = []
    page = 1
    page_size = min(limit, 20)  # API max per request is 20

    print(f"[WordPress.com API] Searching for: '{keyword}'\n", file=sys.stderr)

    while len(posts) < limit:
        params = {
            "q": keyword,
            "number": page_size,
            "offset": (page - 1) * page_size,
            "fields": (
                "ID,site_ID,site_URL,site_name,URL,title,excerpt,content,"
                "author,date,modified,categories,tags,featured_image,"
                "comment_count,like_count,discussion"
            ),
        }

        try:
            r = session.get(WP_SEARCH_API, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"[API] Error on page {page}: {e}", file=sys.stderr)
            break

        raw_posts = data.get("posts", [])
        if not raw_posts:
            break  # no more results

        for post in raw_posts:
            posts.append(_normalize(post))
            if len(posts) >= limit:
                break

        total = data.get("total", 0)
        print(
            f"  Page {page} — fetched {len(raw_posts)} posts "
            f"(total available: {total})",
            file=sys.stderr,
        )

        if len(posts) >= limit or len(posts) >= total:
            break

        page += 1

    return posts


def _normalize(post: dict) -> dict:
    # Author
    author = post.get("author", {})
    author_name = author.get("name") if isinstance(author, dict) else None

    # Categories
    cats_raw = post.get("categories", {})
    categories = list(cats_raw.keys()) if isinstance(cats_raw, dict) else []

    # Tags
    tags_raw = post.get("tags", {})
    tags = list(tags_raw.keys()) if isinstance(tags_raw, dict) else []

    # Body
    body = strip_html(post.get("content", "")) or None

    # Excerpt
    excerpt = strip_html(post.get("excerpt", "")) or None

    # Thumbnail
    thumbnail = post.get("featured_image") or None

    # Comment / like counts
    discussion = post.get("discussion", {})
    comment_count = post.get("comment_count") or (
        discussion.get("comment_count") if isinstance(discussion, dict) else None
    )
    like_count = post.get("like_count")

    return {
        "title": strip_html(post.get("title", "")) or None,
        "body": body,
        "excerpt": excerpt,
        "author": author_name,
        "published": post.get("date"),
        "modified": post.get("modified"),
        "categories": categories or None,
        "tags": tags or None,
        "thumbnail": thumbnail,
        "comment_count": comment_count,
        "like_count": like_count,
        "url": post.get("URL"),
        "site_name": post.get("site_name"),
        "site_url": post.get("site_URL"),
        "platform": "wordpress",
    }


import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scrapers.base import BaseScraper, ScraperConfig, ScraperResult


class WordPressScraper(BaseScraper):
    platform = "wordpress"
    items_key = "posts"

    def validate_config(self, config: ScraperConfig) -> None:
        if not config.keyword.strip():
            raise ValueError("keyword is required")

    def scrape(self, config: ScraperConfig) -> ScraperResult:
        self.validate_config(config)
        posts = fetch_posts(config.keyword, config.limit)
        items = [self.normalize_item(post) for post in posts]
        return ScraperResult(
            query=config.keyword,
            platform=self.platform,
            count=len(items),
            items=items,
        )


# ── Main ───────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Search all WordPress blogs by keyword. Returns full posts as JSON.",
    )
    parser.add_argument("keyword", help="Keyword or topic, e.g. AI or 'climate change'")
    parser.add_argument("--limit", type=int, default=10, help="Max posts (default: 10)")
    parser.add_argument("--output", help="Save JSON to file (default: print to stdout)")
    args = parser.parse_args()

    posts = fetch_posts(args.keyword, args.limit)

    if not posts:
        print("No posts found.", file=sys.stderr)
        sys.exit(1)

    print(f"\nCollected {len(posts)} post(s)\n", file=sys.stderr)

    result = {
        "query": args.keyword,
        "platform": "wordpress",
        "count": len(posts),
        "posts": posts,
    }

    output = json.dumps(result, ensure_ascii=False, indent=2)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Saved {len(posts)} posts → {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
