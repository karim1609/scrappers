#!/usr/bin/env python3
"""
g2_fetch.py

Scrape G2.com product reviews for a given product URL or search keyword.

Usage:
    python g2_fetch.py "Salesforce" --limit 50
    python g2_fetch.py https://www.g2.com/products/salesforce-platform/reviews --limit 100 --output reviews.json

Strategy:
    1. Resolve a G2 product-reviews URL from the keyword (direct if it's already
       a g2.com URL, otherwise via a DuckDuckGo HTML search fallback).
    2. On each reviews page, first try to parse embedded JSON-LD structured
       data (schema.org Review objects) -- this is far more stable than CSS
       selectors since it's SEO-driven and rarely changes shape.
    3. Fall back to DOM scraping via itemprop="review" microdata if no
       JSON-LD reviews are found.
    4. Paginate via ?page=N in the URL.

Streaming behavior:
    - No --output: each review is printed as one JSON line (JSONL) to stdout,
      flushed immediately as it's scraped. All logs go to stderr.
    - With --output: a single JSON object {query, platform, count, reviews}
      is written to the given file path.
"""

import argparse
import json
import os
import re
import sys
import time
import logging
from urllib.parse import quote_plus, urlparse, urlunparse, parse_qs, urlencode
from typing import Optional, List, Dict, Any

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("g2_fetch")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}
REVIEW_ITEMPROP_SELECTOR = '[itemprop="review"]'
DUCKDUCKGO_SEARCH_URL = "https://html.duckduckgo.com/html/?q={query}"
DEBUG_DIR = "/app/output"


def block_heavy_resources(route):
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def _dump_debug(page, tag: str):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        html_path = os.path.join(DEBUG_DIR, f"debug_{tag}.html")
        png_path = os.path.join(DEBUG_DIR, f"debug_{tag}.png")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(page.content())
        page.screenshot(path=png_path, full_page=True)
        log.warning("Saved debug snapshot: %s , %s", html_path, png_path)
    except Exception as exc:
        log.warning("Could not save debug snapshot: %s", exc)


def is_g2_reviews_url(text: str) -> bool:
    return "g2.com" in text.lower()


def resolve_url_via_duckduckgo(page, keyword: str, site: str) -> Optional[str]:
    query = f"{keyword} reviews site:{site}"
    url = DUCKDUCKGO_SEARCH_URL.format(query=quote_plus(query))
    log.info("Falling back to DuckDuckGo search: %s", url)

    try:
        page.goto(url, timeout=20000)
        page.wait_for_selector("a.result__a", timeout=10000)
    except PWTimeoutError:
        log.warning("DuckDuckGo search timed out.")
        _dump_debug(page, "g2_duckduckgo_timeout")
        return None

    soup = BeautifulSoup(page.content(), "html.parser")
    for link in soup.select("a.result__a"):
        href = link.get("href", "")
        if site in href:
            return href

    log.warning("No %s result found in DuckDuckGo search results.", site)
    return None


def resolve_g2_url(page, keyword: str) -> Optional[str]:
    if is_g2_reviews_url(keyword):
        url = keyword
    else:
        url = resolve_url_via_duckduckgo(page, keyword, "g2.com")
        if not url:
            return None

    # Normalize to the /reviews sub-path if it isn't already there
    if "/reviews" not in url:
        url = url.rstrip("/") + "/reviews"
    return url


