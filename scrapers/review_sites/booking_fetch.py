#!/usr/bin/env python3
"""
booking_fetch.py

Scrape Booking.com guest reviews for a hotel/property. Accepts either a direct
Booking.com property URL or a free-text keyword (e.g. "Hilton London Paris"),
in which case it searches Booking.com and opens the first matching property.

Usage:
    python booking_fetch.py "Hilton London" --limit 50
    python booking_fetch.py https://www.booking.com/hotel/gb/hilton-london-park-lane.html --limit 20 --output reviews.json

Streaming behavior:
    - No --output: each review is printed as one JSON line (JSONL) to stdout,
      flushed immediately as it's scraped. All logs go to stderr.
    - With --output: a single JSON object {query, platform, count, reviews}
      is written to the given file path.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin

from playwright.sync_api import (
    Locator,
    Page,
    TimeoutError as PWTimeoutError,
    sync_playwright,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("booking_fetch")

BASE_URL = "https://www.booking.com/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
NAV_TIMEOUT_MS = 20_000
SHORT_TIMEOUT_MS = 5_000
DEBUG_DIR = "/app/output"

REVIEW_CARD_SELECTORS = [
    "[data-testid='review-card']",
    "div[data-review-id]",
    "li.review_item",
    "[data-testid='PropertyReviewsRegionBlock'] li",
    "[data-testid='PropertyReviewsRegionBlock'] article",
    "[data-testid*='review-card' i]",
]

TRAVELER_TYPE_KEYWORDS = [
    "Solo traveler", "Couple", "Family", "Group", "Business traveler",
    "Traveled with friends", "Traveled with pets",
]


def is_booking_property_url(text: str) -> bool:
    lowered = text.lower()
    return "booking.com" in lowered and ("/hotel/" in lowered or "/reviews/" in lowered)


def is_property_page_url(url: str) -> bool:
    lowered = url.lower()
    return "/hotel/" in lowered and "searchresults" not in lowered


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def safe_text(locator: Optional[Locator], timeout: int = SHORT_TIMEOUT_MS) -> Optional[str]:
    if locator is None:
        return None
    try:
        if locator.count() == 0:
            return None
        text = locator.first.inner_text(timeout=timeout).strip()
        return text if text else None
    except (PWTimeoutError, Exception):
        return None


def first_matching(scope, selectors: list) -> Optional[Locator]:
    for sel in selectors:
        try:
            loc = scope.locator(sel)
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def wait_first_matching(page: Page, selectors: list, timeout: int = 8000) -> Optional[Locator]:
    per_selector_timeout = max(timeout // max(len(selectors), 1), 1500)
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=per_selector_timeout, state="visible")
            loc = page.locator(sel)
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def debug_dump(page: Page, name: str) -> None:
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        screenshot_path = os.path.join(DEBUG_DIR, f"debug_{name}.png")
        html_path = os.path.join(DEBUG_DIR, f"debug_{name}.html")
        testids_path = os.path.join(DEBUG_DIR, f"debug_{name}_testids.txt")

        page.screenshot(path=screenshot_path, full_page=True)
        html = page.content()
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)

        testids = sorted(set(re.findall(r'data-testid="([^"]*)"', html)))
        with open(testids_path, "w", encoding="utf-8") as f:
            f.write("\n".join(testids))

        log.warning("Saved debug snapshot: %s, %s, %s", screenshot_path, html_path, testids_path)
    except Exception as exc:
        log.warning("Could not save debug dump: %s", exc)


def dismiss_overlays(page: Page) -> None:
    candidates = [
        "#onetrust-accept-btn-handler",
        "button[aria-label='Dismiss sign-in info.']",
        "button[aria-label='Close']",
        "[data-testid='cross-icon']",
        "button:has-text('Accept')",
        "button:has-text('Accept all')",
        "button:has-text('I agree')",
        "button[id*='accept' i]",
        "button[aria-label*='dismiss' i]",
    ]
    for sel in candidates:
        try:
            btn = page.locator(sel)
            if btn.count() > 0 and btn.first.is_visible(timeout=1200):
                btn.first.click(timeout=1200)
                page.wait_for_timeout(400)
        except Exception:
            pass


def close_any_dialog(page: Page) -> None:
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass

    try:
        dialogs = page.locator("[role='dialog'], [aria-modal='true']")
        for i in range(dialogs.count()):
            dlg = dialogs.nth(i)
            if not dlg.is_visible(timeout=800):
                continue
            close_btn = first_matching(
                dlg,
                ["[aria-label*='close' i]", "[aria-label*='dismiss' i]", "button svg", "button"],
            )
            if close_btn is not None:
                try:
                    close_btn.first.click(timeout=1200)
                    page.wait_for_timeout(300)
                except Exception:
                    pass
    except Exception:
        pass


def robust_click(page: Page, locator: Locator, description: str, attempts: int = 4) -> None:
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            locator.first.click(timeout=4000)
            return
        except Exception as e:
            last_error = e
            log.info("Click on %s blocked (attempt %d/%d) — dismissing overlays...", description, attempt, attempts)
            dismiss_overlays(page)
            close_any_dialog(page)
            page.wait_for_timeout(500)

    try:
        locator.first.click(timeout=4000, force=True)
        return
    except Exception:
        raise last_error


def extract_link_href(locator: Locator) -> Optional[str]:
    try:
        el = locator.first
        href = el.get_attribute("href")
        if href and href.strip() not in ("", "#") and not href.startswith("javascript:"):
            return href.strip()
        anchor = el.locator("xpath=ancestor-or-self::a[1]")
        if anchor.count() > 0:
            href = anchor.first.get_attribute("href")
            if href and href.strip() not in ("", "#") and not href.startswith("javascript:"):
                return href.strip()
    except Exception:
        pass
    return None


def normalize_href(href: str) -> str:
    if href.startswith("/"):
        return urljoin(BASE_URL, href)
    return href


# --------------------------------------------------------------------------
# Resolve property URL
# --------------------------------------------------------------------------

MAX_PROPERTY_ATTEMPTS = 5


def collect_property_urls_from_results(page: Page, max_results: int = MAX_PROPERTY_ATTEMPTS) -> list[str]:
    """Collect property URLs from search results, preferring listings that show a review score."""
    cards = page.locator("[data-testid='property-card']")
    card_count = cards.count()
    if card_count == 0:
        return []

    with_scores: list[str] = []
    without_scores: list[str] = []
    seen: set[str] = set()

    for i in range(min(card_count, max_results * 3)):
        card = cards.nth(i)
        link = first_matching(
            card,
            [
                "a[data-testid='title-link']",
                "a[data-testid='property-card-desktop-single-image']",
                "a",
            ],
        )
        if link is None:
            continue
        href = extract_link_href(link)
        if not href:
            continue
        url = normalize_href(href)
        if url in seen:
            continue
        seen.add(url)

        has_score = card.locator("[data-testid='review-score']").count() > 0
        if has_score:
            with_scores.append(url)
        else:
            without_scores.append(url)

    ordered = with_scores + without_scores
    return ordered[:max_results]


def search_for_property_urls(page: Page, query: str) -> list[str]:
    log.info("Searching Booking.com for %r", query)
    page.goto(BASE_URL + "?lang=en-us", timeout=NAV_TIMEOUT_MS, wait_until="load")

    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except PWTimeoutError:
        pass

    dismiss_overlays(page)
    page.wait_for_timeout(500)
    dismiss_overlays(page)

    search_box = wait_first_matching(
        page,
        [
            "#ss",
            "input[name='ss']",
            "input[data-testid='destination-container'] input",
            "input[aria-label*='destination' i]",
            "input[aria-label*='Where' i]",
            "input[placeholder*='Where' i]",
            "input[type='search']",
        ],
        timeout=12000,
    )
    if search_box is None:
        debug_dump(page, "homepage")
        log.error("Could not find the destination search input on the homepage.")
        return None

    robust_click(page, search_box, "the destination search box")
    search_box.first.fill(query)
    page.wait_for_timeout(1000)

    suggestion = wait_first_matching(
        page,
        [
            "li[data-testid='autocomplete-result']",
            "ul[role='listbox'] li",
            "[data-testid='autocomplete-result']",
        ],
        timeout=4000,
    )
    if suggestion is not None:
        try:
            suggestion.first.click(timeout=SHORT_TIMEOUT_MS)
            page.wait_for_timeout(400)
        except Exception:
            pass

    search_button = wait_first_matching(
        page,
        [
            "button[type='submit']",
            "button:has-text('Search')",
            "[data-testid='searchbox-form-button-icon']",
        ],
        timeout=5000,
    )
    if search_button is not None:
        robust_click(page, search_button, "the search submit button")
    else:
        page.keyboard.press("Enter")

    try:
        page.wait_for_url(re.compile(r".*searchresults.*"), timeout=NAV_TIMEOUT_MS)
    except PWTimeoutError:
        debug_dump(page, "after_search_submit")
        log.error("Search did not navigate to a results page.")
        return None

    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except PWTimeoutError:
        pass
    dismiss_overlays(page)

    urls = collect_property_urls_from_results(page)
    if not urls:
        result_link = wait_first_matching(
            page,
            [
                "[data-testid='property-card'] a[data-testid='title-link']",
                "a[data-testid='title-link']",
                "[data-testid='web-core-property-card'] a[data-testid='title-link']",
                "[data-testid='web-core-property-card'] a",
                "[data-testid='web-core-stacked-card'] a",
                "[data-testid='property-card'] a",
                "a[data-testid='property-card-desktop-single-image']",
                "div[data-testid='property-card-container'] a",
            ],
            timeout=10000,
        )
        if result_link is None:
            debug_dump(page, "search_results")
            log.error("No search results found for %r.", query)
            return []

        href = extract_link_href(result_link)
        if not href:
            debug_dump(page, "search_results_no_href")
            log.error("Found a property card but could not extract its link href.")
            return []
        urls = [normalize_href(href)]

    log.info("Found %d candidate propert(ies) from search results.", len(urls))
    return urls


def resolve_property_urls(page: Page, keyword: str) -> list[str]:
    if is_booking_property_url(keyword):
        return [keyword.strip()]
    return search_for_property_urls(page, keyword.strip())


def resolve_property_url(page: Page, keyword: str) -> Optional[str]:
    urls = resolve_property_urls(page, keyword)
    return urls[0] if urls else None


def open_property_page(page: Page, property_url: str) -> bool:
    log.info("Opening property page: %s", property_url)
    page.goto(property_url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")

    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except PWTimeoutError:
        pass

    dismiss_overlays(page)

    if not is_property_page_url(page.url):
        debug_dump(page, "property_page")
        log.error("Did not land on a property page (url=%s).", page.url)
        return False

    return True


# --------------------------------------------------------------------------
# Reviews section + pagination
# --------------------------------------------------------------------------

def build_reviews_url(property_url: str) -> Optional[str]:
    match = re.search(r"booking\.com/hotel/([^/?#]+)/([^/?#.]+)", property_url, re.I)
    if not match:
        return None
    country, slug = match.group(1), match.group(2)
    return f"https://www.booking.com/reviews/{country}/hotel/{slug}.html?lang=en-us"


def property_reviews_tab_url(property_url: str) -> Optional[str]:
    base = property_url.split("#")[0]
    if "/hotel/" not in base.lower():
        return None
    return f"{base}#tab-reviews"


def reviews_are_loaded(page: Page, timeout: int = 3000) -> bool:
    return wait_first_matching(page, REVIEW_CARD_SELECTORS, timeout=timeout) is not None


def open_reviews_section(page: Page) -> bool:
    log.info("Opening guest reviews section...")

    if "/reviews/" in page.url.lower() and reviews_are_loaded(page, timeout=8000):
        return True

    tab_url = property_reviews_tab_url(page.url)
    if tab_url:
        log.info("Navigating to in-page reviews tab: %s", tab_url)
        try:
            page.goto(tab_url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            dismiss_overlays(page)
            if reviews_are_loaded(page, timeout=12000):
                return True
        except Exception as exc:
            log.warning("Reviews tab navigation failed (%s).", exc)

    trigger_selectors = [
        "[data-testid='fr-read-all-reviews']",
        "[data-testid='review-score-read-all-actionable']",
        "[data-testid='review-score-read-all']",
        "[data-testid='read-all-actionable']",
        "[data-testid='reviews-block-title']",
        "[data-testid='Property-Header-Nav-Tab-Trigger-reviews']",
    ]

    review_list_url = None
    anchor_trigger = None
    for sel in trigger_selectors:
        try:
            loc = page.locator(sel)
            if loc.count() == 0:
                continue
            href = extract_link_href(loc)
            if href:
                if href.startswith("#"):
                    anchor_trigger = loc
                    log.info("Found in-page reviews anchor via %s (%s)", sel, href)
                    break
                review_list_url = normalize_href(href)
                log.info("Found direct reviews link via %s", sel)
                break
        except Exception:
            continue

    if anchor_trigger is not None:
        try:
            anchor_trigger.first.scroll_into_view_if_needed(timeout=3000)
            robust_click(page, anchor_trigger, "reviews tab anchor", attempts=3)
            page.wait_for_timeout(2500)
            if reviews_are_loaded(page, timeout=8000):
                return True
        except Exception as exc:
            log.warning("Could not activate in-page reviews anchor (%s).", exc)

    if review_list_url:
        try:
            page.goto(review_list_url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PWTimeoutError:
                pass
            dismiss_overlays(page)
            if reviews_are_loaded(page, timeout=10000):
                return True
        except Exception as exc:
            log.warning("Direct navigation to reviews link failed (%s).", exc)

    for sel in trigger_selectors:
        try:
            trigger = page.locator(sel)
            if trigger.count() > 0 and trigger.first.is_visible(timeout=1500):
                trigger.first.scroll_into_view_if_needed(timeout=3000)
                robust_click(page, trigger, f"reviews trigger ({sel})", attempts=2)
                page.wait_for_timeout(1500)
                if reviews_are_loaded(page, timeout=5000):
                    return True
        except Exception:
            continue

    fallback_url = build_reviews_url(page.url)
    if fallback_url:
        log.info("Trying dedicated reviews page: %s", fallback_url)
        try:
            page.goto(fallback_url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PWTimeoutError:
                pass
            dismiss_overlays(page)
            if reviews_are_loaded(page, timeout=12000):
                return True
        except Exception as exc:
            log.warning("Fallback reviews page navigation failed: %s", exc)

    log.warning("Could not confirm review cards loaded — saving debug snapshot.")
    debug_dump(page, "reviews_section")
    return False


def go_to_next_page(page: Page) -> bool:
    next_button = first_matching(
        page,
        [
            "button[aria-label='Next page']",
            "button[aria-label*='Next' i]",
            "[data-testid='pagination-next-arrow']",
            "[data-testid='pagination-next']",
            "button:has-text('Load more')",
            "button:has-text('Show more')",
            "button:has-text('Next')",
            "a[aria-label*='Next' i]",
        ],
    )
    if next_button is None:
        return False

    try:
        btn = next_button.first
        if not btn.is_enabled(timeout=1500) or not btn.is_visible(timeout=1500):
            return False
        btn.scroll_into_view_if_needed(timeout=SHORT_TIMEOUT_MS)
        btn.click(timeout=SHORT_TIMEOUT_MS)
        return True
    except Exception:
        return False


def load_more_reviews(page: Page, previous_count: int) -> bool:
    clicked = go_to_next_page(page)
    if clicked:
        page.wait_for_timeout(1200)

    try:
        page.mouse.wheel(0, 2500)
    except Exception:
        pass
    try:
        card_loc = first_matching(page, REVIEW_CARD_SELECTORS)
        if card_loc is not None and card_loc.count() > 0:
            card_loc.nth(card_loc.count() - 1).scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    page.wait_for_timeout(1200)

    card_loc = first_matching(page, REVIEW_CARD_SELECTORS)
    new_count = card_loc.count() if card_loc is not None else 0
    if new_count > previous_count:
        return True
    return clicked


def parse_reviewer(name_text: Optional[str], country_text: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not name_text:
        return None, country_text
    parts = [p.strip() for p in name_text.split("\n") if p.strip()]
    parts = [
        p for p in parts
        if not re.match(r"^Active since \d{4}$", p, re.I)
        and not re.match(r"^\d+\s+reviews?$", p, re.I)
    ]
    if not parts:
        return None, country_text
    if len(parts) == 1:
        return parts[0], country_text
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[-2], parts[-1]


def clean_review_date(date_text: Optional[str]) -> Optional[str]:
    if not date_text:
        return None
    return re.sub(r"^Reviewed:\s*", "", date_text, flags=re.IGNORECASE).strip() or None

    t = tag_text.strip()
    if re.search(r"\d+\s*night", t, re.IGNORECASE):
        return "length_of_stay"
    if t.lower().startswith("stayed in") or re.search(r"\b(19|20)\d{2}\b", t):
        return "stay_date"
    if any(k.lower() in t.lower() for k in TRAVELER_TYPE_KEYWORDS):
        return "reviewer_type"
    return "room_type"


def classify_tag(tag_text: str) -> str:
    t = tag_text.strip()
    if re.search(r"\d+\s*night", t, re.IGNORECASE):
        return "length_of_stay"
    if t.lower().startswith("stayed in") or re.search(r"\b(19|20)\d{2}\b", t):
        return "stay_date"
    if any(k.lower() in t.lower() for k in TRAVELER_TYPE_KEYWORDS):
        return "reviewer_type"
    return "room_type"


def extract_review(card: Locator, property_name: str, property_url: str) -> dict:
    raw: Dict[str, Optional[str]] = {
        "property_name": property_name,
        "property_url": property_url,
    }

    title_loc = first_matching(card, ["[data-testid='review-title']", "h3", "h4"])
    raw["review_title"] = safe_text(title_loc)

    pos_loc = first_matching(card, ["[data-testid='review-positive-text']", ".review_pos"])
    neg_loc = first_matching(card, ["[data-testid='review-negative-text']", ".review_neg"])
    raw["positive_comment"] = safe_text(pos_loc)
    raw["negative_comment"] = safe_text(neg_loc)

    body_loc = first_matching(card, ["[data-testid='review-text']", ".review_item_review_content"])
    body_text = safe_text(body_loc)
    if body_text:
        raw["review_text"] = body_text
    else:
        combined = " | ".join(filter(None, [raw["positive_comment"], raw["negative_comment"]]))
        raw["review_text"] = combined or None

    rating_loc = first_matching(
        card,
        [
            "[aria-label*='Scored' i]",
            "[data-testid='review-score']",
            ".bui-review-score__badge",
            "[data-testid='rating-squares']",
        ],
    )
    rating_text = safe_text(rating_loc)
    if rating_text:
        match = re.search(r"(\d+(?:[.,]\d+)?)", rating_text)
        raw["overall_rating"] = match.group(1).replace(",", ".") if match else rating_text
    else:
        try:
            numeric_badge = card.get_by_text(re.compile(r"^\s*\d{1,2}(?:[.,]\d)?\s*$")).first
            if numeric_badge.count() > 0:
                txt = safe_text(numeric_badge)
                if txt:
                    raw["overall_rating"] = txt.replace(",", ".")
        except Exception:
            pass

    name_loc = first_matching(card, ["[data-testid='review-avatar'] div", ".bui-avatar-block__title"])
    reviewer_block = safe_text(name_loc)

    country_loc = first_matching(card, ["[data-testid='review-avatar'] span", ".bui-avatar-block__subtitle"])
    reviewer_country = safe_text(country_loc)

    author, country = parse_reviewer(reviewer_block, reviewer_country)
    raw["reviewer_name"] = author
    raw["reviewer_country"] = country

    date_loc = first_matching(card, ["[data-testid='review-date']", ".review_item_date"])
    raw["review_date"] = clean_review_date(safe_text(date_loc))
    if not raw["review_date"]:
        try:
            prefixed = card.get_by_text(re.compile(r"^Reviewed:", re.IGNORECASE)).first
            if prefixed.count() > 0:
                raw["review_date"] = clean_review_date(safe_text(prefixed))
        except Exception:
            pass

    helpful_loc = first_matching(card, ["[data-testid='review-helpful-vote']", "*:has-text('found this helpful')"])
    helpful_text = safe_text(helpful_loc)
    if helpful_text:
        match = re.search(r"(\d+)", helpful_text)
        raw["helpful_votes"] = match.group(1) if match else helpful_text

    tag_loc = first_matching(card, ["[data-testid='review-taglist']", ".review_item_info_tags"])
    extra_tags: Dict[str, Optional[str]] = {
        "reviewer_type": None,
        "room_type": None,
        "length_of_stay": None,
        "stay_date": None,
    }
    if tag_loc is not None:
        try:
            tag_items = tag_loc.first.locator("li, span")
            count = min(tag_items.count(), 10)
            for i in range(count):
                txt = safe_text(tag_items.nth(i))
                if not txt:
                    continue
                parts = re.split(r"\s*[·|]\s*", txt) if re.search(r"[·|]", txt) else [txt]
                for part in parts:
                    part = part.strip()
                    if not part:
                        continue
                    field = classify_tag(part)
                    if extra_tags.get(field) is None:
                        extra_tags[field] = part
        except Exception:
            pass

    raw.update(extra_tags)
    return normalize_review(raw)


def normalize_review(raw: dict) -> dict:
    rating = raw.get("overall_rating")
    try:
        rating = float(str(rating).replace(",", ".")) if rating else None
    except (TypeError, ValueError):
        rating = None

    review_id = None
    try:
        review_id = raw.get("review_id")
    except Exception:
        pass

    body = raw.get("review_text")
    title = raw.get("review_title")

    return {
        "review_id": review_id,
        "title": title,
        "body": body,
        "rating": rating,
        "date": clean_review_date(raw.get("review_date")),
        "author": raw.get("reviewer_name"),
        "platform": "booking",
        "country": raw.get("reviewer_country"),
        "positive": raw.get("positive_comment"),
        "negative": raw.get("negative_comment"),
        "traveler_type": raw.get("reviewer_type"),
        "room_type": raw.get("room_type"),
        "length_of_stay": raw.get("length_of_stay"),
        "stay_date": raw.get("stay_date"),
        "helpful_votes": raw.get("helpful_votes"),
        "property_name": raw.get("property_name"),
        "property_url": raw.get("property_url"),
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def get_property_name(page: Page) -> str:
    name_loc = first_matching(page, ["h2[data-testid='title']", "h2.pp-header__title", "h1"])
    return safe_text(name_loc) or "Unknown property"


def scrape_reviews(page: Page, property_name: str, property_url: str, limit: int, emit_fn: Callable[[dict], None]) -> int:
    log.info("Scraping up to %d reviews...", limit)
    count = 0
    seen_signatures = set()
    page_number = 1
    stagnant_attempts = 0
    total_attempts = 0

    while count < limit and total_attempts < 300:
        total_attempts += 1
        card_locator = first_matching(page, REVIEW_CARD_SELECTORS)
        if card_locator is None:
            log.info("No review cards found — stopping.")
            break

        card_count = card_locator.count()
        log.info("Batch %d: %d review card(s) in DOM (%d collected so far).", page_number, card_count, count)

        new_this_batch = 0
        for i in range(card_count):
            if count >= limit:
                break
            review = extract_review(card_locator.nth(i), property_name, property_url)
            sig = (review.get("author"), review.get("date"), (review.get("body") or "")[:30])
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)

            emit_fn(review)
            count += 1
            new_this_batch += 1

        if count >= limit:
            break

        advanced = load_more_reviews(page, card_count)
        if new_this_batch == 0 and not advanced:
            stagnant_attempts += 1
        else:
            stagnant_attempts = 0

        if stagnant_attempts >= 5:
            log.info("No new reviews after several attempts — stopping at %d.", count)
            debug_dump(page, "end_of_reviews")
            break

        page_number += 1
        time.sleep(0.6)

    return count


def scrape(keyword: str, limit: int, emit_fn: Callable[[dict], None]) -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        context.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
        context.set_default_timeout(NAV_TIMEOUT_MS)
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        resolver_page = context.new_page()
        property_urls = resolve_property_urls(resolver_page, keyword)
        if not property_urls:
            browser.close()
            return 0

        page = context.new_page()
        try:
            for attempt, property_url in enumerate(property_urls, start=1):
                log.info("Trying property %d/%d", attempt, len(property_urls))
                if not open_property_page(page, property_url):
                    continue

                property_name = get_property_name(page)
                log.info("Property found: %s", property_name)
                log.info("Property URL: %s", page.url)

                if not open_reviews_section(page):
                    log.warning("No reviews found for %r — trying next property.", property_name)
                    continue

                count = scrape_reviews(page, property_name, page.url, limit, emit_fn)
                if count > 0:
                    return count

                log.warning("Property had review cards but none could be extracted — trying next property.")

            log.error("Could not scrape reviews from any of the %d candidate properties.", len(property_urls))
            return 0
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description="Scrape Booking.com guest reviews.")
    parser.add_argument("keyword", help="Booking.com property URL or search keyword (e.g. 'Hilton London')")
    parser.add_argument("--limit", type=int, default=50, help="Max reviews to extract (default: 50)")
    parser.add_argument("--output", default=None, help="Optional file path to save JSON output")
    args = parser.parse_args()

    log.info("Starting Booking.com review scrape for %r (limit=%d)", args.keyword, args.limit)

    if args.output:
        collected: List[Dict[str, Any]] = []

        def emit(review: dict) -> None:
            collected.append(review)
            log.info("Collected review %d/%d", len(collected), args.limit)

        count = scrape(args.keyword, args.limit, emit)

        result = {
            "query": args.keyword,
            "platform": "booking",
            "count": count,
            "reviews": collected,
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        log.info("Wrote %d reviews to %s", count, args.output)
    else:
        def emit(review: dict) -> None:
            print(json.dumps(review, ensure_ascii=False))
            sys.stdout.flush()

        count = scrape(args.keyword, args.limit, emit)
        log.info("Done. %d review(s) streamed.", count)


if __name__ == "__main__":
    main()
