"""
app_store_reviews_explorer.py
================================
EXPLORATORY SCRIPT — data-source evaluation, not a production connector.

Purpose
-------
Discover what public metadata Apple's App Store exposes for an app and its
customer reviews, using Apple's OFFICIAL, PUBLIC, documented APIs — no
scraping of the App Store website involved at all:

  1. iTunes Search API (find an app by name):
     https://itunes.apple.com/search?term=<name>&entity=software

  2. iTunes Customer Reviews RSS feed (official, per-app, paginated):
     https://itunes.apple.com/{country}/rss/customerreviews/page={n}/id={app_id}/sortby=mostrecent/json

Why APIs instead of scraping the app's App Store page
-------------------------------------------------------
Both of the above are long-standing, publicly documented Apple endpoints
meant for exactly this kind of consumption. There is no reason to scrape
the App Store website (apps.apple.com) — with the usual scraping risks
(ToS, anti-bot, brittle markup) — when Apple already serves the same
review data as structured JSON.

Known limitations
-------------------
- The reviews feed returns at most ~10 pages (Apple's hard limit), each
  with up to 50 reviews, so a maximum of roughly 500 of the MOST RECENT
  reviews per app/country. There is no way to get older reviews or a
  full historical archive through this endpoint.
- Reviews are per storefront/country (e.g. "us", "gb", "fr") — run the
  script once per country if you need multiple markets.
- Apple doesn't publish a strict rate limit for this endpoint, but it's
  still good practice to pace requests — this script pauses briefly
  between pages.

Usage
-----
    pip install requests
    python app_store_reviews_explorer.py --app-name "Nike" --max-reviews 200
    python app_store_reviews_explorer.py --app-id 1234567890 --country gb
"""

import argparse
import csv
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

SEARCH_URL = "https://itunes.apple.com/search"
# NOTE: Apple's RSS-feed-generator URL is picky about segment order/casing
# (it's path-routed, not a real query string, so it behaves more like a
# fixed pattern than case-insensitive query params). This ordering
# (id -> sortBy -> page) is the one most consistently documented/confirmed
# to work; the previous (page -> id -> sortby) ordering this script shipped
# with initially was likely the cause of empty responses for some apps.
REVIEWS_URL_TEMPLATE = (
    "https://itunes.apple.com/{country}/rss/customerreviews/id={app_id}/sortBy=mostRecent/page={page}/json"
)
MAX_PAGES = 10          # Apple's hard limit for this endpoint
REVIEWS_PER_PAGE = 50   # Apple's fixed page size
REQUEST_TIMEOUT = 15
POLITE_DELAY_SECS = 1.0
OUTPUT_CSV = "app_store_reviews.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

CSV_FIELDS = [
    "app_id",
    "app_name",
    "app_developer",
    "app_average_rating",
    "app_url",
    "review_id",
    "review_title",
    "review_text",
    "rating",
    "author_name",
    "app_version_reviewed",
    "helpful_votes",
    "total_votes",
    "review_date",
    "country",
    "scraped_at",
]


# --------------------------------------------------------------------------
# Step 1: resolve an app name to an app ID via the iTunes Search API
# --------------------------------------------------------------------------

def search_app(app_name: str, country: str) -> Optional[dict]:
    """
    Query Apple's official Search API for an app by name and return the
    top match's metadata dict, or None if nothing was found.
    """
    print(f"[1/3] Searching the App Store for '{app_name}' (country={country})...")
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
        print(f"  ERROR: could not reach the iTunes Search API: {e}", file=sys.stderr)
        return None
    except ValueError as e:
        print(f"  ERROR: unexpected (non-JSON) response from the Search API: {e}", file=sys.stderr)
        return None

    results = data.get("results", [])
    if not results:
        print(f"  No apps found matching '{app_name}' in the '{country}' store.")
        return None

    top = results[0]
    print(f"  Top match: \"{top.get('trackName')}\" by {top.get('artistName')} "
          f"(app id: {top.get('trackId')})")
    if len(results) > 1:
        print(f"  ({len(results) - 1} other match(es) also found — pass --app-id "
              f"explicitly if this isn't the app you meant. Other matches:)")
        for alt in results[1:]:
            print(f"    - \"{alt.get('trackName')}\" by {alt.get('artistName')} "
                  f"(app id: {alt.get('trackId')})")
    return top


