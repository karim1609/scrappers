"""
booking_reviews_explorer.py
============================
EXPLORATORY SCRIPT — NOT a production connector.

Purpose
-------
Explore what review data Booking.com exposes publicly on a property page,
in order to design a normalized data model for a social listening platform.

What it does
------------
1. Opens Booking.com and searches for a hotel/business name.
2. Opens the first matching property page.
3. Opens the "Guest reviews" section on that page.
4. Scrapes as many reviews as possible (default target: 50), following
   pagination / "Load more" automatically.
5. Prints every review to the console as it is scraped.
6. Saves everything to a CSV file for later analysis.

Important notes
----------------
- Booking.com's DOM uses obfuscated/hashed CSS classes that change often
  and the site frequently A/B tests different layouts. This script tries
  several selector strategies per field ("selector cascades") and degrades
  gracefully (field = None) rather than crashing when a field is missing.
- Booking.com's Terms of Use restrict automated scraping. This script is
  meant for one-off, small-scale, personal data-model exploration only —
  not for scheduled/production collection. For production use, look into
  Booking.com's official Content/Partner APIs instead.
- Run once with `headless=False` first to visually confirm the flow still
  matches the current site layout before trusting the output.

Usage
-----
    pip install playwright
    playwright install chromium
    python booking_reviews_explorer.py --hotel "Hilton London" --max-reviews 50
"""

import argparse
import csv
import re
import sys
import time
from datetime import datetime
from typing import Optional

from playwright.sync_api import (
    sync_playwright,
    Page,
    Locator,
    TimeoutError as PWTimeoutError,
)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

BASE_URL = "https://www.booking.com/"
DEFAULT_MAX_REVIEWS = 50
NAV_TIMEOUT_MS = 20_000
SHORT_TIMEOUT_MS = 5_000
OUTPUT_CSV = "booking_reviews.csv"

CSV_FIELDS = [
    "property_name",
    "property_url",
    "review_title",
    "review_text",
    "overall_rating",
    "positive_comment",
    "negative_comment",
    "reviewer_name",
    "reviewer_country",
    "reviewer_type",
    "room_type",
    "length_of_stay",
    "stay_date",
    "review_date",
    "helpful_votes",
    "raw_tags",          # any leftover metadata tags we could not classify
    "scraped_at",
]

# Candidate selectors for a single review card. Kept as one shared list so
# open_reviews_section() and scrape_reviews() always agree on what counts
# as "a review card" — update this ONE list if Booking's markup changes.
REVIEW_CARD_SELECTORS = [
    "[data-testid='review-card']",
    "div[data-review-id]",
    "li.review_item",
    "[data-testid='PropertyReviewsRegionBlock'] li",
    "[data-testid='PropertyReviewsRegionBlock'] article",
    "[data-testid*='review-card' i]",
]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def safe_text(locator: Optional[Locator], timeout: int = SHORT_TIMEOUT_MS) -> Optional[str]:
    """Return the trimmed inner text of a locator, or None if it doesn't exist."""
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
    """
    Try a list of candidate CSS/text selectors against `scope` (a Page or
    Locator) and return the first one that actually matches something,
    right now, with no waiting. Good for scanning already-loaded content
    (e.g. inside a review card). For elements that might still be loading,
    use wait_first_matching() instead.
    """
    for sel in selectors:
        try:
            loc = scope.locator(sel)
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def wait_first_matching(page: Page, selectors: list, timeout: int = 8000) -> Optional[Locator]:
    """
    Like first_matching(), but actively WAITS (up to `timeout` ms total,
    split across candidates) for each selector to appear before giving up.
    Use this for anything that depends on page JS having finished rendering
    (search box, results, review cards on first load) — count()-only checks
    are unreliable because they don't wait for the DOM to settle.
    """
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
    """
    Save a screenshot + full HTML of the current page, AND automatically
    extract every distinct data-testid present into the console + a text
    file. This is the equivalent of running:
        grep -oE 'data-testid="[^"]*"' debug_X.html | sort -u
    but built-in, so whenever selectors fail we immediately get a list of
    real, current attribute names to fix them with — no manual grepping.
    """
    try:
        screenshot_path = f"debug_{name}.png"
        html_path = f"debug_{name}.html"
        testids_path = f"debug_{name}_testids.txt"

        page.screenshot(path=screenshot_path, full_page=True)
        html = page.content()
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)

        testids = sorted(set(re.findall(r'data-testid="([^"]*)"', html)))
        with open(testids_path, "w", encoding="utf-8") as f:
            f.write("\n".join(testids))

        print(f"  [debug] Saved '{screenshot_path}', '{html_path}' and '{testids_path}'.")
        print(f"  [debug] {len(testids)} distinct data-testid values found on this page:")
        for t in testids:
            print(f"    - {t}")
    except Exception as e:
        print(f"  [debug] Could not save debug dump: {e}")


