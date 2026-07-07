#!/usr/bin/env python3
"""TikTok scraper — enter a keyword, get TikTok videos and top comments as JSON.

Usage:
    python scrapers/tiktok_fetch.py "OpenAI"
    python scrapers/tiktok_fetch.py "OpenAI" --limit 20
    python scrapers/tiktok_fetch.py "OpenAI" --output results.json
"""

import argparse
import json
import logging
import os
import random
import sys
import time
import urllib.parse
from functools import wraps

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", stream=sys.stderr)
logger = logging.getLogger(__name__)

def retry_with_backoff(retries=3, backoff_in_seconds=2):
    """Exponential backoff decorator for robust scraping."""
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


def normalize_output(keyword: str, raw_data: dict, comments: list) -> dict:
    """Ensure the parsed data complies with the uniform SOLID JSON schema."""
    author = raw_data.get("author", "")
    author_clean = author.replace("@", "").strip()
    profile_url = f"https://www.tiktok.com/@{author_clean}" if author_clean else None
    
    # Internal hidden fields fallback to None
    return {
        "platform": "tiktok",
        "video_id": raw_data.get("video_id", None),
        "keyword": keyword,
        "description": raw_data.get("description", ""),
        "author": author_clean,
        "nickname": raw_data.get("nickname", ""),
        "profile_url": profile_url,
        "video_url": raw_data.get("video_url", None),
        "published_at": raw_data.get("published_at", None), # Hidden on UI usually
        "language": None, 
        "hashtags": raw_data.get("hashtags", []),
        "mentions": raw_data.get("mentions", []),
        "music": {
            "title": raw_data.get("music_title", None),
            "author": raw_data.get("music_author", None)
        },
        "stats": {
            "views": raw_data.get("views", 0),
            "likes": raw_data.get("likes", 0),
            "comments": raw_data.get("comments_count", 0),
            "shares": raw_data.get("shares", 0),
            "favorites": raw_data.get("favorites", 0)
        },
        "comments": comments,
        "thumbnail": raw_data.get("thumbnail", None),
        "verified": raw_data.get("verified", False),
        "raw": raw_data.get("raw", {})
    }


def parse_stat(text: str) -> int:
    """Parse string stats like '1.2M' or '500K' to pure integers."""
    if not text:
        return 0
    text = text.upper().strip()
    multiplier = 1
    if "K" in text:
        multiplier = 1000
    elif "M" in text:
        multiplier = 1000000
    elif "B" in text:
        multiplier = 1000000000
    num_str = "".join([c for c in text if c.isdigit() or c == "."])
    try:
        return int(float(num_str) * multiplier)
    except Exception:
        return 0


def collect_comments(page, limit: int) -> list:
    """Extract top comments from the video context inherently using DOM arrays."""
    comments = []
    
    try:
        # Give comments container time to load via DOM
        page.wait_for_selector('[data-e2e="comment-level-1"]', timeout=3000)
    except Exception:
        # Not found or timeout
        pass

    scroll_attempts = 0
    seen_ids = set()

    while len(comments) < limit and scroll_attempts < 10:
        comment_elements = page.locator('[data-e2e="comment-level-1"]').all()
        if not comment_elements:
            break
            
        initial_count = len(comments)
        
        for el in comment_elements:
            try:
                author_el = el.locator('[data-e2e="comment-username-1"]')
                author = author_el.inner_text().strip() if author_el.count() > 0 else ""
                
                text_el = el.locator('[data-e2e="comment-level-1-text"]')
                text = text_el.inner_text().strip() if text_el.count() > 0 else ""
                
                likes_el = el.locator('[data-e2e="comment-like-count"]')
                likes_text = likes_el.inner_text().strip() if likes_el.count() > 0 else "0"
                likes = parse_stat(likes_text)
                
                date_el = el.locator('[data-e2e="comment-time-1"]')
                date_text = date_el.inner_text().strip() if date_el.count() > 0 else None
                
                # We use a synthetic ID since DOM doesn't expose internal comment ID easily
                synthetic_id = f"{author}_{text[:10]}"
                if synthetic_id not in seen_ids:
                    seen_ids.add(synthetic_id)
                    comments.append({
                        "comment_id": synthetic_id,
                        "author": author.replace("@", ""),
                        "text": text,
                        "likes": likes,
                        "date": date_text,
                        "replies": 0 # Difficult to parse native nested DOM toggles safely
                    })
                    
                    if len(comments) >= limit:
                        break
            except Exception:
                continue

        if len(comments) >= limit:
            break
            
        # Try to scroll the comment section down by focusing on the last loaded comment
        try:
            if comment_elements:
                comment_elements[-1].scroll_into_view_if_needed()
                page.wait_for_timeout(1000)
        except Exception:
            pass
            
        if len(comments) == initial_count:
            scroll_attempts += 1
        else:
            scroll_attempts = 0

    return comments[:limit]


