#!/usr/bin/env python3
"""
newsapi_fetch.py

Search news articles via the NewsAPI /v2/everything endpoint.

Usage:
    python newsapi_fetch.py "OpenAI" --limit 50
    python newsapi_fetch.py "Morocco" --limit 20 --output articles.json
    python newsapi_fetch.py "climate" --language en --sort-by publishedAt

Credentials:
    NEWSAPI_KEY   NewsAPI key (env var overrides the default in code)

Streaming behavior:
    - No --output: each article is printed as one JSON line (JSONL) to stdout,
      flushed immediately. All logs go to stderr.
    - With --output: a single JSON object {query, platform, count, articles}
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

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("newsapi_fetch")

NEWSAPI_BASE_URL = "https://newsapi.org/v2/everything"
REQUEST_TIMEOUT = 30
MAX_PAGE_SIZE = 100
PAGE_DELAY_SECONDS = 0.5


def get_api_key() -> str:
    api_key = os.environ.get("NEWSAPI_KEY", "").strip()
    if not api_key:
        raise RuntimeError("NewsAPI key is required. Set the NEWSAPI_KEY environment variable.")
    return api_key


def newsapi_get(params: Dict[str, Any]) -> Dict[str, Any]:
    response = requests.get(NEWSAPI_BASE_URL, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if data.get("status") != "ok":
        message = data.get("message") or data.get("code") or "Unknown NewsAPI error"
        raise RuntimeError(message)
    return data


def normalize_article(raw: Dict[str, Any], keyword: str) -> Dict[str, Any]:
    source = raw.get("source") or {}
    description = raw.get("description")
    content = raw.get("content")
    body = description or content

    return {
        "article_id": raw.get("url"),
        "title": raw.get("title"),
        "body": body,
        "content": content,
        "description": description,
        "author": raw.get("author"),
        "source": source.get("name"),
        "source_id": source.get("id"),
        "url": raw.get("url"),
        "image_url": raw.get("urlToImage"),
        "published_at": raw.get("publishedAt"),
        "keyword": keyword,
        "platform": "newsapi",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_articles(
    keyword: str,
    api_key: str,
    limit: int,
    language: Optional[str],
    sort_by: str,
    date_from: Optional[str],
    date_to: Optional[str],
    emit_fn: Callable[[dict], None],
) -> int:
    count = 0
    page = 1

    while count < limit:
        page_size = min(MAX_PAGE_SIZE, limit - count)
        params: Dict[str, Any] = {
            "q": keyword,
            "apiKey": api_key,
            "pageSize": page_size,
            "page": page,
            "sortBy": sort_by,
        }
        if language:
            params["language"] = language
        if date_from:
            params["from"] = date_from
        if date_to:
            params["to"] = date_to

        log.info(
            "Fetching NewsAPI page %d for %r (page_size=%d, collected=%d/%d)",
            page,
            keyword,
            page_size,
            count,
            limit,
        )
        data = newsapi_get(params)
        articles = data.get("articles") or []
        if not articles:
            log.info("No articles returned on page %d.", page)
            break

        for raw in articles:
            if count >= limit:
                break
            emit_fn(normalize_article(raw, keyword))
            count += 1

        if count >= limit:
            break

        total_results = data.get("totalResults") or 0
        if page * page_size >= total_results:
            log.info("Reached end of results (%d total).", total_results)
            break

        page += 1
        time.sleep(PAGE_DELAY_SECONDS)

    return count


def scrape(
    keyword: str,
    limit: int,
    emit_fn: Callable[[dict], None],
    language: Optional[str],
    sort_by: str,
    date_from: Optional[str],
    date_to: Optional[str],
) -> int:
    api_key = get_api_key()
    return fetch_articles(
        keyword=keyword,
        api_key=api_key,
        limit=limit,
        language=language,
        sort_by=sort_by,
        date_from=date_from,
        date_to=date_to,
        emit_fn=emit_fn,
    )


_scrape_articles = scrape


import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scrapers.base import BaseScraper, ScraperConfig, ScraperResult


class NewsApiScraper(BaseScraper):
    platform = "newsapi"
    items_key = "articles"

    def validate_config(self, config: ScraperConfig) -> None:
        if not config.keyword.strip():
            raise ValueError("keyword is required")

    def scrape(self, config: ScraperConfig) -> ScraperResult:
        self.validate_config(config)
        items: List[Dict[str, Any]] = []

        def emit(article: dict) -> None:
            items.append(self.normalize_item(article))

        count = _scrape_articles(
            config.keyword,
            config.limit,
            emit,
            config.extra.get("language"),
            config.extra.get("sort_by", "publishedAt"),
            config.extra.get("date_from"),
            config.extra.get("date_to"),
        )
        return ScraperResult(
            query=config.keyword,
            platform=self.platform,
            count=count,
            items=items,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Search news articles via NewsAPI.")
    parser.add_argument("keyword", help="Search keyword, e.g. 'OpenAI' or 'Morocco'")
    parser.add_argument("--limit", type=int, default=50, help="Max articles to fetch (default: 50)")
    parser.add_argument("--output", default=None, help="Optional file path to save JSON output")
    parser.add_argument("--language", default=None, help="Language code, e.g. en or fr")
    parser.add_argument(
        "--sort-by",
        default="publishedAt",
        choices=["relevancy", "popularity", "publishedAt"],
        help="Sort order (default: publishedAt)",
    )
    parser.add_argument("--from", dest="date_from", default=None, help="Start date (ISO), e.g. 2026-01-01")
    parser.add_argument("--to", dest="date_to", default=None, help="End date (ISO), e.g. 2026-07-01")
    args = parser.parse_args()

    log.info(
        "Starting NewsAPI scrape for %r (limit=%d, language=%s, sort_by=%s)",
        args.keyword,
        args.limit,
        args.language,
        args.sort_by,
    )

    try:
        if args.output:
            collected: List[Dict[str, Any]] = []

            def emit(article: dict) -> None:
                collected.append(article)
                log.info("Collected article %d/%d", len(collected), args.limit)

            count = scrape(
                args.keyword,
                args.limit,
                emit,
                args.language,
                args.sort_by,
                args.date_from,
                args.date_to,
            )

            result = {
                "query": args.keyword,
                "platform": "newsapi",
                "count": count,
                "articles": collected,
            }
            with open(args.output, "w", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2)
            log.info("Wrote %d articles to %s", count, args.output)
        else:
            def emit(article: dict) -> None:
                print(json.dumps(article, ensure_ascii=False))
                sys.stdout.flush()

            count = scrape(
                args.keyword,
                args.limit,
                emit,
                args.language,
                args.sort_by,
                args.date_from,
                args.date_to,
            )
            log.info("Done. %d article(s) streamed.", count)
    except requests.HTTPError as exc:
        log.error("NewsAPI HTTP error: %s", exc)
        return 1
    except Exception as exc:
        log.error("NewsAPI scrape failed: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
