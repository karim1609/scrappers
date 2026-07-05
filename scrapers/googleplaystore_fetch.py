#!/usr/bin/env python3
"""Google Play Store scraper — enter a keyword, get app reviews as JSON.

Usage:
    python scrapers/googleplaystore_fetch.py Brand24
    python scrapers/googleplaystore_fetch.py Brand24 --limit 20
    python scrapers/googleplaystore_fetch.py Brand24 --output results.json

Docker:
    docker build -t scrapers .
    docker run scrapers scrapers/googleplaystore_fetch.py Brand24 --limit 20
"""

import argparse
import json
import sys

from google_play_scraper import search, reviews, Sort

def scrape_keyword(keyword: str, limit: int = 50) -> dict:
    print(f"Searching Google Play for '{keyword}'...", file=sys.stderr)
    try:
        apps_results = search(
            keyword,
            lang="en",
            country="us"
        )
    except Exception as e:
        print(f"Error searching apps: {e}", file=sys.stderr)
        return {"query": keyword, "platform": "google_play", "count": 0, "apps": []}
    
    if not apps_results:
        print("No apps found for this keyword.", file=sys.stderr)
        return {"query": keyword, "platform": "google_play", "count": 0, "apps": []}
    
    print(f"Found {len(apps_results)} apps matching keyword.", file=sys.stderr)
    
    final_apps = []
    total_reviews = 0
    
    for app in apps_results:
        app_id = app.get("appId")
        title = app.get("title")
        
        print(f"[App] {title} → {app_id}", file=sys.stderr)
        
        try:
            app_reviews, _ = reviews(
                app_id,
                lang='en',
                country='us',
                sort=Sort.NEWEST,
                count=limit
            )
        except Exception as e:
            print(f"Failed to fetch reviews for {app_id}: {e}", file=sys.stderr)
            app_reviews = []
        
        formatted_reviews = []
        for rv in app_reviews:
            dev_reply = None
            if rv.get("replyContent"):
                dev_reply = {
                    "text": rv.get("replyContent"),
                    "date": rv.get("repliedAt").isoformat() if rv.get("repliedAt") else None
                }
            
            formatted_reviews.append({
                "author": rv.get("userName"),
                "rating": rv.get("score"),
                "date": rv.get("at").isoformat() if rv.get("at") else None,
                "body": rv.get("content"),
                "developer_reply": dev_reply,
                "platform": "google_play",
                "app_title": title,
                "app_url": f"https://play.google.com/store/apps/details?id={app_id}",
            })
            
        final_apps.append({
            "app": {
                "app_id": app_id,
                "title": title,
                "description": app.get("description"),
                "developer": app.get("developer"),
                "icon": app.get("icon"),
                "score": app.get("score"),
                "installs": app.get("installs"),
                "url": f"https://play.google.com/store/apps/details?id={app_id}"
            },
            "reviews": formatted_reviews
        })
        
        total_reviews += len(formatted_reviews)

    return {
        "query": keyword,
        "platform": "google_play",
        "count": total_reviews,
        "apps": final_apps
    }


def main():
    parser = argparse.ArgumentParser(
        description="Scrape Google Play Store reviews for apps matching a keyword."
    )
    parser.add_argument("keyword", nargs="+", help="App name or keyword, e.g. Brand24")
    parser.add_argument("--limit", type=int, default=50, help="Max reviews (default: 50)")
    parser.add_argument("--output", help="Save JSON to file (default: print to stdout)")
    args = parser.parse_args()

    full_keyword = " ".join(args.keyword)
    results = scrape_keyword(full_keyword, args.limit)

    if results["count"] == 0:
        print("No reviews found.", file=sys.stderr)
        sys.exit(1)

    output = json.dumps(results, ensure_ascii=False, indent=2)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Saved {results['count']} total reviews → {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