@retry_with_backoff(retries=2, backoff_in_seconds=2)
def parse_video(page, url: str, comment_limit: int) -> dict:
    """Load a specific video URL and rip metrics structurally via DOM selectors."""
    page.goto(url, wait_until="domcontentloaded", timeout=25000)
    page.wait_for_timeout(2000)

    # Detect CAPTCHA block independently in the video frame
    if page.locator("tiktok-captcha").count() > 0 or page.locator("div#captcha_container").count() > 0:
        logger.warning(f"CAPTCHA block hit on video URL: {url}")
        return None
    
    raw_data = {"video_url": url}
    
    # Extract video ID from URL
    try:
        raw_data["video_id"] = url.split("/video/")[1].split("?")[0]
    except Exception:
        raw_data["video_id"] = None
        
    # Extract Author Details
    try:
        author_loc = page.locator('[data-e2e="browse-username"]')
        raw_data["author"] = author_loc.inner_text().strip() if author_loc.count() > 0 else ""
        
        nickname_loc = page.locator('[data-e2e="browse-user-nickname"]')
        raw_data["nickname"] = nickname_loc.inner_text().strip() if nickname_loc.count() > 0 else ""
        
        verified_icon = page.locator('[data-e2e="verified-badge"]')
        raw_data["verified"] = verified_icon.count() > 0
    except Exception:
        pass

    # Extract Video Descriptions & Tags
    try:
        desc_container = page.locator('[data-e2e="browse-video-desc"]')
        desc_text = desc_container.inner_text().strip() if desc_container.count() > 0 else ""
        raw_data["description"] = desc_text
        
        hashtags = []
        mentions = []
        for word in desc_text.split():
            if word.startswith("#"):
                hashtags.append(word.strip())
            elif word.startswith("@"):
                mentions.append(word.strip())
                
        raw_data["hashtags"] = hashtags
        raw_data["mentions"] = mentions
    except Exception:
        pass

    # Extract Music Info
    try:
        music_loc = page.locator('[data-e2e="browse-music"]')
        music_text = music_loc.inner_text().strip() if music_loc.count() > 0 else ""
        if "-" in music_text:
            parts = music_text.split("-")
            raw_data["music_title"] = parts[0].strip()
            raw_data["music_author"] = parts[1].strip()
        else:
            raw_data["music_title"] = music_text
    except Exception:
        pass

    # Extract Stats
    try:
        likes_loc = page.locator('[data-e2e="like-count"]')
        raw_data["likes"] = parse_stat(likes_loc.inner_text().strip()) if likes_loc.count() > 0 else 0
        
        comments_loc = page.locator('[data-e2e="comment-count"]')
        raw_data["comments_count"] = parse_stat(comments_loc.inner_text().strip()) if comments_loc.count() > 0 else 0
        
        shares_loc = page.locator('[data-e2e="share-count"]')
        raw_data["shares"] = parse_stat(shares_loc.inner_text().strip()) if shares_loc.count() > 0 else 0
        
        favorites_loc = page.locator('[data-e2e="undefined-count"]') # Favorites often lack strong distinct tags on web UI
        raw_data["favorites"] = parse_stat(favorites_loc.inner_text().strip()) if favorites_loc.count() > 0 else 0
        
        # Views are rarely on the individual video page, usually on user profile or search grid, so we default to 0 natively
    except Exception:
        pass
        
    return raw_data


