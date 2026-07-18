#!/usr/bin/env python3
"""Stack Exchange scraper — search questions via the official Stack Exchange REST API.

Uses the /search/advanced endpoint to find questions across Stack Exchange sites.

Usage:
    python scrapers/social_media/stackexchange_fetch.py adidas
    python scrapers/social_media/stackexchange_fetch.py "nike shoes"
    python scrapers/social_media/stackexchange_fetch.py adidas --limit 20
    python scrapers/social_media/stackexchange_fetch.py adidas --output results.json
    python scrapers/social_media/stackexchange_fetch.py adidas --site stackoverflow
    python scrapers/social_media/stackexchange_fetch.py adidas --site superuser
    python scrapers/social_media/stackexchange_fetch.py adidas --strict
"""

import argparse
import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

import requests

API_BASE = "https://api.stackexchange.com/2.3"
SEARCH_ENDPOINT = f"{API_BASE}/search/advanced"
MAX_PAGE_SIZE = 100
REQUEST_TIMEOUT = 30
MAX_RETRIES = 5
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}

HEADERS = {
    "User-Agent": "stackexchange-fetch/1.0 (+https://github.com/local/stackexchange-fetch)",
    "Accept": "application/json",
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
        if tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "blockquote", "pre"):
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def handle_entityref(self, name):
        self._parts.append(html.unescape(f"&{name};"))

    def handle_charref(self, name):
        self._parts.append(html.unescape(f"&#{name};"))

    def text(self):
        return re.sub(r"\n{3,}", "\n\n", "".join(self._parts)).strip()


def strip_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    parser = _TextExtractor()
    parser.feed(raw_html)
    return parser.text()


# ── Helpers ────────────────────────────────────────────────────────────────────


