#!/usr/bin/env python3
"""
guardian_fetch.py

Search news articles via The Guardian Open Platform Content API.

Usage:
    python guardian_fetch.py "OpenAI" --limit 50
    python guardian_fetch.py "Morocco" --limit 20 --output articles.json
    python guardian_fetch.py "climate" --section environment --order-by newest

Credentials:
    GUARDIAN_API_KEY   Guardian API key (env var overrides the default in code)

Streaming behavior:
    - No --output: each article is printed as one JSON line (JSONL) to stdout,
      flushed immediately. All logs go to stderr.
    - With --output: a single JSON object {query, platform, count, articles}
      is written to the given file path.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("guardian_fetch")

GUARDIAN_BASE_URL = "https://content.guardianapis.com/search"
REQUEST_TIMEOUT = 30
MAX_PAGE_SIZE = 50
PAGE_DELAY_SECONDS = 0.3
SHOW_FIELDS = "headline,trailText,body,byline,thumbnail,short-url"


def get_api_key() -> str:
    api_key = os.environ.get("GUARDIAN_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "Guardian API key is required. Set the GUARDIAN_API_KEY environment variable."
        )
    return api_key


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip = True
        if tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "blockquote"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self._parts.append(html.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self._parts.append(html.unescape(f"&#{name};"))

    def text(self) -> str:
        raw = "".join(self._parts)
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def strip_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    parser = _TextExtractor()
    parser.feed(raw_html)
    return parser.text()


def guardian_get(params: Dict[str, Any]) -> Dict[str, Any]:
    response = requests.get(GUARDIAN_BASE_URL, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    payload = data.get("response") or {}
    if payload.get("status") != "ok":
        message = payload.get("message") or "Unknown Guardian API error"
        raise RuntimeError(message)
    return payload


def _contributors_from_tags(tags: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for tag in tags or []:
        if tag.get("type") == "contributor":
            name = tag.get("webTitle") or tag.get("id")
            if name:
                names.append(name)
    return names


def normalize_article(raw: Dict[str, Any], keyword: str) -> Dict[str, Any]:
    fields = raw.get("fields") or {}
    body_html = fields.get("body") or ""
    body = strip_html(body_html) or fields.get("trailText")
    contributors = _contributors_from_tags(raw.get("tags") or [])
    byline = fields.get("byline")
    author = contributors or ([byline] if byline else None)

    return {
        "article_id": raw.get("id"),
        "title": fields.get("headline") or raw.get("webTitle"),
        "body": body,
        "trail_text": fields.get("trailText"),
        "author": author,
        "source": "The Guardian",
        "section": raw.get("sectionName"),
        "section_id": raw.get("sectionId"),
        "pillar": raw.get("pillarName"),
        "url": raw.get("webUrl") or fields.get("short-url"),
        "image_url": fields.get("thumbnail"),
        "published_at": raw.get("webPublicationDate"),
        "keyword": keyword,
        "platform": "guardian",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_articles(
    keyword: str,
    api_key: str,
    limit: int,
    section: Optional[str],
    tag: Optional[str],
    order_by: str,
    from_date: Optional[str],
    to_date: Optional[str],
    lang: Optional[str],
    production_office: Optional[str],
    emit_fn: Callable[[dict], None],
) -> int:
    count = 0
    page = 1

    while count < limit:
        page_size = min(MAX_PAGE_SIZE, limit - count)
        params: Dict[str, Any] = {
            "q": keyword,
            "api-key": api_key,
            "format": "json",
            "page": page,
            "page-size": page_size,
            "order-by": order_by,
            "show-fields": SHOW_FIELDS,
            "show-tags": "contributor,keyword",
        }
        if section:
            params["section"] = section
        if tag:
            params["tag"] = tag
        if from_date:
            params["from-date"] = from_date
        if to_date:
            params["to-date"] = to_date
        if lang:
            params["lang"] = lang
        if production_office:
            params["production-office"] = production_office

        log.info(
            "Fetching Guardian page %d for %r (page_size=%d, collected=%d/%d)",
            page,
            keyword,
            page_size,
            count,
            limit,
        )
        payload = guardian_get(params)
        results = payload.get("results") or []
        if not results:
            log.info("No articles returned on page %d.", page)
            break

        for raw in results:
            if count >= limit:
                break
            emit_fn(normalize_article(raw, keyword))
            count += 1

        if count >= limit:
            break

        total_pages = payload.get("pages") or 0
        if page >= total_pages:
            total = payload.get("total") or 0
            log.info("Reached end of results (%d total).", total)
            break

        page += 1
        time.sleep(PAGE_DELAY_SECONDS)

    return count


def scrape(
    keyword: str,
    limit: int,
    emit_fn: Callable[[dict], None],
    section: Optional[str],
    tag: Optional[str],
    order_by: str,
    from_date: Optional[str],
    to_date: Optional[str],
    lang: Optional[str],
    production_office: Optional[str],
) -> int:
    api_key = get_api_key()
    return fetch_articles(
        keyword=keyword,
        api_key=api_key,
        limit=limit,
        section=section,
        tag=tag,
        order_by=order_by,
        from_date=from_date,
        to_date=to_date,
        lang=lang,
        production_office=production_office,
        emit_fn=emit_fn,
    )


_scrape_articles = scrape

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scrapers.base import BaseScraper, ScraperConfig, ScraperResult


class GuardianScraper(BaseScraper):
    platform = "guardian"
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
            config.extra.get("section"),
            config.extra.get("tag"),
            config.extra.get("order_by", "relevance"),
            config.extra.get("from_date"),
            config.extra.get("to_date"),
            config.extra.get("lang"),
            config.extra.get("production_office"),
        )
        return ScraperResult(
            query=config.keyword,
            platform=self.platform,
            count=count,
            items=items,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Search news articles via The Guardian Content API."
    )
    parser.add_argument("keyword", help="Search keyword, e.g. 'OpenAI' or 'Morocco'")
    parser.add_argument("--limit", type=int, default=50, help="Max articles to fetch (default: 50)")
    parser.add_argument("--output", default=None, help="Optional file path to save JSON output")
    parser.add_argument("--section", default=None, help="Guardian section, e.g. environment or politics")
    parser.add_argument("--tag", default=None, help="Guardian tag filter, e.g. environment/climate-change")
    parser.add_argument(
        "--order-by",
        default="relevance",
        choices=["relevance", "newest", "oldest"],
        help="Sort order (default: relevance)",
    )
    parser.add_argument("--from", dest="from_date", default=None, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date", default=None, help="End date (YYYY-MM-DD)")
    parser.add_argument("--lang", default=None, help="Language code, e.g. en or fr")
    parser.add_argument(
        "--production-office",
        default=None,
        choices=["uk", "us", "au"],
        help="Filter by Guardian edition office",
    )
    args = parser.parse_args()

    log.info(
        "Starting Guardian scrape for %r (limit=%d, section=%s, order_by=%s)",
        args.keyword,
        args.limit,
        args.section,
        args.order_by,
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
                args.section,
                args.tag,
                args.order_by,
                args.from_date,
                args.to_date,
                args.lang,
                args.production_office,
            )

            result = {
                "query": args.keyword,
                "platform": "guardian",
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
                args.section,
                args.tag,
                args.order_by,
                args.from_date,
                args.to_date,
                args.lang,
                args.production_office,
            )
            log.info("Done. %d article(s) streamed.", count)
    except requests.HTTPError as exc:
        log.error("Guardian HTTP error: %s", exc)
        return 1
    except Exception as exc:
        log.error("Guardian scrape failed: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
