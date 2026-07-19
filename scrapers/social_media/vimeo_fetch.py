#!/usr/bin/env python3
"""Vimeo scraper — search public videos via the official Vimeo API.

Usage:
    python scrapers/social_media/vimeo_fetch.py adidas
    python scrapers/social_media/vimeo_fetch.py "nike shoes" --limit 20
    python scrapers/social_media/vimeo_fetch.py adidas --output results.json
    python scrapers/social_media/vimeo_fetch.py adidas --sort date --direction desc

Credentials:
    VIMEO_ACCESS_TOKEN   Personal access token from developer.vimeo.com
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import requests

from scrapers.utils.env import get_required_env
from scrapers.utils.http import request_with_retry

API_BASE = "https://api.vimeo.com"
SEARCH_ENDPOINT = f"{API_BASE}/videos"
MAX_PAGE_SIZE = 100
REQUEST_TIMEOUT = 30

VIDEO_FIELDS = ",".join(
    [
        "uri",
        "name",
        "description",
        "link",
        "duration",
        "created_time",
        "modified_time",
        "release_time",
        "stats",
        "user",
        "tags",
        "pictures",
        "width",
        "height",
        "language",
        "privacy",
        "categories",
    ]
)

HEADERS = {
    "Accept": "application/vnd.vimeo.*+json;version=3.4",
    "User-Agent": "vimeo-fetch/1.0",
}


def get_access_token() -> str:
    return get_required_env(
        "VIMEO_ACCESS_TOKEN", 
        hint="generate a new personal access token at https://developer.vimeo.com/apps."
    )


def _video_id(uri: str | None) -> str:
    if not uri:
        return ""
    return uri.rstrip("/").split("/")[-1]


def _best_thumbnail(pictures: dict | None) -> str | None:
    if not pictures:
        return None
    sizes = pictures.get("sizes") or []
    if not sizes:
        return None
    return max(sizes, key=lambda s: (s.get("width") or 0) * (s.get("height") or 0)).get("link")


def normalize_video(item: dict[str, Any], keyword: str, comments_list: list = None) -> dict[str, Any]:
    user = item.get("user") or {}
    stats = item.get("stats") or {}
    tags = [t.get("name") for t in (item.get("tags") or []) if isinstance(t, dict) and t.get("name")]
    categories = [
        c.get("name") for c in (item.get("categories") or []) if isinstance(c, dict) and c.get("name")
    ]

    return {
        "platform": "vimeo",
        "video_id": _video_id(item.get("uri")),
        "keyword": keyword,
        "title": item.get("name") or "",
        "description": item.get("description") or "",
        "url": item.get("link") or "",
        "author": user.get("name") or "",
        "author_url": user.get("link") or "",
        "plays": stats.get("plays"),
        "likes": stats.get("likes"),
        "comments": stats.get("comments"),
        "duration": item.get("duration") or 0,
        "published": item.get("release_time") or item.get("created_time") or "",
        "modified": item.get("modified_time") or "",
        "tags": tags,
        "categories": categories,
        "thumbnail": _best_thumbnail(item.get("pictures")),
        "width": item.get("width"),
        "height": item.get("height"),
        "language": item.get("language"),
        "privacy": (item.get("privacy") or {}).get("view"),
        "comments_list": comments_list or [],
    }


def _parse_api_error(response: requests.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]

    if isinstance(data, dict):
        return data.get("error") or data.get("developer_message") or response.text[:200]
    return response.text[:200]


def api_get(params: dict[str, Any], access_token: str, endpoint: str = SEARCH_ENDPOINT) -> dict[str, Any]:
    headers = {
        **HEADERS,
        "Authorization": f"bearer {access_token}",
    }

    response = request_with_retry(
        requests.get,
        endpoint,
        params=params,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )

    if response.status_code == 401:
        raise RuntimeError(
            "Vimeo API returned 401 Unauthorized. Check VIMEO_ACCESS_TOKEN — "
            "generate a new personal access token at https://developer.vimeo.com/apps."
        )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(f"HTTP {response.status_code}: {_parse_api_error(response)}") from exc

    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected API response format")
    return data

def fetch_comments(video_uri: str, access_token: str, max_comments: int = 15) -> list[dict[str, Any]]:
    if not video_uri:
        return []
        
    comments_url = f"{API_BASE}{video_uri}/comments"
    params = {"per_page": min(max_comments, 100)}
    try:
        data = api_get(params, access_token, endpoint=comments_url)
        items = data.get("data") or []
        
        comment_list = []
        for c in items:
            user = c.get("user") or {}
            comment_list.append({
                "author": user.get("name") or "",
                "text": c.get("text") or "",
                "date": c.get("created_on") or "",
            })
            if len(comment_list) >= max_comments:
                break
        return comment_list
    except Exception as exc:
        print(f"[API] Warning: Could not fetch comments for {video_uri}: {exc}", file=sys.stderr)
        return []

def search(
    keyword: str,
    limit: int = 50,
    sort: str = "relevant",
    direction: str = "desc",
) -> list[dict[str, Any]]:
    access_token = get_access_token()
    videos: list[dict[str, Any]] = []
    page = 1

    print(f"Searching Vimeo for '{keyword}'...", file=sys.stderr)

    while len(videos) < limit:
        per_page = min(MAX_PAGE_SIZE, limit - len(videos))
        params = {
            "query": keyword,
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "direction": direction,
            "fields": VIDEO_FIELDS,
        }

        try:
            data = api_get(params, access_token)
        except RuntimeError as exc:
            print(f"[API] Error on page {page}: {exc}", file=sys.stderr)
            break

        items = data.get("data") or []
        if page == 1:
            total = data.get("total")
            if total is not None:
                print(f"Found {total} videos...", file=sys.stderr)

        if not items:
            break

        for item in items:
            uri = item.get("uri")
            comments_extracted = fetch_comments(uri, access_token) if uri else []
            videos.append(normalize_video(item, keyword, comments_extracted))
            
            if len(videos) >= limit:
                break

        paging = data.get("paging") or {}
        if not paging.get("next") or len(videos) >= limit:
            break

        page += 1

    print(f"Retrieved {len(videos)} video(s).", file=sys.stderr)
    return videos


import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scrapers.base import BaseScraper, ScraperConfig, ScraperResult


class VimeoScraper(BaseScraper):
    platform = "vimeo"
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
            sort=config.extra.get("sort", "relevant"),
            direction=config.extra.get("direction", "desc"),
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
        description="Search Vimeo videos by keyword using the official API.",
    )
    parser.add_argument("keyword", nargs="+", help="Keyword to search, e.g. adidas")
    parser.add_argument("--limit", type=int, default=50, help="Max videos (default: 50)")
    parser.add_argument("--output", help="Save JSON to file (default: print to stdout)")
    parser.add_argument(
        "--sort",
        default="relevant",
        choices=["relevant", "date", "alphabetical", "plays", "likes", "duration", "modified_time"],
        help="Sort order (default: relevant)",
    )
    parser.add_argument(
        "--direction",
        default="desc",
        choices=["asc", "desc"],
        help="Sort direction (default: desc)",
    )
    args = parser.parse_args()

    full_keyword = " ".join(args.keyword)
    config = ScraperConfig(
        keyword=full_keyword,
        limit=args.limit,
        output_path=args.output,
        extra={"sort": args.sort, "direction": args.direction},
    )
    scraper = VimeoScraper()

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