def set_page_param(url: str, page_num: int) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    qs["page"] = [str(page_num)]
    new_query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def extract_json_ld_reviews(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """Look for schema.org Review objects embedded as JSON-LD."""
    reviews = []

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue

        candidates = data if isinstance(data, list) else [data]
        for node in candidates:
            reviews.extend(_walk_for_reviews(node))

    return reviews


def _walk_for_reviews(node: Any) -> List[Dict[str, Any]]:
    found = []
    if isinstance(node, dict):
        node_type = node.get("@type")
        if node_type == "Review" or (isinstance(node_type, list) and "Review" in node_type):
            found.append(_normalize_json_ld_review(node))
        # Reviews are often nested under "review" (single or list)
        if "review" in node:
            nested = node["review"]
            nested_list = nested if isinstance(nested, list) else [nested]
            for n in nested_list:
                found.extend(_walk_for_reviews(n))
        for value in node.values():
            if isinstance(value, (dict, list)) and not isinstance(value, str):
                if value is not node.get("review"):
                    found.extend(_walk_for_reviews(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_for_reviews(item))
    return found


def _normalize_json_ld_review(node: Dict[str, Any]) -> Dict[str, Any]:
    author = node.get("author")
    if isinstance(author, dict):
        author_name = author.get("name")
    else:
        author_name = author

    rating = None
    rating_obj = node.get("reviewRating")
    if isinstance(rating_obj, dict):
        try:
            rating = float(rating_obj.get("ratingValue"))
        except (TypeError, ValueError):
            rating = None

    return {
        "review_id": node.get("@id") or node.get("url"),
        "title": node.get("name"),
        "body": node.get("reviewBody") or node.get("description"),
        "rating": rating,
        "date": node.get("datePublished"),
        "author": author_name,
        "platform": "g2",
    }


def extract_dom_reviews(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """Fallback: parse via itemprop="review" microdata blocks."""
    reviews = []
    for block in soup.select(REVIEW_ITEMPROP_SELECTOR):
        title_el = block.select_one('[itemprop="name"]')
        body_el = block.select_one('[itemprop="reviewBody"], [itemprop="description"]')
        author_el = block.select_one('[itemprop="author"]')
        date_el = block.select_one('[itemprop="datePublished"]')
        rating_el = block.select_one('[itemprop="ratingValue"]')

        rating = None
        if rating_el:
            rating_text = rating_el.get("content") or rating_el.get_text(strip=True)
            try:
                rating = float(rating_text)
            except (TypeError, ValueError):
                rating = None

        reviews.append({
            "review_id": block.get("id"),
            "title": title_el.get_text(strip=True) if title_el else None,
            "body": body_el.get_text(" ", strip=True) if body_el else None,
            "rating": rating,
            "date": (date_el.get("content") or date_el.get_text(strip=True)) if date_el else None,
            "author": author_el.get_text(strip=True) if author_el else None,
            "platform": "g2",
        })
    return reviews


def parse_reviews(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    reviews = extract_json_ld_reviews(soup)
    if reviews:
        return reviews
    return extract_dom_reviews(soup)


def scrape(start_url: str, limit: int, emit_fn) -> int:
    count = 0
    seen_ids = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(user_agent=USER_AGENT, locale="en-US")
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()
        page.route("**/*", block_heavy_resources)

        try:
            page_num = 1
            while count < limit:
                url = start_url if page_num == 1 else set_page_param(start_url, page_num)
                log.info("Navigating to page %d: %s", page_num, url)

                try:
                    page.goto(url, timeout=30000)
                    page.wait_for_load_state("networkidle", timeout=15000)
                except PWTimeoutError:
                    log.warning("Timeout loading page %d.", page_num)
                    if page_num == 1:
                        _dump_debug(page, "g2_load_timeout")
                    break

                html = page.content()
                page_reviews = parse_reviews(html)

                if not page_reviews:
                    log.info("No reviews parsed on page %d, stopping.", page_num)
                    if page_num == 1:
                        _dump_debug(page, "g2_no_reviews")
                    break

                new_on_page = 0
                for review in page_reviews:
                    key = review.get("review_id") or (review.get("author"), review.get("body"))
                    if key in seen_ids:
                        continue
                    seen_ids.add(key)
                    new_on_page += 1

                    if count >= limit:
                        break
                    emit_fn(review)
                    count += 1

                if new_on_page == 0:
                    log.info("No new reviews on page %d (likely reached the end), stopping.", page_num)
                    break

                if count >= limit:
                    break

                page_num += 1
                time.sleep(1.5)
        finally:
            browser.close()

    return count


def main():
    parser = argparse.ArgumentParser(description="Scrape G2 product reviews.")
    parser.add_argument("keyword", help="G2 product/reviews URL or a search keyword (e.g. 'Salesforce')")
    parser.add_argument("--limit", type=int, default=50, help="Max reviews to extract (default: 50)")
    parser.add_argument("--output", default=None, help="Optional file path to save JSON output")
    args = parser.parse_args()

    log.info("Starting G2 review scrape for %r (limit=%d)", args.keyword, args.limit)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(user_agent=USER_AGENT, locale="en-US")
        resolver_page = context.new_page()
        target_url = resolve_g2_url(resolver_page, args.keyword)
        browser.close()

    if not target_url:
        log.error("Could not resolve a G2 reviews URL for %r", args.keyword)
        sys.exit(1)

    log.info("Resolved target URL: %s", target_url)

    if args.output:
        collected: List[Dict[str, Any]] = []

        def emit(review):
            collected.append(review)
            log.info("Collected review %d/%d", len(collected), args.limit)

        count = scrape(target_url, args.limit, emit)

        result = {
            "query": args.keyword,
            "platform": "g2",
            "count": count,
            "reviews": collected,
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        log.info("Wrote %d reviews to %s", count, args.output)
    else:
        def emit(review):
            print(json.dumps(review, ensure_ascii=False))
            sys.stdout.flush()

        count = scrape(target_url, args.limit, emit)
        log.info("Done. %d review(s) streamed.", count)


if __name__ == "__main__":
    main()