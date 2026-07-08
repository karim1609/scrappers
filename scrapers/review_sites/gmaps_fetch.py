#!/usr/bin/env python3
"""
gmaps_fetch.py

Fetch Google Maps reviews for a place using the SerpAPI Google Maps endpoints.

Usage:
    python gmaps_fetch.py "Eiffel Tower Paris" --limit 50
    python gmaps_fetch.py "Starbucks Times Square" --limit 20 --output reviews.json
    python gmaps_fetch.py "ChIJ..." --limit 10   # direct Google place_id

Credentials:
    SERPAPI_KEY   SerpAPI key (required unless the default is set in code)

Streaming behavior:
    - No --output: each review is printed as one JSON line (JSONL) to stdout,
      flushed immediately. All logs go to stderr.
    - With --output: a single JSON object {query, platform, place, count, reviews}
      is written to the given file path.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("gmaps_fetch")

SERPAPI_BASE_URL = "https://serpapi.com/search"
DEFAULT_API_KEY = "83daa33ba1304ef1e8d0b7664938c9d63d42d76f09a3d0e010c1e3bc3242d9ac"
REQUEST_TIMEOUT = 30
PAGE_DELAY_SECONDS = 2


def get_api_key() -> str:
    api_key = os.environ.get("SERPAPI_KEY", DEFAULT_API_KEY).strip()
    if not api_key:
        raise RuntimeError("SerpAPI key is required. Set the SERPAPI_KEY environment variable.")
    return api_key


def is_place_id(text: str) -> bool:
    return text.startswith("ChI") and len(text) > 10


def serpapi_get(params: Dict[str, Any]) -> Dict[str, Any]:
    response = requests.get(SERPAPI_BASE_URL, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data


def resolve_place(keyword: str, api_key: str) -> Dict[str, Any]:
    if is_place_id(keyword):
        log.info("Using provided place_id: %s", keyword)
        return {
            "place_id": keyword,
            "title": None,
            "address": None,
            "rating": None,
            "reviews_count": None,
            "type": None,
        }

    log.info("Searching Google Maps for %r", keyword)
    data = serpapi_get(
        {
            "engine": "google_maps",
            "q": keyword,
            "type": "search",
            "api_key": api_key,
        }
    )

    places = data.get("local_results") or []
    place = places[0] if places else data.get("place_results") or {}
    place_id = place.get("place_id")
    if not place_id:
        raise RuntimeError(f"No Google Maps place found for {keyword!r}")

    return {
        "place_id": place_id,
        "title": place.get("title"),
        "address": place.get("address"),
        "rating": place.get("rating"),
        "reviews_count": place.get("reviews"),
        "type": place.get("type"),
        "gps_coordinates": place.get("gps_coordinates"),
        "website": place.get("website"),
    }


def normalize_review(raw: Dict[str, Any], place: Dict[str, Any], keyword: str) -> Dict[str, Any]:
    user = raw.get("user") or {}
    rating = raw.get("rating")
    try:
        rating = float(rating) if rating is not None else None
    except (TypeError, ValueError):
        pass

    body = raw.get("snippet") or raw.get("extracted_snippet", {}).get("original") or raw.get("text")
    review_id = raw.get("review_id") or raw.get("link")

    return {
        "review_id": review_id,
        "title": None,
        "body": body,
        "rating": rating,
        "date": raw.get("date") or raw.get("iso_date"),
        "author": user.get("name"),
        "author_profile": user.get("link"),
        "author_thumbnail": user.get("thumbnail"),
        "author_reviews_count": user.get("reviews"),
        "author_photos_count": user.get("photos"),
        "likes": raw.get("likes"),
        "images": raw.get("images"),
        "response": raw.get("response"),
        "place_name": place.get("title"),
        "place_id": place.get("place_id"),
        "place_address": place.get("address"),
        "keyword": keyword,
        "platform": "google_maps",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_reviews(
    place: Dict[str, Any],
    keyword: str,
    api_key: str,
    limit: int,
    hl: str,
    sort_by: str,
    emit_fn: Callable[[dict], None],
) -> int:
    place_id = place["place_id"]
    count = 0
    next_page_token: Optional[str] = None
    page_num = 0

    while count < limit:
        page_num += 1
        params: Dict[str, Any] = {
            "engine": "google_maps_reviews",
            "api_key": api_key,
            "place_id": place_id,
            "hl": hl,
            "sort_by": sort_by,
        }
        if next_page_token:
            params["next_page_token"] = next_page_token

        log.info(
            "Fetching reviews page %d for place_id=%s (collected=%d/%d)",
            page_num,
            place_id,
            count,
            limit,
        )
        data = serpapi_get(params)
        reviews = data.get("reviews") or []
        if not reviews:
            log.info("No reviews returned on page %d.", page_num)
            break

        for raw in reviews:
            if count >= limit:
                break
            emit_fn(normalize_review(raw, place, keyword))
            count += 1

        if count >= limit:
            break

        next_page_token = (data.get("serpapi_pagination") or {}).get("next_page_token")
        if not next_page_token:
            log.info("No further review pages available.")
            break

        time.sleep(PAGE_DELAY_SECONDS)

    return count


def scrape(
    keyword: str,
    limit: int,
    emit_fn: Callable[[dict], None],
    hl: str,
    sort_by: str,
) -> tuple[Dict[str, Any], int]:
    api_key = get_api_key()
    place = resolve_place(keyword, api_key)
    log.info(
        "Resolved place: %s (place_id=%s, rating=%s, reviews=%s)",
        place.get("title") or keyword,
        place.get("place_id"),
        place.get("rating"),
        place.get("reviews_count"),
    )
    count = fetch_reviews(place, keyword, api_key, limit, hl, sort_by, emit_fn)
    return place, count


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Google Maps reviews via SerpAPI.")
    parser.add_argument(
        "keyword",
        help="Place name to search for, or a direct Google place_id (ChIJ...)",
    )
    parser.add_argument("--limit", type=int, default=50, help="Max reviews to fetch (default: 50)")
    parser.add_argument("--output", default=None, help="Optional file path to save JSON output")
    parser.add_argument("--hl", default="fr", help="Review language/locale code (default: fr)")
    parser.add_argument(
        "--sort-by",
        default="newestFirst",
        choices=["newestFirst", "mostRelevant", "highestRating", "lowestRating"],
        help="Review sort order (default: newestFirst)",
    )
    args = parser.parse_args()

    log.info(
        "Starting Google Maps review scrape for %r (limit=%d, hl=%s, sort_by=%s)",
        args.keyword,
        args.limit,
        args.hl,
        args.sort_by,
    )

    try:
        if args.output:
            collected: List[Dict[str, Any]] = []

            def emit(review: dict) -> None:
                collected.append(review)
                log.info("Collected review %d/%d", len(collected), args.limit)

            place, count = scrape(args.keyword, args.limit, emit, args.hl, args.sort_by)

            result = {
                "query": args.keyword,
                "platform": "google_maps",
                "place": place,
                "count": count,
                "reviews": collected,
            }
            with open(args.output, "w", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2)
            log.info("Wrote %d reviews to %s", count, args.output)
        else:
            def emit(review: dict) -> None:
                print(json.dumps(review, ensure_ascii=False))
                sys.stdout.flush()

            _, count = scrape(args.keyword, args.limit, emit, args.hl, args.sort_by)
            log.info("Done. %d review(s) streamed.", count)
    except requests.HTTPError as exc:
        log.error("SerpAPI HTTP error: %s", exc)
        return 1
    except Exception as exc:
        log.error("Google Maps scrape failed: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
