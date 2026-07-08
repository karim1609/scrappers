#!/usr/bin/env python3
"""
amazon_fetch.py

Scrape Amazon product reviews for a given ASIN or product URL.

Usage:
    python amazon_fetch.py B08N5WRWNW --limit 50
    python amazon_fetch.py https://www.amazon.com/dp/B08N5WRWNW --limit 100 --output reviews.json

Streaming behavior:
    - No --output: each review is printed as one JSON line (JSONL) to stdout,
      flushed immediately as it's scraped. All logs go to stderr.
    - With --output: a single JSON object {query, platform, count, reviews}
      is written to the given file path.
"""

import argparse
import json
import re
import sys
import time
import logging
from typing import Optional, List, Dict, Any

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("amazon_fetch")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}
REVIEW_SELECTOR = 'div[data-hook="review"]'
CAPTCHA_SELECTOR = 'form[action="/errors/validateCaptcha"]'
NEXT_PAGE_SELECTOR = "li.a-last a"


def extract_asin(query: str) -> str:
    """Accept a raw ASIN or a full product URL and return the ASIN."""
    query = query.strip()
    if re.fullmatch(r"[A-Z0-9]{10}", query):
        return query

    # Look for /dp/ASIN, /product-reviews/ASIN, /gp/product/ASIN patterns
    match = re.search(r"/(?:dp|product-reviews|gp/product)/([A-Z0-9]{10})", query)
    if match:
        return match.group(1)

    raise ValueError(f"Could not extract a valid ASIN from: {query}")


def build_review_url(asin: str, page: int) -> str:
    return (
        f"https://www.amazon.com/product-reviews/{asin}/"
        f"ref=cm_cr_arp_d_paging_btm_next_{page}"
        f"?ie=UTF8&reviewerType=all_reviews&sortBy=recent&pageNumber={page}"
    )


def block_heavy_resources(route):
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def parse_reviews(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    reviews = []

    for block in soup.select(REVIEW_SELECTOR):
        review_id = block.get("id")

        title_el = block.select_one('[data-hook="review-title"]')
        title = title_el.get_text(strip=True) if title_el else None
        # Amazon nests the rating text and title text together; strip rating span if present
        if title_el:
            rating_span = title_el.select_one("span.a-icon-alt")
            if rating_span:
                rating_span.extract()
            title = title_el.get_text(strip=True)

        body_el = block.select_one('[data-hook="review-body"]')
        body = body_el.get_text(" ", strip=True) if body_el else None

        rating_el = block.select_one('[data-hook="review-star-rating"] span.a-icon-alt') or \
            block.select_one('i[data-hook="review-star-rating"] span.a-icon-alt')
        rating = None
        if rating_el:
            m = re.search(r"([\d.]+)\s+out of\s+5", rating_el.get_text())
            if m:
                rating = float(m.group(1))

        date_location_el = block.select_one('[data-hook="review-date"]')
        date_location = date_location_el.get_text(strip=True) if date_location_el else None

        author_el = block.select_one(".a-profile-name")
        author = author_el.get_text(strip=True) if author_el else None

        verified_el = block.select_one('[data-hook="avp-badge"]')
        is_verified = verified_el is not None

        reviews.append({
            "review_id": review_id,
            "title": title,
            "body": body,
            "rating": rating,
            "date_location": date_location,
            "author": author,
            "is_verified": is_verified,
            "platform": "amazon",
        })

    return reviews


def scrape(asin: str, limit: int, emit_fn) -> int:
    """Scrape reviews for an ASIN, calling emit_fn(review) for each one found.
    Returns total number of reviews emitted."""
    count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="en-US",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()
        page.route("**/*", block_heavy_resources)

        page_num = 1
        try:
            while count < limit:
                url = build_review_url(asin, page_num)
                log.info("Navigating to page %d: %s", page_num, url)

                try:
                    page.goto(url, timeout=30000)
                except PWTimeoutError:
                    log.warning("Timeout loading page %d, stopping.", page_num)
                    break

                html = page.content()
                if BeautifulSoup(html, "html.parser").select_one(CAPTCHA_SELECTOR):
                    log.warning("Captcha detected on page %d. Stopping gracefully.", page_num)
                    break

                try:
                    page.wait_for_selector(REVIEW_SELECTOR, timeout=15000)
                except PWTimeoutError:
                    log.warning("No reviews found on page %d, stopping.", page_num)
                    break

                html = page.content()
                page_reviews = parse_reviews(html)
                if not page_reviews:
                    log.info("No more reviews found, stopping.")
                    break

                for review in page_reviews:
                    if count >= limit:
                        break
                    emit_fn(review)
                    count += 1

                if count >= limit:
                    break

                next_link = page.query_selector(NEXT_PAGE_SELECTOR)
                if not next_link:
                    log.info("No next page link found, stopping.")
                    break

                page_num += 1
                time.sleep(1.5)  # polite delay between pages
        finally:
            browser.close()

    return count


def main():
    parser = argparse.ArgumentParser(description="Scrape Amazon product reviews.")
    parser.add_argument("keyword", help="ASIN (e.g. B08N5WRWNW) or full Amazon product URL")
    parser.add_argument("--limit", type=int, default=50, help="Max reviews to extract (default: 50)")
    parser.add_argument("--output", default=None, help="Optional file path to save JSON output")
    args = parser.parse_args()

    try:
        asin = extract_asin(args.keyword)
    except ValueError as exc:
        log.error(str(exc))
        sys.exit(1)

    log.info("Starting Amazon review scrape for ASIN=%s (limit=%d)", asin, args.limit)

    if args.output:
        collected: List[Dict[str, Any]] = []

        def emit(review):
            collected.append(review)
            log.info("Collected review %d/%d", len(collected), args.limit)

        count = scrape(asin, args.limit, emit)

        result = {
            "query": args.keyword,
            "platform": "amazon",
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

        count = scrape(asin, args.limit, emit)
        log.info("Done. %d review(s) streamed.", count)


if __name__ == "__main__":
    main()