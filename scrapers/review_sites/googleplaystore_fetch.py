#!/usr/bin/env python3
"""Google Play Store scraper — search for an app by name and fetch reviews.

Usage:
    python scrapers/googleplaystore_fetch.py Brand24
    python scrapers/googleplaystore_fetch.py "Google Maps" --limit 20
    python scrapers/googleplaystore_fetch.py Brand24 --output results.json
"""

import argparse
import json
import sys
from rapidfuzz import fuzz

from google_play_scraper import search, reviews, Sort

MIN_FUZZY_SCORE = 85


def _score(app: dict) -> float:
    """Safely read an app's rating score, treating missing/None as 0."""
    return app.get("score") or 0


def find_best_match(keyword: str, apps_results: list):
    """
    Return the app result that best matches `keyword`.

    Priority:
    1. Exact title match (case-insensitive)
    2. Title starts with keyword (highest-rated among these)
    3. Best fuzzy title match, but only if it clears MIN_FUZZY_SCORE
       (otherwise we'd rather return nothing than a wrong app)
    """
    keyword_norm = keyword.strip().lower()

    # 1. Exact title match
    for app in apps_results:
        if app.get("title", "").strip().lower() == keyword_norm:
            print("✓ Exact app name found.", file=sys.stderr)
            return app

    # 2. Starts with keyword
    starts_matches = [
        app
        for app in apps_results
        if app.get("title", "").strip().lower().startswith(keyword_norm)
    ]
    if starts_matches:
        starts_matches.sort(key=_score, reverse=True)
        print("✓ Using app whose title starts with the keyword.", file=sys.stderr)
        return starts_matches[0]

    # 3. Fuzzy match, with a floor so unrelated apps get rejected
    best_app = None
    best_fuzzy_score = 0

    for app in apps_results:
        title = app.get("title", "").strip().lower()
        score = fuzz.ratio(keyword_norm, title)

        if score > best_fuzzy_score:
            best_fuzzy_score = score
            best_app = app

    print(f"Best fuzzy match score: {best_fuzzy_score}", file=sys.stderr)

    if best_app is None or best_fuzzy_score < MIN_FUZZY_SCORE:
        print("⚠ No sufficiently close match found.", file=sys.stderr)
        return None

    print("✓ Using best fuzzy title match.", file=sys.stderr)
    return best_app


def fallback_search(keyword: str):
    """Fallback search manually scraping the Play Store search page if the python package fails."""
    print("Falling back to direct Google Play search...", file=sys.stderr)
    try:
        import requests
        import urllib.parse
        from bs4 import BeautifulSoup
        from google_play_scraper import app as get_app_details

        res = requests.get(
            f"https://play.google.com/store/search?q={urllib.parse.quote(keyword)}&c=apps",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        soup = BeautifulSoup(res.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/store/apps/details?id=" in href:
                app_id = href.split("?id=")[1].split("&")[0]
                print(f"✓ Found fallback app ID: {app_id}", file=sys.stderr)
                fallback_app = get_app_details(app_id, lang="en", country="us")
                return fallback_app
    except Exception as e:
        print(f"Fallback search failed: {e}", file=sys.stderr)
    return None


def search_apps(keyword: str):
    """Search Google Play and return the best matching app, or None."""
    print(f"Searching Google Play for '{keyword}'...", file=sys.stderr)

    apps = []
    try:
        apps = search(
            keyword,
            lang="en",
            country="us",
        )
    except Exception as e:
        print(f"Search failed: {e}", file=sys.stderr)

    # Filter out invalid entries (e.g., ads or parse failures from google-play-scraper)
    valid_apps = [app for app in apps if app.get("appId")]

    best_match = None
    if valid_apps:
        best_match = find_best_match(keyword, valid_apps)

    if best_match is None:
        best_match = fallback_search(keyword)

    return best_match


def scrape_keyword(keyword: str, limit: int = 50) -> dict:
    app = search_apps(keyword)

    if app is None:
        print("No app found.", file=sys.stderr)
        return {
            "query": keyword,
            "platform": "google_play",
            "count": 0,
            "apps": [],
        }

    app_id = app["appId"]
    title = app["title"]

    print(f"Selected app: {title}", file=sys.stderr)
    print(f"App ID: {app_id}", file=sys.stderr)

    try:
        app_reviews, _ = reviews(
            app_id,
            lang="en",
            country="us",
            sort=Sort.NEWEST,
            count=limit,
        )
    except Exception as e:
        print(f"Failed to fetch reviews: {e}", file=sys.stderr)
        app_reviews = []

    formatted_reviews = []

    for rv in app_reviews:
        developer_reply = None

        if rv.get("replyContent"):
            developer_reply = {
                "text": rv["replyContent"],
                "date": rv["repliedAt"].isoformat()
                if rv.get("repliedAt")
                else None,
            }

        formatted_reviews.append(
            {
                "author": rv.get("userName"),
                "rating": rv.get("score"),
                "date": rv.get("at").isoformat()
                if rv.get("at")
                else None,
                "body": rv.get("content"),
                "developer_reply": developer_reply,
                "platform": "google_play",
                "app_title": title,
                "app_url": f"https://play.google.com/store/apps/details?id={app_id}",
            }
        )

    return {
        "query": keyword,
        "platform": "google_play",
        "count": len(formatted_reviews),
        "apps": [
            {
                "app": {
                    "app_id": app_id,
                    "title": title,
                    "description": app.get("description"),
                    "developer": app.get("developer"),
                    "icon": app.get("icon"),
                    "score": app.get("score"),
                    "installs": app.get("installs"),
                    "url": f"https://play.google.com/store/apps/details?id={app_id}",
                },
                "reviews": formatted_reviews,
            }
        ],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Google Play reviews for the best matching app."
    )

    parser.add_argument(
        "keyword",
        nargs="+",
        help="App name (e.g. Brand24 or Google Maps)",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum number of reviews (default: 50)",
    )

    parser.add_argument(
        "--output",
        help="Save JSON output to a file",
    )

    args = parser.parse_args()

    keyword = " ".join(args.keyword)

    results = scrape_keyword(keyword, args.limit)

    if results["count"] == 0:
        print("No reviews found.", file=sys.stderr)
        sys.exit(1)

    output = json.dumps(results, ensure_ascii=False, indent=2)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)

        print(f"Saved {results['count']} reviews to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()