# --------------------------------------------------------------------------
# Step 2: fetch reviews via the official customer-reviews RSS/JSON feed
# --------------------------------------------------------------------------

def fetch_reviews_page(app_id: str, country: str, page: int, debug: bool = False) -> Optional[dict]:
    """Fetch a single page of the reviews feed. Returns the parsed JSON, or
    None if the page doesn't exist / an error occurred. When `debug` is
    True (automatically enabled for page 1 if it comes back empty), prints
    the raw HTTP status and a body snippet so a real failure can be
    diagnosed instead of silently assumed to mean 'no more reviews'."""
    url = REVIEWS_URL_TEMPLATE.format(country=country, page=page, app_id=app_id)
    try:
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"  WARNING: request failed for page {page}: {e}", file=sys.stderr)
        return None

    if response.status_code != 200:
        if debug:
            print(f"  [debug] GET {url}")
            print(f"  [debug] HTTP {response.status_code}")
            print(f"  [debug] Body (first 500 chars): {response.text[:500]!r}")
        return None

    try:
        data = response.json()
    except ValueError:
        if debug:
            print(f"  [debug] GET {url}")
            print(f"  [debug] HTTP 200 but response was not valid JSON.")
            print(f"  [debug] Body (first 500 chars): {response.text[:500]!r}")
        return None

    if debug and not data.get("feed", {}).get("entry"):
        print(f"  [debug] GET {url}")
        print(f"  [debug] HTTP 200, valid JSON, but no 'feed.entry' found.")
        print(f"  [debug] Top-level JSON keys: {list(data.keys())}")
        if "feed" in data:
            print(f"  [debug] 'feed' keys: {list(data['feed'].keys())}")

    return data