@retry_with_backoff(retries=2, backoff_in_seconds=2)
def collect_video_cards(page, keyword: str, limit: int) -> list:
    """Search TikTok by keyword and aggregate raw URL footprints organically."""
    search_url = f"https://www.tiktok.com/search/video?q={urllib.parse.quote(keyword)}"
    page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)

    # Detect anti-bot
    if page.locator("tiktok-captcha").count() > 0 or page.locator("div#captcha_container").count() > 0:
        logger.warning("TikTok anti-bot block (CAPTCHA) detected on Search Page.")
        return []
        
    if page.locator('text="Verify to continue"').count() > 0:
        logger.warning("TikTok 'Verify to continue' block detected.")
        return []

    collected_urls = []
    seen = set()
    scroll_attempts = 0

    while len(collected_urls) < limit and scroll_attempts < 15:
        # Native fallback selectors: trying e2e and a class-less fallback based on href patterns
        elements = page.locator('[data-e2e="search_video-item"]').all()
        if not elements:
            # Fallback 1
            elements = page.locator('div a[href*="/video/"]').all()
            
        initial_count = len(collected_urls)
        
        for el in elements:
            try:
                # Find the a tag inside the item with href directing to a video
                a_tags = el.locator('a[href*="/video/"]').all()
                if not a_tags and el.get_attribute("href") and "/video/" in el.get_attribute("href"):
                    a_tags = [el]
                    
                for a in a_tags:
                    url = a.get_attribute("href")
                    if url and "/video/" in url:
                        # Clean tracking params
                        url = url.split("?")[0]
                        if url not in seen:
                            seen.add(url)
                            collected_urls.append(url)
                            if len(collected_urls) >= limit:
                                break
            except Exception:
                pass
                
            if len(collected_urls) >= limit:
                break
                
        if len(collected_urls) >= limit:
            break
            
        # Scroll logic
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2500)
        
        if len(collected_urls) == initial_count:
            scroll_attempts += 1
        else:
            scroll_attempts = 0
            
    return collected_urls[:limit]


def search(keyword: str, limit: int = 50, comment_limit: int = 10) -> list[dict]:
    """
    Main orchestration routine coordinating DOM-based extractions.
    Uses Proxy if provided via TIKTOK_PROXY environment variable.
    """
    logger.info(f"Searching TikTok DOM for '{keyword}' [Video Limit: {limit}]")
    
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("Playwright not installed. Check requirements.txt")
        return []

    results = []
    
    with sync_playwright() as pw:
        proxy_string = os.environ.get("TIKTOK_PROXY")
        proxy = {"server": proxy_string} if proxy_string else None
        
        browser = pw.chromium.launch(
            headless=True, 
            proxy=proxy, 
            args=["--disable-blink-features=AutomationControlled"]
        )
        
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="en-US"
        )
        
        try:
            from stealth_sync import stealth_sync
            stealth_sync(context)
        except ImportError:
            pass 

        page = context.new_page()
        page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media", "font"] else route.continue_())

        try:
            urls = collect_video_cards(page, keyword, limit)
            if not urls:
                logger.warning("No URLs collected. Terminating flow.")
                return []
                
            logger.info(f"Scraped {len(urls)} video links. Processing details...")
            
            for url in urls:
                try:
                    raw_properties = parse_video(page, url, comment_limit)
                    if not raw_properties:
                        continue # Skip if CAPTCHA or broken layout
                        
                    comments = collect_comments(page, comment_limit)
                    normalized = normalize_output(keyword, raw_properties, comments)
                    results.append(normalized)
                except Exception as e:
                    logger.warning(f"Failed to process video {url}: {e}")
                    
        except Exception as e:
            logger.error(f"Critical error during DOM flow: {e}")
        finally:
            browser.close()
            
    return results


def main():
    parser = argparse.ArgumentParser(description="Scrape TikTok videos via DOM purely for a keyword.")
    parser.add_argument("keyword", nargs="+", help="Topic or keyword to search TikTok for.")
    parser.add_argument("--limit", type=int, default=50, help="Max videos to scrape (default: 50)")
    parser.add_argument("--comment-limit", type=int, default=20, help="Max comments per video (default: 20)")
    parser.add_argument("--output", help="Save JSON output to this file.")
    args = parser.parse_args()

    full_keyword = " ".join(args.keyword)
    
    try:
        results = search(keyword=full_keyword, limit=args.limit, comment_limit=args.comment_limit)
    except Exception as e:
        logger.error(f"Critical failure: {e}")
        sys.exit(1)

    if not results:
        # Graceful failure array
        output = json.dumps([], ensure_ascii=False, indent=2)
    else:
        logger.info(f"Successfully scraped {len(results)} videos.")
        output = json.dumps(results, ensure_ascii=False, indent=2)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        logger.info(f"Saved results -> {args.output}")
    else:
        print(output)

if __name__ == "__main__":
    main()
