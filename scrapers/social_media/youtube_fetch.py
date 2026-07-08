#!/usr/bin/env python3
"""
youtube_fetch.py

Scrape YouTube search results for a given brand keyword using the official YouTube Data API.

Usage:
    python youtube_fetch.py "Nike" --limit 50
    python youtube_fetch.py "Apple" --limit 100 --output youtube_videos.json

Strategy:
    1. Authenticate with YouTube Data API v3.
    2. Search for the keyword to retrieve basic video metadata and video IDs.
    3. Perform a secondary API call to fetch view counts.
    4. Provide output as real-time JSONL stream or save to a file.
"""

import argparse
import json
import sys
import logging
from typing import List, Dict, Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("youtube_fetch")

# Official API Key provided by the user
YOUTUBE_API_KEY = "AIzaSyDX8alMAxYALYk9TWR2gZ6zRcczH5AJV6s"


def scrape(keyword: str, limit: int, emit_fn) -> int:
    try:
        # Build the youtube client
        youtube = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)
        
        count = 0
        next_page_token = None
        
        # Max results per request is 50 for search
        while count < limit:
            batch_size = min(50, limit - count)
            log.info(f"Fetching batch of size {batch_size} for keyword '{keyword}'...")
            
            search_request = youtube.search().list(
                q=keyword,
                part="id,snippet",
                type="video",
                maxResults=batch_size,
                pageToken=next_page_token
            )
            
            search_response = search_request.execute()
            items = search_response.get("items", [])
            
            if not items:
                log.info("No more items found in API response.")
                break
                
            video_ids = [item["id"]["videoId"] for item in items]
            
            # Fetch statistics (like viewCount) for the batch of videos
            stats_request = youtube.videos().list(
                part="statistics",
                id=",".join(video_ids)
            )
            stats_response = stats_request.execute()
            
            # Map video_id to statistics dictionary
            stats_items = {item["id"]: item["statistics"] for item in stats_response.get("items", [])}
            
            for item in items:
                video_id = item["id"]["videoId"]
                snippet = item["snippet"]
                stats = stats_items.get(video_id, {})
                
                video_data = {
                    "video_id": video_id,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "title": snippet.get("title", ""),
                    "channel": snippet.get("channelTitle", ""),
                    "views": stats.get("viewCount", "0"),
                    "published": snippet.get("publishedAt", ""),
                    "description": snippet.get("description", ""),
                    "platform": "youtube"
                }
                
                emit_fn(video_data)
                count += 1
                if count >= limit:
                    break
                    
            next_page_token = search_response.get("nextPageToken")
            if not next_page_token:
                log.info("No nextPageToken found, stopping pagination.")
                break
                
        return count

    except HttpError as e:
        log.error("An HTTP error %d occurred:\\n%s", e.resp.status, e.content)
        return count
    except Exception as e:
        log.error("An unexpected error occurred: %s", e)
        return count


def main():
    parser = argparse.ArgumentParser(description="Scrape YouTube search results via API.")
    parser.add_argument("keyword", help="Brand keyword to search on YouTube (e.g. 'Apple')")
    parser.add_argument("--limit", type=int, default=50, help="Max videos to extract (default: 50)")
    parser.add_argument("--output", default=None, help="Optional file path to save JSON output")
    args = parser.parse_args()

    log.info("Starting YouTube API scrape for %r (limit=%d)", args.keyword, args.limit)

    if args.output:
        collected: List[Dict[str, Any]] = []

        def emit(video):
            collected.append(video)
            log.info("Collected video %d/%d", len(collected), args.limit)

        count = scrape(args.keyword, args.limit, emit)

        result = {
            "query": args.keyword,
            "platform": "youtube",
            "count": count,
            "videos": collected,
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        log.info("Wrote %d videos to %s", count, args.output)
    else:
        def emit(video):
            print(json.dumps(video, ensure_ascii=False))
            sys.stdout.flush()

        count = scrape(args.keyword, args.limit, emit)
        log.info("Done. %d video(s) streamed.", count)


if __name__ == "__main__":
    main()