def normalize_review(entry: dict, app_meta: dict, country: str) -> dict:
    """Turn one feed 'entry' dict into our common record shape. Apple's
    Atom-derived JSON wraps every value as {"label": ...}, hence the
    repeated .get('label')."""
    def label(field_name):
        field = entry.get(field_name)
        if isinstance(field, dict):
            return field.get("label")
        return None

    return {
        "app_id": app_meta.get("trackId"),
        "app_name": app_meta.get("trackName"),
        "app_developer": app_meta.get("artistName"),
        "app_average_rating": app_meta.get("averageUserRating"),
        "app_url": app_meta.get("trackViewUrl"),
        "review_id": label("id"),
        "review_title": label("title"),
        "review_text": label("content"),
        "rating": label("im:rating"),
        "author_name": (entry.get("author") or {}).get("name", {}).get("label")
        if isinstance(entry.get("author"), dict) else None,
        "app_version_reviewed": label("im:version"),
        "helpful_votes": label("im:voteSum"),
        "total_votes": label("im:voteCount"),
        "review_date": label("updated"),
        "country": country,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_all_reviews(app_id: str, app_meta: dict, country: str, max_reviews: int) -> list:
    """Page through the official reviews feed until we hit max_reviews,
    run out of pages, or reach Apple's hard MAX_PAGES limit."""
    print(f"[2/3] Fetching reviews for app id {app_id} (country={country})...")
    reviews = []
    max_pages_needed = min(MAX_PAGES, -(-max_reviews // REVIEWS_PER_PAGE))  # ceil division

    for page in range(1, max_pages_needed + 1):
        data = fetch_reviews_page(app_id, country, page, debug=(page == 1))
        if data is None:
            print(f"  Page {page}: no data returned — likely no more reviews available "
                  f"(see [debug] lines above if this is page 1, which usually means a real problem).")
            break

        entries = data.get("feed", {}).get("entry", [])
        if not entries:
            print(f"  Page {page}: empty — reached the end of available reviews.")
            break

        # The very first entry on page 1 is the APP ITSELF (app-level
        # metadata), not a review — it lacks an 'im:rating' field, which
        # every real review entry has. We filter on that rather than
        # hardcoding "skip index 0", since it's more robust across apps.
        page_reviews = [e for e in entries if isinstance(e.get("im:rating"), dict)]
        print(f"  Page {page}/{max_pages_needed}: {len(page_reviews)} review(s) found.")

        for entry in page_reviews:
            if len(reviews) >= max_reviews:
                break
            review = normalize_review(entry, app_meta, country)
            reviews.append(review)
            print_review(review, len(reviews))

        if len(reviews) >= max_reviews:
            print(f"  Reached the requested {max_reviews} review(s) — stopping.")
            break

        time.sleep(POLITE_DELAY_SECS)

    return reviews


# --------------------------------------------------------------------------
# Console output + CSV persistence
# --------------------------------------------------------------------------

def print_review(review: dict, index: int) -> None:
    print("\n" + "-" * 70)
    print(f"Review #{index}")
    for field in CSV_FIELDS:
        value = review.get(field)
        if field == "review_text" and value:
            value = value[:200] + ("..." if len(value) > 200 else "")
        print(f"  {field:22s}: {value if value not in (None, '') else '(missing)'}")
    print("-" * 70)


def save_to_csv(reviews: list, filename: str) -> None:
    print(f"\n[3/3] Saving {len(reviews)} review(s) to '{filename}'...")
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for review in reviews:
            writer.writerow(review)
    print("Done.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Explore App Store app metadata + reviews via Apple's official public APIs."
    )
    parser.add_argument("--app-name", help="App name to search for (e.g. 'Nike').")
    parser.add_argument("--app-id", help="Exact numeric App Store id, if already known (skips search).")
    parser.add_argument("--country", default="us", help="App Store storefront/country code (default: us).")
    parser.add_argument("--max-reviews", type=int, default=200, help="Max reviews to collect (Apple caps at ~500).")
    parser.add_argument("--output", default=OUTPUT_CSV, help="Output CSV filename.")
    args = parser.parse_args()

    if not args.app_name and not args.app_id:
        print("Provide either --app-name or --app-id.", file=sys.stderr)
        sys.exit(1)

    if args.max_reviews > MAX_PAGES * REVIEWS_PER_PAGE:
        print(f"Note: Apple's reviews feed caps out at {MAX_PAGES * REVIEWS_PER_PAGE} reviews "
              f"regardless of --max-reviews; capping automatically.")

    # --- Resolve app metadata ---
    if args.app_id:
        # We still need basic app metadata (name, developer, url) for the
        # CSV even when the id is given directly, so do a lightweight
        # lookup by id via the same Search API's "lookup" mode.
        try:
            response = requests.get(
                "https://itunes.apple.com/lookup",
                params={"id": args.app_id, "country": args.country},
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            results = response.json().get("results", [])
            app_meta = results[0] if results else {"trackId": args.app_id}
        except Exception:
            app_meta = {"trackId": args.app_id}
    else:
        app_meta = search_app(args.app_name, args.country)
        if not app_meta:
            sys.exit(1)

    app_id = app_meta.get("trackId") or args.app_id

    # --- Fetch reviews ---
    reviews = fetch_all_reviews(app_id, app_meta, args.country, args.max_reviews)

    if not reviews:
        print("\nNo reviews could be collected. This can happen if the app has very "
              "few (or no) reviews in this storefront/country, or if the app id is wrong.")
        sys.exit(1)

    save_to_csv(reviews, args.output)
    print(f"\nSummary: {len(reviews)} review(s) collected for "
          f"\"{app_meta.get('trackName', app_id)}\" ({args.country}).")


if __name__ == "__main__":
    main()