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
import os
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

# Key loaded from environment
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")


def fetch_video_comments(youtube, video_id: str, max_results: int = 10) -> list:
    """Fetch top-level comments for a video."""
    comments = []
    if max_results <= 0:
        return comments
        
    try:
        request = youtube.commentThreads().list(
            part="snippet",
            videoId=video_id,
            maxResults=max_results,
            textFormat="plainText"
        )
        response = request.execute()
        
        for item in response.get("items", []):
            topLevelComment = item["snippet"]["topLevelComment"]["snippet"]
            comments.append({
                "author": topLevelComment.get("authorDisplayName", ""),
                "text": topLevelComment.get("textDisplay", ""),
                "like_count": topLevelComment.get("likeCount", 0),
                "published_at": topLevelComment.get("publishedAt", "")
            })
    except HttpError as e:
        # Silently handle videos with comments disabled
        if e.resp.status in (403, 404):
            pass
        else:
            log.warning(f"HTTP error fetching comments for {video_id}: {e}")
    except Exception as e:
        log.warning(f"Error fetching comments for {video_id}: {e}")
        
    return comments


def scrape(keyword: str, limit: int, emit_fn, comment_limit: int = 10) -> int:
    """
    Fonction principale pour l'extraction de données de YouTube via son API officielle v3.
    Prend un mot-clé, une limite et une fonction d'émission (pour envoyer les données au fur et à mesure).
    """
    count = 0
    try:
        # Vérification si la clé API a bien été fournie
        if not YOUTUBE_API_KEY:
            raise RuntimeError("YOUTUBE_API_KEY is not set.")
        # Build the youtube client
        youtube = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)
        
        next_page_token = None
        
        # Max results per request is 50 for search
        while count < limit:
            batch_size = min(50, limit - count)
            log.info(f"Fetching batch of size {batch_size} for keyword '{keyword}'...")
            
            # Étape 1 : Récupérer d'abord les IDs et les 'snippets' (titre, description) des vidéos
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
            
            # Étape 2 : Un second appel API est nécessaire pour avoir le nombre de vues (statistiques)
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
                    "platform": "youtube",
                    "comments": fetch_video_comments(youtube, video_id, comment_limit)
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


_scrape_videos = scrape


import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scrapers.base import BaseScraper, ScraperConfig, ScraperResult


class YouTubeScraper(BaseScraper):
    """
    Implémentation compatible avec la classe mère BaseScraper.
    Configure le nom de la 'platform' ("youtube") et valide les pré-requis.
    """
    platform = "youtube"
    items_key = "videos"

    def validate_config(self, config: ScraperConfig) -> None:
        if not config.keyword.strip():
            raise ValueError("keyword is required")

    def scrape(self, config: ScraperConfig) -> ScraperResult:
        self.validate_config(config)
        items: List[Dict[str, Any]] = []

        def emit(video: dict) -> None:
            items.append(self.normalize_item(video))

        comment_limit = config.extra.get("comment_limit", 10)
        count = _scrape_videos(config.keyword, config.limit, emit, comment_limit)
        return ScraperResult(
            query=config.keyword,
            platform=self.platform,
            count=count,
            items=items,
        )


def main():
    parser = argparse.ArgumentParser(description="Scrape YouTube search results via API.")
    parser.add_argument("keyword", help="Brand keyword to search on YouTube (e.g. 'Apple')")
    parser.add_argument("--limit", type=int, default=50, help="Max videos to extract (default: 50)")
    parser.add_argument("--output", default=None, help="Optional file path to save JSON output")
    args = parser.parse_args()

    log.info("Starting YouTube API scrape for %r (limit=%d)", args.keyword, args.limit)

    config = ScraperConfig(
        keyword=args.keyword,
        limit=args.limit,
        output_path=args.output,
    )
    scraper = YouTubeScraper()
    
    try:
        result = scraper.scrape(config)
    except Exception as e:
        log.error("Scraping failed: %s", e)
        sys.exit(1)

    if args.output:
        output_data = scraper.to_json(result)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
        log.info("Wrote %d videos to %s", result.count, args.output)
    else:
        for video in result.items:
            print(json.dumps(video, ensure_ascii=False))
            sys.stdout.flush()
        log.info("Done. %d video(s) retrieved.", result.count)


if __name__ == "__main__":
    main()
