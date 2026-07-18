#!/usr/bin/env python3
"""Fetch public Mastodon posts and return normalized JSON."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any


DEFAULT_INSTANCE = "https://mastodon.social"
USER_AGENT = "mastodon-fetch/1.0 (+https://github.com/local/mastodon-fetch)"


class _TextExtractor(HTMLParser):
    """Strip HTML tags and decode entities from Mastodon content."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self._parts.append(html.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self._parts.append(html.unescape(f"&#{name};"))

    def text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._parts)).strip()


def clean_html(raw_html: str | None) -> str:
    if not raw_html:
        return ""
    parser = _TextExtractor()
    parser.feed(raw_html)
    return parser.text()


def parse_instance(acct: str | None, account_url: str | None, post_url: str | None) -> str | None:
    if acct and "@" in acct:
        return acct.split("@", 1)[1]
    for candidate in (account_url, post_url):
        if candidate:
            host = urllib.parse.urlparse(candidate).netloc
            if host:
                return host
    return None


def normalize_media(media: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for attachment in media or []:
        items.append(
            {
                "id": attachment.get("id"),
                "type": attachment.get("type"),
                "url": attachment.get("url"),
                "preview_url": attachment.get("preview_url"),
                "remote_url": attachment.get("remote_url"),
                "description": attachment.get("description"),
                "blurhash": attachment.get("blurhash"),
                "meta": attachment.get("meta"),
            }
        )
    return items


def normalize_mentions(mentions: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        {
            "id": mention.get("id"),
            "username": mention.get("username"),
            "acct": mention.get("acct"),
            "url": mention.get("url"),
        }
        for mention in (mentions or [])
    ]


def normalize_hashtags(tags: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        {
            "name": tag.get("name"),
            "url": tag.get("url"),
        }
        for tag in (tags or [])
    ]


def normalize_status(status: dict[str, Any], source_instance: str) -> dict[str, Any]:
    account = status.get("account") or {}
    acct = account.get("acct")
    account_url = account.get("url")
    post_url = status.get("url")

    return {
        "post_id": status.get("id"),
        "content": clean_html(status.get("content")),
        "content_html": status.get("content"),
        "spoiler_text": status.get("spoiler_text") or "",
        "author_username": account.get("username"),
        "author_display_name": account.get("display_name"),
        "author_acct": acct,
        "author_account_url": account_url,
        "post_url": post_url,
        "created_at": status.get("created_at"),
        "edited_at": status.get("edited_at"),
        "language": status.get("language"),
        "hashtags": normalize_hashtags(status.get("tags")),
        "mentions": normalize_mentions(status.get("mentions")),
        "replies_count": status.get("replies_count"),
        "reblogs_count": status.get("reblogs_count"),
        "favourites_count": status.get("favourites_count"),
        "quotes_count": status.get("quotes_count"),
        "media_attachments": normalize_media(status.get("media_attachments")),
        "visibility": status.get("visibility"),
        "sensitive": status.get("sensitive"),
        "instance": parse_instance(acct, account_url, post_url),
        "source_instance": source_instance,
        "uri": status.get("uri"),
        "in_reply_to_id": status.get("in_reply_to_id"),
        "in_reply_to_account_id": status.get("in_reply_to_account_id"),
        "reblog": bool(status.get("reblog")),
        "card": status.get("card"),
        "poll": status.get("poll"),
    }


def api_get(base_url: str, path: str, params: dict[str, Any] | None = None) -> Any:
    query = urllib.parse.urlencode(params or {})
    url = f"{base_url.rstrip('/')}{path}"
    if query:
        url = f"{url}?{query}"

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_by_hashtag(instance: str, tag: str, limit: int) -> list[dict[str, Any]]:
    tag = tag.lstrip("#")
    all_statuses = []
    max_id = None
    
    while len(all_statuses) < limit:
        req_limit = min(40, limit - len(all_statuses))
        params = {"limit": req_limit}
        if max_id:
            params["max_id"] = max_id
            
        statuses = api_get(instance, f"/api/v1/timelines/tag/{urllib.parse.quote(tag)}", params)
        if not isinstance(statuses, list) or not statuses:
            break
            
        all_statuses.extend(statuses)
        max_id = statuses[-1]["id"]
        
        if len(statuses) < req_limit:
            break # Reached the end of available posts
            
    return [normalize_status(status, instance) for status in all_statuses[:limit]]


def fetch_by_search(instance: str, query: str, limit: int) -> list[dict[str, Any]]:
    # Note: /api/v2/search requires authentication for paginated offsets. We limit to 40.
    req_limit = min(limit, 40)
    if limit > 40:
        import sys
        print(f"[Warning] Full-text search pagination is restricted without auth. Capping to {req_limit} posts.", file=sys.stderr)
        
    payload = api_get(
        instance,
        "/api/v2/search",
        {
            "q": query,
            "type": "statuses",
            "limit": req_limit,
            "resolve": "false",
        },
    )
    statuses = payload.get("statuses") if isinstance(payload, dict) else []
    if not isinstance(statuses, list):
        return []
    return [normalize_status(status, instance) for status in statuses[:req_limit]]


def fetch_posts(instance: str, keyword: str, limit: int, mode: str) -> dict[str, Any]:
    keyword = keyword.strip()
    if not keyword:
        raise ValueError("Keyword must not be empty.")

    posts: list[dict[str, Any]] = []
    strategy = mode

    if mode in {"auto", "hashtag"}:
        try:
            posts = fetch_by_hashtag(instance, keyword, limit)
        except urllib.error.HTTPError as exc:
            if mode == "hashtag":
                raise RuntimeError(f"Hashtag lookup failed: HTTP {exc.code}") from exc

    if not posts and mode in {"auto", "search"}:
        strategy = "search"
        posts = fetch_by_search(instance, keyword, limit)

    return {
        "query": keyword,
        "instance": instance,
        "strategy": strategy,
        "count": len(posts),
        "posts": posts,
    }


import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scrapers.base import BaseScraper, ScraperConfig, ScraperResult


class MastodonScraper(BaseScraper):
    platform = "mastodon"
    items_key = "posts"

    def validate_config(self, config: ScraperConfig) -> None:
        if not config.keyword.strip():
            raise ValueError("keyword is required")

    def scrape(self, config: ScraperConfig) -> ScraperResult:
        self.validate_config(config)
        instance = config.extra.get("instance", DEFAULT_INSTANCE)
        mode = config.extra.get("mode", "auto")
        result = fetch_posts(instance, config.keyword, config.limit, mode)
        items = [self.normalize_item(post) for post in result.get("posts", [])]
        return ScraperResult(
            query=config.keyword,
            platform=self.platform,
            count=len(items),
            items=items,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch Mastodon posts and print JSON.")
    parser.add_argument("keyword", help="Hashtag or search keyword, e.g. python or #python")
    parser.add_argument(
        "--instance",
        default=DEFAULT_INSTANCE,
        help=f"Mastodon instance base URL (default: {DEFAULT_INSTANCE})",
    )
    parser.add_argument("--limit", type=int, default=20, help="Maximum number of posts to return")
    parser.add_argument(
        "--mode",
        choices=["auto", "hashtag", "search"],
        default="auto",
        help="Lookup mode: hashtag timeline, full-text search, or auto",
    )
    parser.add_argument(
        "--output",
        help="Optional path to write JSON output. Prints to stdout when omitted.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    args = build_parser().parse_args(argv)

    try:
        result = fetch_posts(args.instance, args.keyword, args.limit, args.mode)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(json.dumps({"error": str(exc), "query": args.keyword, "instance": args.instance}), file=sys.stderr)
        return 1

    payload = json.dumps(result, ensure_ascii=False, indent=2)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload)
    else:
        print(payload)
        print("\n\n")
        print(len(result["posts"]))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
