#!/usr/bin/env python3
"""Reddit scraper — search for a keyword and get posts & comments primarily using the official Reddit API.

Usage:
    python scrapers/reddit_fetch.py OpenAI
    python scrapers/reddit_fetch.py OpenAI --limit 20
    python scrapers/reddit_fetch.py OpenAI --subreddit artificial --sort new --time month --limit 20
"""

import argparse
import json
import logging
import os
import random
import sys
import time
from functools import wraps

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", stream=sys.stderr)
logger = logging.getLogger(__name__)

def retry_with_backoff(retries=3, backoff_in_seconds=2):
    """
    Décorateur customisé pour gérer les limites de l'API (Rate Limiting).
    Augmente de façon exponentielle le temps d'attente à chaque échec pour éviter un blocage de l'API.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            x = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if x == retries:
                        logger.error(f"Failed after {retries} retries in {func.__name__}: {e}")
                        raise
                    sleep_time = (backoff_in_seconds * 2 ** x) + random.uniform(0, 1)
                    logger.warning(f"Error in {func.__name__}: {e}. Retrying in {sleep_time:.2f} seconds...")
                    time.sleep(sleep_time)
                    x += 1
        return wrapper
    return decorator

def normalize_post(post_data: dict, comments: list, keyword: str) -> dict:
    """Normalize raw post data into the requested unified JSON schema."""
    author = post_data.get("author", "")
    author_clean = author if author else ""
    profile_url = f"https://www.reddit.com/user/{author_clean}" if author_clean else ""

    return {
        "platform": "reddit",
        "post_id": post_data.get("post_id", ""),
        "keyword": keyword,
        "subreddit": post_data.get("subreddit", ""),
        "title": post_data.get("title", ""),
        "content": post_data.get("content", ""),
        "author": author_clean,
        "author_profile": profile_url,
        "post_url": post_data.get("post_url", ""),
        "permalink": post_data.get("permalink", ""),
        "created_at": post_data.get("created_at", ""),
        "language": post_data.get("language", ""),
        "score": post_data.get("score", 0),
        "upvote_ratio": post_data.get("upvote_ratio", 0.0),
        "comments_count": post_data.get("comments_count", 0),
        "awards": post_data.get("awards", []),
        "flair": post_data.get("flair", ""),
        "nsfw": post_data.get("nsfw", False),
        "locked": post_data.get("locked", False),
        "stickied": post_data.get("stickied", False),
        "spoiler": post_data.get("spoiler", False),
        "post_type": post_data.get("post_type", "text"),
        "media_url": post_data.get("media_url", ""),
        "thumbnail": post_data.get("thumbnail", ""),
        "external_url": post_data.get("external_url", ""),
        "edited": post_data.get("edited", False),
        "distinguished": post_data.get("distinguished", None),
        "comments": comments,
        "raw": post_data.get("raw", {})
    }

def normalize_comment(comment_data: dict) -> dict:
    """Normalize raw comment data."""
    return {
        "comment_id": comment_data.get("comment_id", ""),
        "author": comment_data.get("author", ""),
        "body": comment_data.get("body", ""),
        "score": comment_data.get("score", 0),
        "created_at": comment_data.get("created_at", ""),
        "parent_id": comment_data.get("parent_id", ""),
        "depth": comment_data.get("depth", 0),
        "edited": comment_data.get("edited", False),
        "distinguished": comment_data.get("distinguished", None)
    }

def has_credentials() -> bool:
    """Check if all required Reddit API credentials exist in the environment."""
    return True

def _get_praw_client():
    import praw
    client_id = os.environ.get("REDDIT_CLIENT_ID")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
    user_agent = os.environ.get("REDDIT_USER_AGENT", "aper_scraper_v1.0")
    
    if not client_id or not client_secret:
        raise RuntimeError("REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET must be set in environment.")
    
    return praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        user_agent=user_agent
    )

def collect_comments_api(submission, comment_limit: int) -> list:
    """
    Explore l'arbre des commentaires via PRAW. Garde en mémoire la 'profondeur' (depth) 
    pour savoir qui a répondu à qui. Récupère récursivement les réponses des commentaires.
    """
    if comment_limit <= 0:
        return []
        
    from praw.models import MoreComments
    
    logger.info(f"Retrieving comments for {submission.id}")
    # Force l'API à se dérouler complètement sans limiter le nombre "MoreComments" cachés
    submission.comments.replace_more(limit=0)
    comments = []
    
    def traverse_comments(comment_list, depth=0):
        # Fonction récursive naviguant de parent en enfant
        for comment in comment_list:
            if len(comments) >= comment_limit:
                break
            if isinstance(comment, MoreComments):
                continue
                
            author_name = comment.author.name if comment.author else "[deleted]"
            comments.append(normalize_comment({
                "comment_id": comment.id,
                "author": author_name,
                "body": comment.body,
                "score": getattr(comment, 'score', 0),
                "created_at": str(getattr(comment, 'created_utc', '')),
                "parent_id": getattr(comment, 'parent_id', ''),
                "depth": depth,
                "edited": bool(getattr(comment, 'edited', False)),
                "distinguished": getattr(comment, 'distinguished', None)
            }))
            
            # Recursively walk the nested replies
            if hasattr(comment, 'replies'):
                traverse_comments(comment.replies, depth + 1)
                
    traverse_comments(submission.comments, 0)
    return comments[:comment_limit]

@retry_with_backoff(retries=3, backoff_in_seconds=2)
def execute_praw_search(keyword: str, subreddit_name: str, sort: str, time_filter: str, limit: int, comment_limit: int) -> list:
    """
    Exécute la recherche de l'API Reddit via la librairie PRAW.
    Convertit les objets complexes "submission" fournis par le paquet PRAW
    en notre format standard JSON unifié.
    """
    logger.info("Using Reddit API")
    logger.info(f"Searching r/{subreddit_name}")
    
    reddit = _get_praw_client()
    results = []
    
    # Use standard search via the targeted subreddit mapping
    subreddit = reddit.subreddit(subreddit_name)
    submissions = subreddit.search(keyword, sort=sort, time_filter=time_filter, limit=limit)
    
    for submission in submissions:
        author_name = submission.author.name if submission.author else "[deleted]"
        
        raw_post = {
            "post_id": submission.id,
            "subreddit": submission.subreddit.display_name,
            "title": submission.title,
            "content": submission.selftext,
            "author": author_name,
            "post_url": getattr(submission, "url", ""),
            "permalink": getattr(submission, "permalink", ""),
            "created_at": str(getattr(submission, "created_utc", "")),
            "score": getattr(submission, "score", 0),
            "upvote_ratio": getattr(submission, "upvote_ratio", 0.0),
            "comments_count": getattr(submission, "num_comments", 0),
            "flair": getattr(submission, "link_flair_text", ""),
            "nsfw": getattr(submission, "over_18", False),
            "locked": getattr(submission, "locked", False),
            "stickied": getattr(submission, "stickied", False),
            "spoiler": getattr(submission, "spoiler", False),
            "post_type": "text" if getattr(submission, "is_self", False) else getattr(submission, "post_hint", "link"),
            "thumbnail": getattr(submission, "thumbnail", ""),
            "external_url": getattr(submission, "url", ""),
            "edited": bool(getattr(submission, "edited", False)),
            "distinguished": getattr(submission, "distinguished", None),
            "raw": {}
        }
        
        comments = collect_comments_api(submission, comment_limit)
        results.append(normalize_post(raw_post, comments, keyword))
        
    return results

def search(keyword: str, subreddit: str, sort: str, time_filter: str, limit: int, comment_limit: int) -> list:
    """Main orchestration point. Handles API credentials constraints directly."""
    
    if not has_credentials():
        logger.warning("Reddit API credentials missing")
        logger.warning("Reddit API credentials are required. No HTML scraping fallback is permitted.")
        return []
        
    logger.info("Reddit API credentials detected")
    
    try:
        results = execute_praw_search(keyword, subreddit, sort, time_filter, limit, comment_limit)
        logger.info(f"Retrieved {len(results)} posts")
        logger.info(f"Successfully normalized {len(results)} posts")
        return results
    except Exception as e:
        logger.error("Reddit API request failed")
        logger.error(f"Reason: {e}")
        return []


import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scrapers.base import BaseScraper, ScraperConfig, ScraperResult


class RedditScraper(BaseScraper):
    platform = "reddit"
    items_key = "posts"

    def validate_config(self, config: ScraperConfig) -> None:
        if not config.keyword.strip():
            raise ValueError("keyword is required")

    def scrape(self, config: ScraperConfig) -> ScraperResult:
        self.validate_config(config)
        posts = search(
            config.keyword,
            config.extra.get("subreddit", "all"),
            config.extra.get("sort", "relevance"),
            config.extra.get("time_filter", "all"),
            config.limit,
            config.extra.get("comment_limit", 20),
        )
        items = [self.normalize_item(post) for post in posts]
        return ScraperResult(
            query=config.keyword,
            platform=self.platform,
            count=len(items),
            items=items,
        )


def main():
    parser = argparse.ArgumentParser(description="Scrape Reddit strictly using the PRAW API.")
    parser.add_argument("keyword", nargs="+", help="Topic or keyword to search Reddit for.")
    parser.add_argument("--limit", type=int, default=50, help="Max posts to scrape (default: 50)")
    parser.add_argument("--comment-limit", type=int, default=20, help="Max comments per post (default: 20)")
    parser.add_argument("--output", help="Save JSON output to this file.")
    
    # Bonus Arguments
    parser.add_argument("--sort", choices=["relevance", "new", "top", "hot"], default="relevance", help="Sort order for search (default: relevance)")
    parser.add_argument("--time", choices=["all", "year", "month", "week", "day", "hour"], default="all", help="Time filter for search (default: all)")
    parser.add_argument("--subreddit", default="all", help="Target subreddit (default: all)")
    
    args = parser.parse_args()
    full_keyword = " ".join(args.keyword)
    
    try:
        results = search(
            keyword=full_keyword,
            subreddit=args.subreddit,
            sort=args.sort,
            time_filter=args.time,
            limit=args.limit,
            comment_limit=args.comment_limit
        )
    except Exception as e:
        logger.error(f"Critical failure: {e}")
        results = []

    # Final Output Rendering
    if not results:
        output = json.dumps([], ensure_ascii=False, indent=2)
    else:
        output = json.dumps(results, ensure_ascii=False, indent=2)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        logger.info(f"Saved results -> {args.output}")
    else:
        print(output)

if __name__ == "__main__":
    main()
