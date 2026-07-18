#!/usr/bin/env python3
"""Dailymotion scraper — search public videos via the official Dailymotion API.

Usage:
    python scrapers/social_media/dailymotion_fetch.py adidas
    python scrapers/social_media/dailymotion_fetch.py "nike shoes" --limit 20
    python scrapers/social_media/dailymotion_fetch.py adidas --output results.json
    python scrapers/social_media/dailymotion_fetch.py adidas --sort recent

Credentials:
    DAILYMOTION_API_KEY      Dailymotion API client key
    DAILYMOTION_API_SECRET   Dailymotion API client secret
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import requests


API_BASE = "https://api.dailymotion.com"
SEARCH_ENDPOINT = f"{API_BASE}/videos"
TOKEN_ENDPOINT = f"{API_BASE}/oauth/token"
MAX_PAGE_SIZE = 100
REQUEST_TIMEOUT = 30
MAX_RETRIES = 5
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}

VIDEO_FIELDS = ",".join(
    [
        "id",
        "title",
        "description",
        "url",
        "owner.username",
        "views_total",
        "bookmarks_total",
        "comments_total",
        "duration",
        "created_time",
        "tags",
        "thumbnail_url",
        "language",
    ]
)

HEADERS = {
    "User-Agent": "dailymotion-fetch/1.0",
}


def get_access_token() -> str:
    """Fetch an OAuth2 access token using client credentials."""
    client_id = os.environ.get("DAILYMOTION_API_KEY", "").strip()
    client_secret = os.environ.get("DAILYMOTION_API_SECRET", "").strip()

    if not client_id or not client_secret:
        raise RuntimeError(
            "DAILYMOTION_API_KEY and/or DAILYMOTION_API_SECRET are not set. "
            "Please export them as environment variables."
        )

    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret
    }
    
    response = requests.post(TOKEN_ENDPOINT, data=data, timeout=REQUEST_TIMEOUT)
    if response.status_code != 200:
        raise RuntimeError(f"Failed to authenticate with Dailymotion API. HTTP {response.status_code}: {response.text}")
    
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("Successfully authenticated but no access_token found in response.")
    return token


def normalize_video(item: dict[str, Any], keyword: str) -> dict[str, Any]:
    tags = item.get("tags") or []
    
    return {
        "platform": "dailymotion",
        "video_id": item.get("id") or "",
        "keyword": keyword,
        "title": item.get("title") or "",
        "description": item.get("description") or "",
        "url": item.get("url") or "",
        "author": item.get("owner.username") or "",
        "author_url": f"https://www.dailymotion.com/{item.get('owner.username')}" if item.get("owner.username") else "",
        "plays": item.get("views_total"),
        "likes": item.get("bookmarks_total"), 
        "comments": item.get("comments_total"),
        "duration": item.get("duration") or 0,
        "published": item.get("created_time") or "",
        "tags": tags,
        "thumbnail": item.get("thumbnail_url"),
        "language": item.get("language"),
    }


def _parse_api_error(response: requests.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]

    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return error.get("message") or response.text[:200]
        return str(error) or response.text[:200]
    return response.text[:200]


def api_get(params: dict[str, Any], access_token: str) -> dict[str, Any]:
    headers = {
        **HEADERS,
        "Authorization": f"Bearer {access_token}",
    }
    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(
                SEARCH_ENDPOINT,
                params=params,
                headers=headers,
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

        if response.status_code == 401:
            raise RuntimeError(
                "Dailymotion API returned 401 Unauthorized. Check your API Key and Secret."
            )

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(f"HTTP {response.status_code}: {_parse_api_error(response)}") from exc

        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("Unexpected API response format")
        return data

    if last_error:
        raise RuntimeError(f"Request timed out after {MAX_RETRIES} retries") from last_error
    raise RuntimeError(f"Request failed after {MAX_RETRIES} retries")


def search(
    keyword: str,
    limit: int = 50,
    sort: str = "relevance",
) -> list[dict[str, Any]]:
    access_token = get_access_token()
    videos: list[dict[str, Any]] = []
    page = 1

    print(f"Searching Dailymotion for '{keyword}'...", file=sys.stderr)

    while len(videos) < limit:
        per_page = min(MAX_PAGE_SIZE, limit - len(videos))
        params = {
            "search": keyword,
            "page": page,
            "limit": per_page,
            "sort": sort,
            "fields": VIDEO_FIELDS,
        }

        try:
            data = api_get(params, access_token)
        except RuntimeError as exc:
            print(f"[API] Error on page {page}: {exc}", file=sys.stderr)
            break

        items = data.get("list") or []
        if page == 1:
            total = data.get("total")
            if total is not None:
                print(f"Found {total} videos...", file=sys.stderr)

        if not items:
            break

        for item in items:
            videos.append(normalize_video(item, keyword))
            if len(videos) >= limit:
                break

        if not data.get("has_more") or len(videos) >= limit:
            break

        page += 1

    print(f"Retrieved {len(videos)} video(s).", file=sys.stderr)
    return videos


import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scrapers.base import BaseScraper, ScraperConfig, ScraperResult


class DailymotionScraper(BaseScraper):
    platform = "dailymotion"
    items_key = "videos"

    def validate_config(self, config: ScraperConfig) -> None:
        if not config.keyword.strip():
            raise ValueError("keyword is required")
        get_access_token()

    def scrape(self, config: ScraperConfig) -> ScraperResult:
        self.validate_config(config)
        videos = search(
            config.keyword,
            config.limit,
            sort=config.extra.get("sort", "relevance"),
        )
        items = [self.normalize_item(video) for video in videos]
        return ScraperResult(
            query=config.keyword,
            platform=self.platform,
            count=len(items),
            items=items,
        )


def main():
    parser = argparse.ArgumentParser(
        description="Search Dailymotion videos by keyword using the official API.",
    )
    parser.add_argument("keyword", nargs="+", help="Keyword to search, e.g. adidas")
    parser.add_argument("--limit", type=int, default=50, help="Max videos (default: 50)")
    parser.add_argument("--output", help="Save JSON to file (default: print to stdout)")
    parser.add_argument(
        "--sort",
        default="relevance",
        choices=["relevance", "recent", "visited", "random"],
        help="Sort order (default: relevance)",
    )
    args = parser.parse_args()

    full_keyword = " ".join(args.keyword)
    config = ScraperConfig(
        keyword=full_keyword,
        limit=args.limit,
        output_path=args.output,
        extra={"sort": args.sort},
    )
    scraper = DailymotionScraper()

    try:
        result = scraper.scrape(config)
    except Exception as exc:
        print(f"[Error] {exc}", file=sys.stderr)
        sys.exit(1)

    if not result.items:
        print("No videos found.", file=sys.stderr)
        sys.exit(1)

    if args.output:
        output_data = scraper.to_json(result)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
        print(f"Saved {result.count} videos → {args.output}", file=sys.stderr)
    else:
        for video in result.items:
            print(json.dumps(video, ensure_ascii=False))
            sys.stdout.flush()


if __name__ == "__main__":
    main()
