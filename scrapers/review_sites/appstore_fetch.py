#!/usr/bin/env python3
"""
appstore_fetch.py

Scrape App Store reviews for a given app name using Apple's official JSON API.

Usage:
    python appstore_fetch.py "Nike" --limit 50
    python appstore_fetch.py "Nike" --limit 100 --output reviews.json

Strategy:
    1. iTunes Search API (find an app by name).
    2. iTunes Customer Reviews JSON feed (official, per-app, paginated).
    3. Stream real-time output as JSONL for Dockerized environment pipeline.
"""

import argparse
import json
import sys
import time
import logging
from typing import Optional, List, Dict, Any
from urllib.parse import quote_plus
from datetime import datetime, timezone

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("appstore_fetch")

SEARCH_URL = "https://itunes.apple.com/search"
REVIEWS_URL_TEMPLATE = (
    "https://itunes.apple.com/{country}/rss/customerreviews/id={app_id}/sortBy=mostRecent/page={page}/json"
)
MAX_PAGES = 10          # Apple's hard limit for this endpoint
REVIEWS_PER_PAGE = 50   # Apple's fixed page size
REQUEST_TIMEOUT = 15
POLITE_DELAY_SECS = 1.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def search_app(app_name: str, country: str) -> Optional[dict]:
    log.info(f"Searching the App Store for '{app_name}' (country={country})...")
    params = {
        "term": app_name,
        "entity": "software",
        "country": country,
        "limit": 5,
    }
    try:
        response = requests.get(SEARCH_URL, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        log.error(f"Could not reach the iTunes Search API: {e}")
        return None
    except ValueError as e:
        log.error(f"Unexpected non-JSON response from Search API: {e}")
        return None

    results = data.get("results", [])
    if not results:
        log.warning(f"No apps found matching '{app_name}' in the '{country}' store.")
        return None

    top = results[0]
    log.info(f"Top match: \"{top.get('trackName')}\" by {top.get('artistName')} (app id: {top.get('trackId')})")
    
    return top


def fetch_reviews_page(app_id: str, country: str, page: int) -> Optional[dict]:
    url = REVIEWS_URL_TEMPLATE.format(country=country, page=page, app_id=app_id)
    try:
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        log.warning(f"Request failed for page {page}: {e}")
        return None

    if response.status_code != 200:
        return None

    try:
        return response.json()
    except ValueError:
        return None


def normalize_review(entry: dict, app_meta: dict, country: str) -> dict:
    def label(field_name):
        field = entry.get(field_name)
        if isinstance(field, dict):
            return field.get("label")
        return None

    # Conform to standard schema
    rating = label("im:rating")
    try:
        rating = float(rating)
    except (TypeError, ValueError):
        rating = None

    author_name = None
    author = entry.get("author")
    if isinstance(author, dict):
        author_name = author.get("name", {}).get("label")

    return {
        "review_id": label("id"),
        "title": label("title"),
        "body": label("content"),
        "rating": rating,
        "date": label("updated"),
        "author": author_name,
        "platform": "appstore",
        # Extra context
        "app_id": app_meta.get("trackId"),
        "app_name": app_meta.get("trackName"),
        "app_version": label("im:version"),
        "country": country
    }


def fetch_all_reviews(app_meta: dict, limit: int, emit_fn, country: str = "us") -> int:
    app_id = str(app_meta.get("trackId"))
    log.info(f"Fetching reviews for app id {app_id} (country={country})...")
    
    count = 0
    max_pages_needed = min(MAX_PAGES, -(-limit // REVIEWS_PER_PAGE))

    for page in range(1, max_pages_needed + 1):
        data = fetch_reviews_page(app_id, country, page)
        if data is None:
            log.info(f"Page {page} empty or failed — assumed end of available reviews.")
            break

        entries = data.get("feed", {}).get("entry", [])
        if not entries:
            log.info(f"Page {page} yielded no entries — ending pagination.")
            break

        # Filter out the app bundle description which sits at index 0 on page 1 sometimes
        page_reviews = [e for e in entries if isinstance(e.get("im:rating"), dict)]
        
        for entry in page_reviews:
            if count >= limit:
                break
            review = normalize_review(entry, app_meta, country)
            emit_fn(review)
            count += 1
            
        if count >= limit:
            break
            
        time.sleep(POLITE_DELAY_SECS)

    return count


def main():
    parser = argparse.ArgumentParser(description="Scrape App Store reviews via Apple APIs.")
    parser.add_argument("keyword", help="App name/keyword to search for (e.g. 'Nike')")
    parser.add_argument("--limit", type=int, default=50, help="Max reviews to collect (Apple caps at ~500)")
    parser.add_argument("--country", default="us", help="App Store storefront/country code (default: us)")
    parser.add_argument("--output", default=None, help="Optional file path to save JSON output")
    args = parser.parse_args()

    log.info(f"Starting App Store review scrape for '{args.keyword}' (limit={args.limit})")

    app_meta = search_app(args.keyword, args.country)
    if not app_meta:
        log.error("Could not resolve an App Store app. Exiting.")
        sys.exit(1)

    if args.output:
        collected: List[Dict[str, Any]] = []

        def emit(review):
            collected.append(review)

        count = fetch_all_reviews(app_meta, args.limit, emit, country=args.country)

        result = {
            "query": args.keyword,
            "platform": "appstore",
            "count": count,
            "reviews": collected,
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        log.info(f"Wrote {count} reviews to {args.output}")
    else:
        def emit(review):
            print(json.dumps(review, ensure_ascii=False))
            sys.stdout.flush()

        count = fetch_all_reviews(app_meta, args.limit, emit, country=args.country)
        log.info(f"Done. {count} review(s) streamed.")


if __name__ == "__main__":
    main()