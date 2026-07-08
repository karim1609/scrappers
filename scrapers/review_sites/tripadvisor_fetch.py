#!/usr/bin/env python3
"""
tripadvisor_fetch.py

Scrape Tripadvisor reviews for a given place. Accepts either a direct
Tripadvisor profile URL or a free-text keyword (e.g. "Eiffel Tower"),
in which case it resolves the URL via a DuckDuckGo HTML search fallback.

Usage:
    python tripadvisor_fetch.py "Eiffel Tower" --limit 50
    python tripadvisor_fetch.py https://www.tripadvisor.com/Attraction_Review-... --limit 100 --output reviews.json

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
from urllib.parse import quote_plus
from typing import Optional, List, Dict, Any

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("tripadvisor_fetch")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}
REVIEW_SELECTOR = 'div[data-test-target="HR_CC_CARD"], div.review-container'
NEXT_PAGE_SELECTORS = ["a.ui_button.nav.next.primary", "a.nav.next"]
DUCKDUCKGO_SEARCH_URL = "https://html.duckduckgo.com/html/?q={query}"


def block_heavy_resources(route):
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def is_tripadvisor_url(text: str) -> bool:
    return "tripadvisor.com" in text.lower()


def resolve_url_via_duckduckgo(page, keyword: str) -> Optional[str]:
    """Fallback: search DuckDuckGo's HTML endpoint for a Tripadvisor page."""
    query = f"{keyword} site:tripadvisor.com"
    url = DUCKDUCKGO_SEARCH_URL.format(query=quote_plus(query))
    log.info("Falling back to DuckDuckGo search: %s", url)

    try:
        page.goto(url, timeout=20000)
        page.wait_for_selector("a.result__a", timeout=10000)
    except PWTimeoutError:
        log.warning("DuckDuckGo search timed out.")
        return None

    html = page.content()
    soup = BeautifulSoup(html, "html.parser")

    for link in soup.select("a.result__a"):
        href = link.get("href", "")
        if "tripadvisor.com" in href:
            return href

    log.warning("No Tripadvisor result found in DuckDuckGo search results.")
    return None


def resolve_tripadvisor_url(page, keyword: str) -> Optional[str]:
    """Try navigating directly to Tripadvisor's search; fall back to DuckDuckGo on failure."""
    if is_tripadvisor_url(keyword):
        return keyword

    search_url = f"https://www.tripadvisor.com/Search?q={quote_plus(keyword)}"
    log.info("Attempting direct Tripadvisor search: %s", search_url)

    try:
        page.goto(search_url, timeout=20000)
        page.wait_for_selector('a[href*="Review"], a[href*="Attraction_Review"]', timeout=10000)
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        link = soup.select_one('a[href*="Review"], a[href*="Attraction_Review"]')
        if link and link.get("href"):
            href = link["href"]
            if href.startswith("/"):
                href = "https://www.tripadvisor.com" + href
            return href
    except PWTimeoutError:
        log.warning("Direct Tripadvisor search failed or was blocked (bot detection).")

    # Fallback to DuckDuckGo
    return resolve_url_via_duckduckgo(page, keyword)


def parse_rating_from_classes(el) -> Optional[float]:
    """Tripadvisor encodes rating as e.g. class 'bubble_45' => 4.5 / 5."""
    if el is None:
        return None
    for cls in el.get("class", []):
        m = re.match(r"bubble_(\d+)", cls)
        if m:
            return int(m.group(1)) / 10.0
    return None


def parse_rating_from_svg_title(block) -> Optional[float]:
    svg_title = block.select_one("svg title")
    if svg_title:
        m = re.search(r"([\d.]+)\s+of\s+5", svg_title.get_text())
        if m:
            return float(m.group(1))
    return None


def parse_reviews(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    reviews = []

    for block in soup.select(REVIEW_SELECTOR):
        review_id = block.get("data-reviewid") or block.get("id")

        title_el = block.select_one('[data-test-target="review-title"], a.reviewSelector, span.noQuotes')
        title = title_el.get_text(strip=True) if title_el else None

        body_el = block.select_one('q.QewHA, p.partial_entry, span[data-test-target="review-text"]')
        body = body_el.get_text(" ", strip=True) if body_el else None

        rating_el = block.select_one('[class*="bubble_"]')
        rating = parse_rating_from_classes(rating_el)
        if rating is None:
            rating = parse_rating_from_svg_title(block)

        date_el = block.select_one('.ratingDate, span.euPKI9')
        date = None
        if date_el:
            date = date_el.get("title") or date_el.get_text(strip=True)

        author_el = block.select_one('.info_text div, a.ui_header_link, span.expand_inline')
        author = author_el.get_text(strip=True) if author_el else None

        reviews.append({
            "review_id": review_id,
            "title": title,
            "body": body,
            "rating": rating,
            "date": date,
            "author": author,
            "platform": "tripadvisor",
        })

    return reviews


def scrape(target_url: str, limit: int, emit_fn) -> int:
    """Scrape reviews from a resolved Tripadvisor URL, calling emit_fn(review) for each.
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

        try:
            log.info("Navigating to: %s", target_url)
            try:
                page.goto(target_url, timeout=30000)
            except PWTimeoutError:
                log.warning("Timeout loading target page.")
                return count

            try:
                page.wait_for_selector(REVIEW_SELECTOR, timeout=15000)
            except PWTimeoutError:
                log.warning("No reviews found on initial page, stopping.")
                return count

            page_num = 1
            while count < limit:
                html = page.content()
                page_reviews = parse_reviews(html)
                if not page_reviews:
                    log.info("No reviews parsed on page %d, stopping.", page_num)
                    break

                for review in page_reviews:
                    if count >= limit:
                        break
                    emit_fn(review)
                    count += 1

                if count >= limit:
                    break

                next_link = None
                for selector in NEXT_PAGE_SELECTORS:
                    next_link = page.query_selector(selector)
                    if next_link:
                        break

                if not next_link:
                    log.info("No next page button found, stopping.")
                    break

                log.info("Clicking next page (page %d -> %d)", page_num, page_num + 1)
                try:
                    next_link.click()
                    page.wait_for_selector(REVIEW_SELECTOR, timeout=15000)
                except PWTimeoutError:
                    log.warning("Timed out loading next page, stopping.")
                    break

                page_num += 1
                time.sleep(1.5)  # polite delay between pages
        finally:
            browser.close()

    return count


def main():
    parser = argparse.ArgumentParser(description="Scrape Tripadvisor reviews.")
    parser.add_argument("keyword", help="Tripadvisor profile URL or a search keyword (e.g. 'Eiffel Tower')")
    parser.add_argument("--limit", type=int, default=50, help="Max reviews to extract (default: 50)")
    parser.add_argument("--output", default=None, help="Optional file path to save JSON output")
    args = parser.parse_args()

    log.info("Starting Tripadvisor review scrape for %r (limit=%d)", args.keyword, args.limit)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(user_agent=USER_AGENT, locale="en-US")
        resolver_page = context.new_page()
        target_url = resolve_tripadvisor_url(resolver_page, args.keyword)
        browser.close()

    if not target_url:
        log.error("Could not resolve a Tripadvisor URL for %r", args.keyword)
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
            "platform": "tripadvisor",
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