def unix_to_iso(ts: int | float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def passes_strict_filter(keyword: str, title: str, body: str) -> bool:
    kw_lower = keyword.lower()
    title_lower = (title or "").lower()
    body_lower = (body or "").lower()
    return kw_lower in title_lower or body_lower.count(kw_lower) >= 2


def normalize_question(item: dict, site: str) -> dict:
    owner = item.get("owner") or {}
    author = owner.get("display_name") or ""

    return {
        "title": html.unescape(item.get("title") or ""),
        "body": strip_html(item.get("body") or ""),
        "author": author,
        "score": item.get("score") or 0,
        "answer_count": item.get("answer_count") or 0,
        "view_count": item.get("view_count") or 0,
        "accepted_answer": item.get("accepted_answer_id") is not None,
        "tags": item.get("tags") or [],
        "published": unix_to_iso(item.get("creation_date")),
        "last_activity": unix_to_iso(item.get("last_activity_date")),
        "url": item.get("link") or "",
        "site": site,
        "platform": "stackexchange",
    }


def _handle_api_backoff(data: dict) -> None:
    backoff = data.get("backoff")
    if backoff:
        wait = float(backoff)
        print(f"[API] Backoff requested — sleeping {wait:.0f}s", file=sys.stderr)
        time.sleep(wait)


def _parse_api_response(response: requests.Response) -> dict:
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("Invalid JSON response from Stack Exchange API") from exc

    if not isinstance(data, dict):
        raise RuntimeError("Unexpected API response format")

    if data.get("error_id"):
        message = data.get("error_message") or f"API error {data.get('error_id')}"
        raise RuntimeError(message)

    return data


def api_get(params: dict) -> dict:
    """GET the search/advanced endpoint with retries and backoff handling."""
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(
                SEARCH_ENDPOINT,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.Timeout as exc:
            last_error = exc
            wait = 2 ** attempt
            print(f"[API] Timeout — retrying in {wait}s ({attempt + 1}/{MAX_RETRIES})", file=sys.stderr)
            time.sleep(wait)
            continue
        except requests.RequestException as exc:
            raise RuntimeError(f"Network error: {exc}") from exc

        if response.status_code in RETRY_STATUS_CODES:
            wait = 2 ** attempt
            print(
                f"[API] HTTP {response.status_code} — retrying in {wait}s "
                f"({attempt + 1}/{MAX_RETRIES})",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}") from exc

        data = _parse_api_response(response)
        _handle_api_backoff(data)
        return data

    if last_error:
        raise RuntimeError(f"Request timed out after {MAX_RETRIES} retries") from last_error
    raise RuntimeError(f"Request failed after {MAX_RETRIES} retries")


# ── Fetch questions ────────────────────────────────────────────────────────────


def fetch_questions(keyword: str, site: str, limit: int, strict: bool) -> list[dict]:
    posts: list[dict] = []
    page = 1
    total_available = None

    print("Searching Stack Exchange...", file=sys.stderr)

    while len(posts) < limit:
        page_size = min(MAX_PAGE_SIZE, limit - len(posts))
        if strict:
            page_size = MAX_PAGE_SIZE

        params = {
            "q": keyword,
            "site": site,
            "page": page,
            "pagesize": page_size,
            "order": "desc",
            "sort": "activity",
            "filter": "withbody",
        }

        try:
            data = api_get(params)
        except RuntimeError as exc:
            print(f"[API] Error on page {page}: {exc}", file=sys.stderr)
            break

        quota_remaining = data.get("quota_remaining")
        if quota_remaining is not None and quota_remaining <= 0:
            print("[API] Quota exceeded.", file=sys.stderr)
            break

        items = data.get("items") or []
        if total_available is None:
            total_available = data.get("total", len(items))
            print(f"Found {total_available} questions...", file=sys.stderr)
            print("Fetching...", file=sys.stderr)

        if not items:
            break

        for item in items:
            post = normalize_question(item, site)

            if strict and not passes_strict_filter(keyword, post["title"], post["body"]):
                continue

            posts.append(post)
            if len(posts) >= limit:
                break

        if len(posts) >= limit:
            break

        if not data.get("has_more"):
            break

        page += 1

    return posts


import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scrapers.base import BaseScraper, ScraperConfig, ScraperResult


class StackExchangeScraper(BaseScraper):
    platform = "stackexchange"
    items_key = "posts"

    def validate_config(self, config: ScraperConfig) -> None:
        if not config.keyword.strip():
            raise ValueError("keyword is required")

    def filter_strict(self, keyword: str, item: dict[str, Any]) -> bool:
        return passes_strict_filter(keyword, item.get("title", ""), item.get("body", ""))

    def scrape(self, config: ScraperConfig) -> ScraperResult:
        self.validate_config(config)
        site = config.extra.get("site", "stackoverflow")
        posts = fetch_questions(config.keyword, site, config.limit, config.strict)
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
        description="Search Stack Exchange questions by keyword. Returns JSON.",
    )
    parser.add_argument("keyword", help="Keyword or topic, e.g. adidas or 'nike shoes'")
    parser.add_argument("--limit", type=int, default=10, help="Max questions (default: 10)")
    parser.add_argument("--output", help="Save JSON to file (default: print to stdout)")
    parser.add_argument(
        "--site",
        default="stackoverflow",
        help="Stack Exchange site name (default: stackoverflow)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Only keep posts where the keyword is in the title or appears at least twice in the body.",
    )
    args = parser.parse_args()

    print(f"\nSearching Stack Exchange ({args.site}) for: '{args.keyword}'\n", file=sys.stderr)

    config = ScraperConfig(
        keyword=args.keyword,
        limit=args.limit,
        strict=args.strict,
        output_path=args.output,
        extra={"site": args.site}
    )
    scraper = StackExchangeScraper()

    try:
        scraper_result = scraper.scrape(config)
        posts = scraper_result.items
    except Exception as exc:
        print(f"[Error] {exc}", file=sys.stderr)
        sys.exit(1)

    if not posts:
        print("No questions found.", file=sys.stderr)
        sys.exit(1)

    print(f"\nCollected {len(posts)} question(s)", file=sys.stderr)
    print("Done.", file=sys.stderr)

    if args.output:
        output_data = scraper.to_json(scraper_result)
        output = json.dumps(output_data, ensure_ascii=False, indent=2)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"\nSaved {len(posts)} questions → {args.output}", file=sys.stderr)
    else:
        for post in posts:
            print(json.dumps(post, ensure_ascii=False))
            sys.stdout.flush()


if __name__ == "__main__":
    main()
