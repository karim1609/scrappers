#!/usr/bin/env python3
"""
bluesky_fetch.py

Search Bluesky posts via the official AT Protocol API (authenticated).

Usage:
    python bluesky_fetch.py "python" --limit 50
    python bluesky_fetch.py "openai" --limit 20 --output posts.json

Credentials (env vars override defaults):
    BSKY_HANDLE       Bluesky handle, e.g. anouardev.bsky.social
    BSKY_APP_PASSWORD App password or account password

Streaming behavior:
    - No --output: each post is printed as one JSON line (JSONL) to stdout,
      flushed immediately. All logs go to stderr.
    - With --output: a single JSON object {query, platform, count, posts}
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("bluesky_fetch")

SEARCH_PAGE_SIZE = 25
DEFAULT_HANDLE = "anouardev.bsky.social"
DEFAULT_APP_PASSWORD = "Anaanoiar12"


def get_credentials() -> tuple[str, str]:
    handle = os.environ.get("BSKY_HANDLE", DEFAULT_HANDLE).strip()
    password = os.environ.get("BSKY_APP_PASSWORD", DEFAULT_APP_PASSWORD).strip()
    if not handle or not password:
        raise RuntimeError(
            "Bluesky credentials are required. Set BSKY_HANDLE and BSKY_APP_PASSWORD."
        )
    return handle, password


def create_client():
    from atproto import Client

    handle, password = get_credentials()
    client = Client()
    profile = client.login(handle, password)
    log.info("Logged in as %s (%s)", profile.handle, profile.display_name or profile.handle)
    return client


def post_web_url(post: Any) -> Optional[str]:
    handle = getattr(getattr(post, "author", None), "handle", None)
    uri = getattr(post, "uri", "") or ""
    if not handle or not uri:
        return None
    rkey = uri.rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


def normalize_post(post: Any, keyword: str) -> Dict[str, Any]:
    record = getattr(post, "record", None)
    author = getattr(post, "author", None)
    text = getattr(record, "text", None) if record else None
    created_at = getattr(record, "created_at", None) if record else None

    return {
        "post_id": getattr(post, "uri", None),
        "cid": getattr(post, "cid", None),
        "title": None,
        "body": text,
        "author": getattr(author, "handle", None),
        "author_display_name": getattr(author, "display_name", None),
        "author_did": getattr(author, "did", None),
        "author_avatar": getattr(author, "avatar", None),
        "created_at": created_at,
        "indexed_at": getattr(post, "indexed_at", None),
        "url": post_web_url(post),
        "likes": getattr(post, "like_count", None),
        "reposts": getattr(post, "repost_count", None),
        "replies": getattr(post, "reply_count", None),
        "keyword": keyword,
        "platform": "bluesky",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def search_posts(client, keyword: str, limit: int, emit_fn: Callable[[dict], None]) -> int:
    count = 0
    cursor: Optional[str] = None

    while count < limit:
        batch_size = min(SEARCH_PAGE_SIZE, limit - count)
        params: Dict[str, Any] = {"q": keyword, "limit": batch_size}
        if cursor:
            params["cursor"] = cursor

        log.info("Searching Bluesky for %r (batch=%d, collected=%d)", keyword, batch_size, count)
        try:
            response = client.app.bsky.feed.search_posts(params)
        except Exception as exc:
            if cursor and "403" in str(exc):
                log.warning("Pagination blocked by Bluesky API — stopping at %d post(s).", count)
                break
            raise

        posts = getattr(response, "posts", None) or []
        if not posts:
            log.info("No more posts returned by the API.")
            break

        for post in posts:
            if count >= limit:
                break
            emit_fn(normalize_post(post, keyword))
            count += 1

        cursor = getattr(response, "cursor", None)
        if not cursor or count >= limit:
            break

        time.sleep(0.5)

    return count


def scrape(keyword: str, limit: int, emit_fn: Callable[[dict], None]) -> int:
    client = create_client()
    return search_posts(client, keyword, limit, emit_fn)


def main() -> int:
    parser = argparse.ArgumentParser(description="Search Bluesky posts via the AT Protocol API.")
    parser.add_argument("keyword", help="Search keyword or hashtag, e.g. 'python' or '#ai'")
    parser.add_argument("--limit", type=int, default=50, help="Max posts to fetch (default: 50)")
    parser.add_argument("--output", default=None, help="Optional file path to save JSON output")
    args = parser.parse_args()

    log.info("Starting Bluesky search for %r (limit=%d)", args.keyword, args.limit)

    try:
        if args.output:
            collected: List[Dict[str, Any]] = []

            def emit(post: dict) -> None:
                collected.append(post)
                log.info("Collected post %d/%d", len(collected), args.limit)

            count = scrape(args.keyword, args.limit, emit)

            result = {
                "query": args.keyword,
                "platform": "bluesky",
                "count": count,
                "posts": collected,
            }
            with open(args.output, "w", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2)
            log.info("Wrote %d posts to %s", count, args.output)
        else:
            def emit(post: dict) -> None:
                print(json.dumps(post, ensure_ascii=False))
                sys.stdout.flush()

            count = scrape(args.keyword, args.limit, emit)
            log.info("Done. %d post(s) streamed.", count)
    except Exception as exc:
        log.error("Bluesky scrape failed: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