def dismiss_overlays(page: Page) -> None:
    """Close cookie banners / sign-in promo popups that block interaction."""
    candidates = [
        "#onetrust-accept-btn-handler",           # cookie consent (OneTrust)
        "button[aria-label='Dismiss sign-in info.']",
        "button[aria-label='Close']",
        "[data-testid='cross-icon']",              # generic modal close (X)
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
            # Overlay not present — that's fine, just move on.
            pass


def close_any_dialog(page: Page) -> None:
    """
    Language-independent fallback for closing whatever overlay/modal is
    currently blocking interaction. Booking.com's consent/interstitial
    dialogs change wording per locale (English 'Accept', Arabic 'موافق',
    etc.) and use hashed CSS classes we can't hardcode reliably — so
    instead of matching text, we:
      1. Press Escape (closes most modal dialogs regardless of language).
      2. Look for anything with role='dialog' and try clicking a close
         control inside it (icon-only close buttons are usually flagged
         via aria-label='close'/'Close' even when the visible text is
         localized, or are simply the only <button> present).
    """
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass

    try:
        dialogs = page.locator("[role='dialog'], [aria-modal='true']")
        dcount = dialogs.count()
        for i in range(dcount):
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
    """
    Click a locator, retrying through overlay-dismissal in between attempts.
    Falls back to a force-click (bypasses Playwright's actionability/
    interception checks) on the final attempt rather than hanging for the
    full default timeout on a blocked element.
    """
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            locator.first.click(timeout=4000)
            return
        except Exception as e:
            last_error = e
            print(f"  Click on {description} blocked (attempt {attempt}/{attempts}) — "
                  f"trying to dismiss overlays and retry...")
            dismiss_overlays(page)
            close_any_dialog(page)
            page.wait_for_timeout(500)

    # Last resort: force the click even if something is technically on top
    # of the element (works for cosmetic overlays that don't actually
    # capture the click once JS-side).
    try:
        locator.first.click(timeout=4000, force=True)
        return
    except Exception:
        raise last_error


# --------------------------------------------------------------------------
# Step 1-2: search + open property page
# --------------------------------------------------------------------------

def search_and_open_property(page: Page, query: str) -> str:
    """
    Search Booking.com for `query`, click the first result, and return the
    URL of the opened property page. Booking.com usually opens the property
    in a NEW TAB, so we listen for that via context.expect_page().
    """
    print(f"[1/6] Opening Booking.com and searching for '{query}'...")
    page.goto(BASE_URL + "?lang=en-us", timeout=NAV_TIMEOUT_MS, wait_until="load")

    # Let the page settle (Booking's homepage is JS-heavy). Ignore timeout —
    # some background requests never go fully idle, that's normal.
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except PWTimeoutError:
        pass

    # Dismiss cookie / consent / sign-in overlays. Try twice: sometimes a
    # second overlay (e.g. "Sign in, save more") appears right after the
    # first one closes.
    dismiss_overlays(page)
    page.wait_for_timeout(500)
    dismiss_overlays(page)

    # The main destination search box. Booking has used id="ss" for a long
    # time, but we cascade through several alternatives and ACTIVELY WAIT
    # for one to appear rather than checking only once.
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
        # We couldn't find it — dump evidence instead of failing blind.
        debug_dump(page, "homepage")
        raise RuntimeError(
            "Could not find the destination search input on the homepage. "
            "Check debug_homepage.png / debug_homepage.html to see what "
            "Booking.com actually served (cookie wall? bot-check? different "
            "country/language layout?)."
        )

    robust_click(page, search_box, "the destination search box")
    search_box.first.fill(query)
    page.wait_for_timeout(1000)  # let the autocomplete dropdown render

    # Try to pick the first autocomplete suggestion if one appears. This
    # matters: Booking generally expects a destination to be SELECTED from
    # the dropdown (it carries an internal destination id), not just typed
    # as free text.
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

    # IMPORTANT: pressing Enter in the destination field on Booking.com only
    # confirms/closes the dropdown — it does NOT submit the search. We must
    # explicitly click the search button, then wait for real navigation to
    # a searchresults.html URL before assuming we're on the results page.
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
        # Last resort fallback.
        page.keyboard.press("Enter")

    # Confirm we actually navigated to a results page. If the URL never
    # changes to something containing "searchresults", the destination was
    # probably never properly selected from the dropdown.
    try:
        page.wait_for_url(re.compile(r".*searchresults.*"), timeout=NAV_TIMEOUT_MS)
    except PWTimeoutError:
        debug_dump(page, "after_search_submit")
        raise RuntimeError(
            "Search did not navigate to a results page (URL still doesn't "
            "contain 'searchresults'). This usually means the destination "
            "wasn't properly selected from the autocomplete dropdown. Check "
            "debug_after_search_submit.png / .html."
        )

    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except PWTimeoutError:
        pass
    dismiss_overlays(page)

    print("[2/6] Opening the first matching property page...")
    result_link = wait_first_matching(
        page,
        [
            "[data-testid='web-core-property-card'] a",
            "[data-testid='web-core-stacked-card'] a",
            "[data-testid='property-card'] a[data-testid='title-link']",
            "[data-testid='property-card'] a",
            "a[data-testid='property-card-desktop-single-image']",
            "div[data-testid='property-card-container'] a",
        ],
        timeout=10000,
    )
    if result_link is None:
        debug_dump(page, "search_results")
        raise RuntimeError(
            "No search results found for that query. Check "
            "debug_search_results.png / .html to see what the results page "
            "actually looked like."
        )

    # Clear any lingering overlay before clicking, same reasoning as the
    # destination search box above.
    dismiss_overlays(page)
    close_any_dialog(page)

    # Booking.com property links typically open in a new tab (target=_blank).
    try:
        with page.context.expect_page(timeout=NAV_TIMEOUT_MS) as new_page_info:
            try:
                result_link.first.click(timeout=SHORT_TIMEOUT_MS)
            except Exception:
                result_link.first.click(timeout=SHORT_TIMEOUT_MS, force=True)
        property_page = new_page_info.value
    except PWTimeoutError:
        # Fallback: maybe it navigated in the SAME tab instead of a new one.
        try:
            result_link.first.click(timeout=SHORT_TIMEOUT_MS)
        except Exception:
            result_link.first.click(timeout=SHORT_TIMEOUT_MS, force=True)
        property_page = page

    property_page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
    dismiss_overlays(property_page)

    return property_page.url


# --------------------------------------------------------------------------
# Step 3: open the reviews section on the property page
# --------------------------------------------------------------------------

def open_reviews_section(page: Page) -> None:
    """
    Navigate into the full guest-reviews list.

    Booking.com's property page only shows a couple of "featured" teaser
    reviews inline. Clicking the various "read all reviews" controls on
    this heavy React SPA turned out to be unreliable (overlays intercept
    the click, or the click fires but nothing actually re-renders). Many
    of these controls are plain <a href="..."> links pointing straight at
    Booking's dedicated, fully server-rendered review-list page — so
    instead of fighting the SPA, we grab that href and navigate to it
    directly with page.goto(), which sidesteps click/overlay issues
    entirely. We only fall back to clicking if no usable href is found.
    """
    print("[3/6] Opening the guest reviews section...")

    trigger_selectors = [
        "[data-testid='fr-read-all-reviews']",
        "[data-testid='review-score-read-all-actionable']",
        "[data-testid='review-score-read-all']",
        "[data-testid='read-all-actionable']",
        "[data-testid='reviews-block-title']",
        "[data-testid='Property-Header-Nav-Tab-Trigger-reviews']",
    ]

    # --- Strategy 1: find a real href and navigate straight to it. ---
    review_list_url = None
    for sel in trigger_selectors:
        try:
            loc = page.locator(sel)
            if loc.count() == 0:
                continue
            el = loc.first
            # The data-testid element might be a <span>/<div> wrapping the
            # actual <a>; check both the element itself and its closest
            # anchor ancestor.
            href = el.get_attribute("href")
            if not href:
                anchor = el.locator("xpath=ancestor-or-self::a[1]")
                if anchor.count() > 0:
                    href = anchor.first.get_attribute("href")
            if href and href.strip() not in ("", "#") and not href.startswith("javascript:"):
                review_list_url = href
                print(f"  Found a direct reviews link via {sel} — navigating there.")
                break
        except Exception:
            continue

    if review_list_url:
        try:
            if review_list_url.startswith("/"):
                from urllib.parse import urljoin
                review_list_url = urljoin(page.url, review_list_url)
            page.goto(review_list_url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PWTimeoutError:
                pass
            dismiss_overlays(page)
        except Exception as e:
            print(f"  Direct navigation to reviews link failed ({e}) — falling back to clicking.")
            review_list_url = None

    # --- Strategy 2 (fallback): click through the triggers as before. ---
    if not review_list_url:
        print("  No direct reviews link found on any trigger — falling back to clicking.")
        for sel in trigger_selectors:
            try:
                trigger = page.locator(sel)
                if trigger.count() > 0 and trigger.first.is_visible(timeout=1500):
                    trigger.first.scroll_into_view_if_needed(timeout=3000)
                    robust_click(page, trigger, f"reviews trigger ({sel})", attempts=2)
                    page.wait_for_timeout(800)
            except Exception:
                continue
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeoutError:
            pass

    # Wait for at least one review card to show up. We cast a wide net of
    # guesses since we don't yet know Booking's exact current markup for
    # an individual review card in this view.
    card_loc = wait_first_matching(page, REVIEW_CARD_SELECTORS, timeout=10000)
    if card_loc is None:
        print("  Warning: could not confirm individual review cards loaded — "
              "dumping full diagnostics (including the live list of "
              "data-testid values) so we can pinpoint the correct selector.")
        debug_dump(page, "reviews_section")


# --------------------------------------------------------------------------
# Step 4: pagination / load-more handling
# --------------------------------------------------------------------------

def go_to_next_page(page: Page) -> bool:
    """
    Try to advance via an explicit 'Next page' / 'Load more' BUTTON.
    Returns True if a click was performed, False if no such button exists.
    (This does not by itself guarantee new content loaded — the caller
    checks the review-card count before/after.)
    """
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


def load_more_reviews(page: Page, review_card_selectors: list, previous_count: int) -> bool:
    """
    Try to reveal additional reviews using two complementary strategies,
    since Booking.com sometimes uses real pagination (a 'Next'/'Load more'
    button) and sometimes uses infinite scroll instead (or a mix):

      1. Click an explicit pagination / load-more button, if one exists.
      2. Otherwise (or in addition), scroll the page / the last review card
         into view to trigger lazy-loading of further reviews.

    Returns True if the number of review cards present in the DOM grew,
    which is our real signal that more content actually arrived.
    """
    clicked = go_to_next_page(page)
    if clicked:
        page.wait_for_timeout(1200)

    # Whether or not a button was clicked, also try scrolling — cheap, and
    # covers infinite-scroll layouts where no button exists at all.
    try:
        page.mouse.wheel(0, 2500)
    except Exception:
        pass
    try:
        card_loc = first_matching(page, review_card_selectors)
        if card_loc is not None and card_loc.count() > 0:
            card_loc.nth(card_loc.count() - 1).scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    page.wait_for_timeout(1200)

    card_loc = first_matching(page, review_card_selectors)
    new_count = card_loc.count() if card_loc is not None else 0

    if new_count > previous_count:
        return True
    # Some real-pagination flows REPLACE the set of cards instead of
    # appending to it (count stays the same or even drops), so a button
    # click alone still counts as "advanced" — the caller will re-scan.
    return clicked


# --------------------------------------------------------------------------
# Step 5: per-review field extraction
# --------------------------------------------------------------------------

TRAVELER_TYPE_KEYWORDS = [
    "Solo traveler", "Couple", "Family", "Group", "Business traveler",
    "Traveled with friends", "Traveled with pets",
]


def classify_tag(tag_text: str) -> str:
    """
    Booking.com groups extra review metadata (traveler type, room type,
    length of stay, stay date) into a loose list of small 'tag' strings.
    We heuristically classify each tag into a field.
    """
    t = tag_text.strip()
    if re.search(r"\d+\s*night", t, re.IGNORECASE):
        return "length_of_stay"
    if t.lower().startswith("stayed in") or re.search(r"\b(19|20)\d{2}\b", t):
        return "stay_date"
    if any(k.lower() in t.lower() for k in TRAVELER_TYPE_KEYWORDS):
        return "reviewer_type"
    return "room_type"  # fallback bucket: usually room type descriptions


def extract_review(card: Locator, property_name: str, property_url: str) -> dict:
    """Extract every available field from a single review card, tolerating missing fields."""
    data = {field: None for field in CSV_FIELDS}
    data["property_name"] = property_name
    data["property_url"] = property_url
    data["scraped_at"] = datetime.utcnow().isoformat()

    # --- Review title ---
    title_loc = first_matching(card, ["[data-testid='review-title']", "h3", "h4"])
    data["review_title"] = safe_text(title_loc)

    # --- Positive / negative comments ---
    pos_loc = first_matching(card, ["[data-testid='review-positive-text']", ".review_pos"])
    neg_loc = first_matching(card, ["[data-testid='review-negative-text']", ".review_neg"])
    data["positive_comment"] = safe_text(pos_loc)
    data["negative_comment"] = safe_text(neg_loc)

    # --- Combined review text (fallback if site doesn't split pos/neg) ---
    body_loc = first_matching(card, ["[data-testid='review-text']", ".review_item_review_content"])
    body_text = safe_text(body_loc)
    if body_text:
        data["review_text"] = body_text
    else:
        # Fall back to concatenating positive + negative if that's all we have.
        combined = " | ".join(filter(None, [data["positive_comment"], data["negative_comment"]]))
        data["review_text"] = combined or None

    # --- Overall rating (the small colored badge, e.g. "10", "8.5") ---
    # Booking's hashed CSS classes change often, so we cascade through
    # several strategies, from most to least specific:
    #   1. aria-label based (accessibility labels tend to be the most stable)
    #   2. known data-testid / class names
    #   3. last resort: find a short standalone number (e.g. "10", "8.5")
    #      near the top of the card — this is what the visible score badge
    #      looks like when nothing else matches.
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
        data["overall_rating"] = match.group(1).replace(",", ".") if match else rating_text
    else:
        # Fallback: look for a small isolated number (score badges show just
        # "10", "9.2", etc. with no surrounding text) anywhere in the card.
        try:
            numeric_badge = card.get_by_text(re.compile(r"^\s*\d{1,2}(?:[.,]\d)?\s*$")).first
            if numeric_badge.count() > 0:
                txt = safe_text(numeric_badge)
                if txt:
                    data["overall_rating"] = txt.replace(",", ".")
        except Exception:
            pass

    # --- Reviewer name ---
    name_loc = first_matching(card, ["[data-testid='review-avatar'] div", ".bui-avatar-block__title"])
    data["reviewer_name"] = safe_text(name_loc)

    # --- Reviewer country ---
    country_loc = first_matching(card, ["[data-testid='review-avatar'] span", ".bui-avatar-block__subtitle"])
    data["reviewer_country"] = safe_text(country_loc)

    # --- Publication date ---
    date_loc = first_matching(card, ["[data-testid='review-date']", ".review_item_date"])
    data["review_date"] = safe_text(date_loc)
    if not data["review_date"]:
        # Fallback: Booking often prefixes this with "Reviewed:" in the UI.
        try:
            prefixed = card.get_by_text(re.compile(r"^Reviewed:", re.IGNORECASE)).first
            if prefixed.count() > 0:
                txt = safe_text(prefixed)
                if txt:
                    data["review_date"] = re.sub(r"^Reviewed:\s*", "", txt, flags=re.IGNORECASE)
        except Exception:
            pass

    # --- Helpful votes ---
    helpful_loc = first_matching(card, ["[data-testid='review-helpful-vote']", "*:has-text('found this helpful')"])
    helpful_text = safe_text(helpful_loc)
    if helpful_text:
        match = re.search(r"(\d+)", helpful_text)
        data["helpful_votes"] = match.group(1) if match else helpful_text

    # --- Tag list: reviewer_type / room_type / length_of_stay / stay_date ---
    tag_loc = first_matching(card, ["[data-testid='review-taglist']", ".review_item_info_tags"])
    leftover_tags = []
    if tag_loc is not None:
        try:
            tag_items = tag_loc.first.locator("li, span")
            count = min(tag_items.count(), 10)  # safety cap
            for i in range(count):
                txt = safe_text(tag_items.nth(i))
                if not txt:
                    continue
                # Booking sometimes combines two facts in one tag, e.g.
                # "1 night · July 2026" (length_of_stay + stay_date) joined
                # by a middle-dot. Split on common separators and classify
                # each part independently so neither gets lost.
                parts = re.split(r"\s*[·|]\s*", txt) if re.search(r"[·|]", txt) else [txt]
                for part in parts:
                    part = part.strip()
                    if not part:
                        continue
                    field = classify_tag(part)
                    if data.get(field) is None:
                        data[field] = part
                    else:
                        leftover_tags.append(part)
        except Exception:
            pass
    data["raw_tags"] = "; ".join(leftover_tags) if leftover_tags else None

    return data


# --------------------------------------------------------------------------
# Step 6: orchestrate scraping + pagination
# --------------------------------------------------------------------------

def scrape_reviews(page: Page, property_name: str, property_url: str, max_reviews: int) -> list:
    """Scrape review cards from the current reviews view, loading more as needed."""
    print(f"[4/6] Scraping up to {max_reviews} reviews (this may take a bit)...")
    collected = []
    seen_signatures = set()  # dedupe guard, since scrolling/pagination can overlap
    page_number = 1
    stagnant_attempts = 0
    MAX_STAGNANT_ATTEMPTS = 5   # stop if several attempts in a row load nothing new
    MAX_TOTAL_ATTEMPTS = 300    # hard safety cap against runaway loops

    review_card_selectors = REVIEW_CARD_SELECTORS

    total_attempts = 0
    while len(collected) < max_reviews and total_attempts < MAX_TOTAL_ATTEMPTS:
        total_attempts += 1
        card_locator = first_matching(page, review_card_selectors)
        if card_locator is None:
            print("  No review cards found on this page — stopping.")
            break

        count = card_locator.count()
        print(f"  Batch {page_number}: {count} review card(s) currently in the DOM "
              f"({len(collected)} collected so far).")

        new_this_batch = 0
        for i in range(count):
            if len(collected) >= max_reviews:
                break
            card = card_locator.nth(i)
            review = extract_review(card, property_name, property_url)

            # Simple de-dup signature (reviewer + date + first 30 chars of text)
            sig = (review["reviewer_name"], review["review_date"], (review["review_text"] or "")[:30])
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)

            collected.append(review)
            new_this_batch += 1
            print_review(review, len(collected))

        if len(collected) >= max_reviews:
            break

        # Try to reveal more reviews (button click and/or scroll).
        previous_count = count
        advanced = load_more_reviews(page, review_card_selectors, previous_count)

        if new_this_batch == 0 and not advanced:
            stagnant_attempts += 1
        else:
            stagnant_attempts = 0

        if stagnant_attempts >= MAX_STAGNANT_ATTEMPTS:
            print(f"  No new reviews after {MAX_STAGNANT_ATTEMPTS} attempts — "
                  f"this is likely all Booking.com exposes for this property "
                  f"({len(collected)} reviews collected).")
            debug_dump(page, "end_of_reviews")
            break

        page_number += 1
        time.sleep(0.6)  # be polite, avoid hammering the site

    if total_attempts >= MAX_TOTAL_ATTEMPTS:
        print(f"  Reached the safety cap of {MAX_TOTAL_ATTEMPTS} load attempts — stopping.")

    return collected


# --------------------------------------------------------------------------
# Console output + CSV persistence
# --------------------------------------------------------------------------

def print_review(review: dict, index: int) -> None:
    """Requirement #5: print every review to the console as it's scraped."""
    print("\n" + "-" * 70)
    print(f"Review #{index}")
    for field in CSV_FIELDS:
        value = review.get(field)
        print(f"  {field:18s}: {value if value else '(missing)'}")
    print("-" * 70)


def save_to_csv(reviews: list, filename: str) -> None:
    print(f"\n[6/6] Saving {len(reviews)} reviews to '{filename}'...")
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for review in reviews:
            writer.writerow(review)
    print("Done.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def get_property_name(page: Page) -> str:
    name_loc = first_matching(page, ["h2[data-testid='title']", "h2.pp-header__title", "h1"])
    return safe_text(name_loc) or "Unknown property"


def main():
    parser = argparse.ArgumentParser(description="Explore Booking.com review data (data-model discovery only).")
    parser.add_argument("--hotel", default="Hilton London", help="Hotel / property name to search for.")
    parser.add_argument("--max-reviews", type=int, default=DEFAULT_MAX_REVIEWS, help="Target number of reviews to collect.")
    parser.add_argument("--show-browser", action="store_true", default=False,
                         help="Show the browser window (default: run headless / in the background).")
    parser.add_argument("--output", default=OUTPUT_CSV, help="Output CSV filename.")
    args = parser.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.show_browser, slow_mo=0 if not args.show_browser else 50)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        context.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
        context.set_default_timeout(NAV_TIMEOUT_MS)
        page = context.new_page()

        try:
            property_url = search_and_open_property(page, args.hotel)
            property_page = next(pg for pg in context.pages if pg.url == property_url)
        except StopIteration:
            # Fallback: just use whichever page is currently active/newest.
            property_page = context.pages[-1]
        except Exception as e:
            print(f"FATAL: could not search / open property page: {e}", file=sys.stderr)
            browser.close()
            sys.exit(1)

        try:
            property_name = get_property_name(property_page)
            print(f"    Property found: {property_name}")
            print(f"    Property URL:   {property_page.url}")

            open_reviews_section(property_page)
            reviews = scrape_reviews(property_page, property_name, property_page.url, args.max_reviews)

            if not reviews:
                print("No reviews could be extracted. The page layout may have changed — "
                      "try re-running with --headless off to inspect visually.")
            else:
                save_to_csv(reviews, args.output)
                print(f"\nSummary: {len(reviews)} reviews collected for '{property_name}'.")

        except Exception as e:
            print(f"ERROR during scraping: {e}", file=sys.stderr)
        finally:
            browser.close()


if __name__ == "__main__":
    main()