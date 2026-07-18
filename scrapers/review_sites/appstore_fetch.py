#!/usr/bin/env python3
"""
appstore_fetch.py

Production App Store review scraper.

Pipeline: resolve_app() -> fetch_reviews() -> normalize_review() -> AppStoreScraper

Design notes (see class/function docstrings for detail):
  - App resolution combines fuzzy text matching (RapidFuzz), Apple's own
    Search API result ranking, and review-count popularity, so a
    typo-squatting clone app can't outscore the real app just by having a
    name closer to a misspelled query. Falls back across multiple
    storefronts (countries) until a confident match is found, and fails
    loudly rather than guessing if nothing clears the confidence bar.
  - Review collection is a strategy chain, not a single endpoint: Apple's
    public RSS customer-reviews feed is tried first (fast, no auth), and
    if it comes back empty after retries, an AMP-API fallback (Apple's
    internal web API, used by apps.apple.com itself) is tried next. Both
    are undocumented/unsupported by Apple and can change without notice --
    that's a structural limitation of any free approach here, not
    something retries alone can fix.
  - Every HTTP call goes through one retry-with-backoff helper.

Usage:
    python appstore_fetch.py "Nike" --limit 50
    python appstore_fetch.py "Nike" --limit 100 --country gb --output reviews.json
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import math
import re
import sys
import time
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote

import requests
from rapidfuzz import fuzz

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("appstore_scraper")


# =============================================================================
# Configuration -- tune behavior by editing this section only. No magic
# numbers should appear anywhere else in this file.
# =============================================================================

# --- iTunes Search API -------------------------------------------------

SEARCH_URL = "https://itunes.apple.com/search"
SEARCH_RESULT_LIMIT = 10  # candidates fetched per country before ranking

# Countries tried in order until a confident app match is found.
FALLBACK_COUNTRIES: tuple[str, ...] = ("us", "gb", "fr", "ca", "de", "jp", "au")

# Minimum weighted confidence (0-100) required to accept a candidate as
# "the" app. Below this, resolution fails loudly rather than guessing.
MIN_MATCH_CONFIDENCE = 55.0

# Weights for combining fuzzy-match scores across app fields. These three
# combine into the "text_score" signal below (must sum to 1.0).
FIELD_WEIGHTS = {
    "trackName": 0.6,
    "sellerName": 0.25,
    "bundleId": 0.15,
}

# Weights combining the three independent confidence signals:
#   text_score        - fuzzy similarity to the query (gameable by
#                        keyword-stuffed clone apps on its own)
#   rank_score         - Apple's own result ordering (their relevance
#                        algorithm already applies typo-correction)
#   popularity_score   - log-scaled review count (anti-typosquatting:
#                        clones rarely have many ratings)
# Must sum to 1.0.
SIGNAL_WEIGHTS = {
    "text_score": 0.5,
    "rank_score": 0.25,
    "popularity_score": 0.25,
}

# userRatingCount above this is treated as "maximally popular" (100 score).
POPULARITY_SATURATION_RATING_COUNT = 1_000_000

# --- Reviews: RSS feed ---------------------------------------------------

# NOTE: segment order matters to Apple's router. `page` and `id` must
# precede the sort segment, and `sortby` must be lowercase, or the feed
# silently returns an empty (but HTTP 200) result for every app.
RSS_REVIEWS_URL_TEMPLATE = (
    "https://itunes.apple.com/{country}/rss/customerreviews/"
    "page={page}/id={app_id}/sortby=mostrecent/xml"
)
RSS_MAX_PAGES = 10          # Apple's hard limit on this endpoint
RSS_REVIEWS_PER_PAGE = 50   # Apple's fixed page size -> 500 review hard cap

# --- Reviews: AMP (internal web) API fallback -----------------------------

AMP_APP_PAGE_URL_TEMPLATE = "https://apps.apple.com/{country}/app/id{app_id}"
AMP_REVIEWS_URL_TEMPLATE = (
    "https://amp-api.apps.apple.com/v1/catalog/{country_upper}/apps/{app_id}/reviews"
)
AMP_REVIEWS_PAGE_SIZE = 20   # observed max page size for this endpoint
AMP_MAX_OFFSET = 500         # practical cap; unauthenticated access isn't guaranteed beyond this

# --- HTTP / retry policy --------------------------------------------------

REQUEST_TIMEOUT_SECS = 15
POLITE_DELAY_SECS = 1.0

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
MAX_RETRIES_PER_REQUEST = 4
RETRY_BACKOFF_BASE_SECS = 2.0
RETRY_BACKOFF_MAX_SECS = 30.0

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

PLATFORM_NAME = "appstore"


# =============================================================================
# Exceptions
# =============================================================================

class AppStoreScraperError(Exception):
    """Base class for all errors raised by this scraper."""


class AppResolutionError(AppStoreScraperError):
    """No app could be confidently resolved from a keyword. Raised instead
    of silently picking a low-confidence candidate."""


class ReviewFetchError(AppStoreScraperError):
    """Every review-fetching strategy has been exhausted with no results."""


class UpstreamRequestError(AppStoreScraperError):
    """A single HTTP request to an upstream Apple endpoint failed. Caught
    internally by fetch strategies, which then retry or move on."""


# =============================================================================
# Data containers
# =============================================================================

@dataclass(frozen=True)
class AppCandidate:
    """A resolved App Store app, ready to be used for review fetching."""

    app_id: int
    track_name: str
    seller_name: str
    bundle_id: str
    country: str
    confidence: float  # 0-100 weighted score
    raw: dict[str, Any] = field(repr=False)  # original iTunes Search API record


@dataclass(frozen=True)
class RawReview:
    """A single review in a source-agnostic shape, before normalization.
    Every fetch strategy must produce this same shape."""

    review_id: Optional[str]
    title: Optional[str]
    body: Optional[str]
    rating: Optional[float]
    date: Optional[str]
    author: Optional[str]
    app_version: Optional[str]
    source: str  # which strategy produced this, e.g. "rss" or "amp_api"


# =============================================================================
# HTTP helper: one retry-with-backoff implementation, used everywhere
# =============================================================================

def request_with_retries(
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    max_retries: int = MAX_RETRIES_PER_REQUEST,
) -> requests.Response:
    """Perform an HTTP request, retrying on transient failures.

    Retries on connection errors, timeouts, and status codes in
    RETRYABLE_STATUS_CODES, using capped exponential backoff. Raises
    UpstreamRequestError once every attempt is exhausted.

    Does NOT retry on a 200 with an empty/unexpected body -- "empty" can
    legitimately mean "no more reviews," which is a content-level decision
    the caller must make.
    """
    last_error: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            response = requests.request(
                method,
                url,
                headers=headers or {"User-Agent": USER_AGENT},
                params=params,
                timeout=REQUEST_TIMEOUT_SECS,
            )
        except requests.RequestException as exc:
            last_error = exc
            log.warning("Request error on attempt %d/%d for %s: %s", attempt + 1, max_retries + 1, url, exc)
        else:
            if response.status_code not in RETRYABLE_STATUS_CODES:
                return response
            last_error = UpstreamRequestError(f"HTTP {response.status_code} from {url}")
            log.warning(
                "Retryable HTTP %d on attempt %d/%d for %s",
                response.status_code, attempt + 1, max_retries + 1, url,
            )

        if attempt < max_retries:
            backoff = min(RETRY_BACKOFF_BASE_SECS * (2 ** attempt), RETRY_BACKOFF_MAX_SECS)
            time.sleep(backoff)

    raise UpstreamRequestError(f"Exhausted {max_retries + 1} attempts for {url}: {last_error}")


# =============================================================================
# App resolution: iTunes Search API + weighted fuzzy/rank/popularity scoring
# =============================================================================

def _field_score(keyword: str, value: Optional[str]) -> float:
    """Fuzzy similarity (0-100) between the keyword and a single field."""
    if not value:
        return 0.0
    return fuzz.WRatio(keyword, value)


def _text_score(keyword: str, result: dict[str, Any]) -> float:
    """Weighted fuzzy similarity across trackName/sellerName/bundleId.
    Deliberately not the sole confidence signal -- see _popularity_score."""
    total = 0.0
    for field_name, weight in FIELD_WEIGHTS.items():
        total += weight * _field_score(keyword, str(result.get(field_name, "")))
    return total


def _rank_score(position: int, total_results: int) -> float:
    """Score from Apple's own result ordering (0=worst, 100=best). Apple's
    Search API already applies typo-correction and spam demotion; a
    candidate ranked first is meaningfully more trustworthy than one
    ranked last, independent of raw text similarity."""
    if total_results <= 1:
        return 100.0
    return 100.0 * (1 - position / (total_results - 1))


def _popularity_score(result: dict[str, Any]) -> float:
    """Log-scaled score from userRatingCount (0-100). Anti-typosquatting
    signal: clone/scam apps rarely accumulate more than a handful of
    ratings, while the real app usually has orders of magnitude more."""
    rating_count = result.get("userRatingCount") or 0
    if rating_count <= 0:
        return 0.0
    return min(100.0, 100.0 * math.log10(rating_count + 1) / math.log10(POPULARITY_SATURATION_RATING_COUNT + 1))


def _weighted_confidence(keyword: str, result: dict[str, Any], position: int, total_results: int) -> float:
    """Combine text, rank, and popularity signals into one confidence score."""
    signals = {
        "text_score": _text_score(keyword, result),
        "rank_score": _rank_score(position, total_results),
        "popularity_score": _popularity_score(result),
    }
    return sum(SIGNAL_WEIGHTS[name] * value for name, value in signals.items())


def _search_one_country(keyword: str, country: str) -> list[dict[str, Any]]:
    """Query the iTunes Search API for one storefront. Returns [] on
    failure rather than raising, so the caller can try the next country."""
    params = {"term": keyword, "entity": "software", "country": country, "limit": SEARCH_RESULT_LIMIT}
    try:
        response = request_with_retries("GET", SEARCH_URL, params=params)
        response.raise_for_status()
        data = response.json()
    except (UpstreamRequestError, ValueError, requests.RequestException) as exc:
        log.warning("Search API failed for country=%s keyword=%r: %s", country, keyword, exc)
        return []
    return data.get("results", [])


def resolve_app(
    keyword: str,
    preferred_country: Optional[str] = None,
    min_confidence: float = MIN_MATCH_CONFIDENCE,
) -> AppCandidate:
    """Resolve `keyword` to the best-matching app.

    Search order: `preferred_country` first (if given), then
    FALLBACK_COUNTRIES, skipping duplicates. Returns as soon as a candidate
    clears `min_confidence` in some storefront. Raises AppResolutionError
    if no storefront yields a confident match.
    """
    if not keyword or not keyword.strip():
        raise AppResolutionError("keyword must be non-empty")

    countries = list(dict.fromkeys(([preferred_country] if preferred_country else []) + list(FALLBACK_COUNTRIES)))
    best_overall: Optional[AppCandidate] = None

    for country in countries:
        log.info("Searching App Store for %r (country=%s)...", keyword, country)
        results = _search_one_country(keyword, country)
        if not results:
            continue

        scored = [
            (result, _weighted_confidence(keyword, result, position, len(results)))
            for position, result in enumerate(results)
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        top_result, top_score = scored[0]

        log.info(
            "Best match in %s: %r by %r (score=%.1f)",
            country, top_result.get("trackName"), top_result.get("sellerName"), top_score,
        )

        candidate = AppCandidate(
            app_id=top_result.get("trackId"),
            track_name=top_result.get("trackName", ""),
            seller_name=top_result.get("sellerName", ""),
            bundle_id=top_result.get("bundleId", ""),
            country=country,
            confidence=top_score,
            raw=top_result,
        )

        if best_overall is None or candidate.confidence > best_overall.confidence:
            best_overall = candidate
        if candidate.confidence >= min_confidence:
            return candidate

    if best_overall is not None:
        raise AppResolutionError(
            f"No confident match for {keyword!r} in any storefront "
            f"(best: {best_overall.track_name!r} in {best_overall.country}, "
            f"confidence={best_overall.confidence:.1f} < {min_confidence})"
        )
    raise AppResolutionError(f"No app found for {keyword!r} in any storefront")


# =============================================================================
# Review collection: pluggable strategy chain (RSS -> AMP API)
# =============================================================================
#
# Each strategy is independent, retries its own transient failures, and
# signals "I have nothing" by returning 0 rather than raising -- that lets
# the chain move to the next strategy instead of stopping on first failure.
# Adding a new source later means writing one class implementing
# ReviewFetchStrategy and adding it to DEFAULT_STRATEGY_CHAIN below.

EmitFn = Callable[[RawReview], None]

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "im": "http://itunes.apple.com/rss"}

# The AMP-API bearer token is embedded as JSON inside an HTML attribute on
# the app's apps.apple.com page. That JSON can arrive HTML-entity-escaped
# (&quot;) or percent-encoded (%22) depending on how Apple templated the
# page -- we try all plausible decodings against one literal-JSON pattern
# rather than maintaining separate encoded regexes. This is the single
# most likely thing to break if Apple changes its site; it's isolated here
# so a fix is a one-place change.
_TOKEN_JSON_PATTERN = re.compile(r'"token"\s*:\s*"([\w\-.]+)"')


def _extract_bearer_token(page_text: str) -> Optional[str]:
    for transform in (lambda s: s, unquote, html.unescape):
        match = _TOKEN_JSON_PATTERN.search(transform(page_text))
        if match:
            return match.group(1)
    return None


class ReviewFetchStrategy(ABC):
    """One way of collecting reviews for an app. Must not raise on "no
    reviews found" -- return 0 so the chain can move on."""

    name: str

    @abstractmethod
    def fetch(self, app: AppCandidate, limit: int, emit: EmitFn) -> int:
        """Fetch up to `limit` reviews, calling `emit` for each. Returns
        the count fetched; 0 means "try the next strategy"."""
        raise NotImplementedError


class RssReviewFetchStrategy(ReviewFetchStrategy):
    """Apple's public (undocumented, unauthenticated) customer-reviews
    feed. Tried first: fast, no token needed. Known to return an
    empty-but-valid feed both when reviews are genuinely exhausted and
    when the request is throttled -- those can't be told apart from the
    client side, so a short retry-with-backoff runs before giving up."""

    name = "rss"

    def fetch(self, app: AppCandidate, limit: int, emit: EmitFn) -> int:
        count = 0
        max_pages = min(RSS_MAX_PAGES, -(-limit // RSS_REVIEWS_PER_PAGE))

        for page in range(1, max_pages + 1):
            entries = self._fetch_page(app, page)
            if not entries:
                log.info("[rss] page %d empty -- stopping RSS strategy here.", page)
                break

            for entry in entries:
                if count >= limit:
                    return count
                emit(self._to_raw_review(entry))
                count += 1

            time.sleep(POLITE_DELAY_SECS)

        return count

    def _fetch_page(self, app: AppCandidate, page: int) -> list[ET.Element]:
        url = RSS_REVIEWS_URL_TEMPLATE.format(country=app.country, page=page, app_id=app.app_id)
        try:
            response = request_with_retries("GET", url)
        except UpstreamRequestError as exc:
            log.warning("[rss] page %d request failed: %s", page, exc)
            return []

        if response.status_code != 200:
            log.warning("[rss] page %d returned HTTP %d", page, response.status_code)
            return []

        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as exc:
            log.warning("[rss] page %d malformed XML: %s", page, exc)
            return []

        entries = root.findall("atom:entry", _ATOM_NS)
        # The feed's first entry is sometimes app metadata, not a review --
        # a real review always carries an im:rating child.
        return [e for e in entries if e.find("im:rating", _ATOM_NS) is not None]

    @staticmethod
    def _text(entry: ET.Element, tag: str, ns_key: str = "atom") -> Optional[str]:
        el = entry.find(f"{ns_key}:{tag}", _ATOM_NS)
        return el.text if el is not None else None

    @classmethod
    def _to_raw_review(cls, entry: ET.Element) -> RawReview:
        rating_text = cls._text(entry, "rating", ns_key="im")
        try:
            rating = float(rating_text) if rating_text is not None else None
        except ValueError:
            rating = None

        author_el = entry.find("atom:author/atom:name", _ATOM_NS)

        return RawReview(
            review_id=cls._text(entry, "id"),
            title=cls._text(entry, "title"),
            body=cls._text(entry, "content"),
            rating=rating,
            date=cls._text(entry, "updated"),
            author=author_el.text if author_el is not None else None,
            app_version=cls._text(entry, "version", ns_key="im"),
            source="rss",
        )


class AmpApiReviewFetchStrategy(ReviewFetchStrategy):
    """Fallback: Apple's internal web API (used by apps.apple.com itself).
    Requires a short-lived bearer token scraped from the app's own page
    HTML. Undocumented -- if this starts returning 0 consistently, check
    _TOKEN_JSON_PATTERN / _extract_bearer_token first."""

    name = "amp_api"

    def fetch(self, app: AppCandidate, limit: int, emit: EmitFn) -> int:
        token = self._get_bearer_token(app)
        if token is None:
            log.warning("[amp_api] could not extract bearer token -- skipping this strategy.")
            return 0

        count = 0
        offset = 0
        headers = {
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {token}",
            "Origin": "https://apps.apple.com",
        }

        while count < limit and offset < AMP_MAX_OFFSET:
            batch = self._fetch_batch(app, headers, offset)
            if not batch:
                log.info("[amp_api] offset %d empty -- stopping.", offset)
                break

            for raw in batch:
                if count >= limit:
                    return count
                review = self._to_raw_review(raw)
                if review is not None:
                    emit(review)
                    count += 1

            offset += AMP_REVIEWS_PAGE_SIZE
            time.sleep(POLITE_DELAY_SECS)

        return count

    def _get_bearer_token(self, app: AppCandidate) -> Optional[str]:
        url = AMP_APP_PAGE_URL_TEMPLATE.format(country=app.country, app_id=app.app_id)
        try:
            response = request_with_retries("GET", url, max_retries=1)
        except UpstreamRequestError as exc:
            log.warning("[amp_api] could not fetch app page for token: %s", exc)
            return None
        if response.status_code != 200:
            return None
        return _extract_bearer_token(response.text)

    def _fetch_batch(self, app: AppCandidate, headers: dict, offset: int) -> list[dict]:
        url = AMP_REVIEWS_URL_TEMPLATE.format(country_upper=app.country.upper(), app_id=app.app_id)
        params = {"l": "en-us", "offset": str(offset), "limit": str(AMP_REVIEWS_PAGE_SIZE)}
        try:
            response = request_with_retries("GET", url, headers=headers, params=params, max_retries=1)
        except UpstreamRequestError as exc:
            log.warning("[amp_api] batch request failed at offset %d: %s", offset, exc)
            return []
        if response.status_code != 200:
            log.warning("[amp_api] batch at offset %d returned HTTP %d", offset, response.status_code)
            return []
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError):
            return []
        return data.get("data", [])

    @staticmethod
    def _to_raw_review(raw: dict) -> Optional[RawReview]:
        attrs = raw.get("attributes")
        if not isinstance(attrs, dict):
            return None
        return RawReview(
            review_id=raw.get("id"),
            title=attrs.get("title"),
            body=attrs.get("review"),
            rating=float(attrs["rating"]) if attrs.get("rating") is not None else None,
            date=attrs.get("date"),
            author=attrs.get("userName"),
            app_version=None,  # not exposed by this endpoint
            source="amp_api",
        )


# Default chain, in priority order. Append a new strategy here (e.g. a
# headless-browser last resort) without touching any caller.
DEFAULT_STRATEGY_CHAIN: tuple[ReviewFetchStrategy, ...] = (
    RssReviewFetchStrategy(),
    AmpApiReviewFetchStrategy(),
)


def fetch_reviews(
    app: AppCandidate,
    limit: int,
    emit: EmitFn,
    strategies: tuple[ReviewFetchStrategy, ...] = DEFAULT_STRATEGY_CHAIN,
) -> int:
    """Run the strategy chain until `limit` reviews are collected or every
    strategy is exhausted. Raises ReviewFetchError only if every strategy
    returns zero reviews."""
    total = 0
    tried: list[str] = []

    for strategy in strategies:
        remaining = limit - total
        if remaining <= 0:
            break

        log.info("Trying review strategy '%s' (need %d more)...", strategy.name, remaining)
        tried.append(strategy.name)
        try:
            fetched = strategy.fetch(app, remaining, emit)
        except Exception as exc:  # a strategy must never take down the chain
            log.error("Strategy '%s' raised unexpectedly: %s", strategy.name, exc)
            fetched = 0

        total += fetched
        log.info("Strategy '%s' produced %d review(s).", strategy.name, fetched)

    if total == 0:
        raise ReviewFetchError(f"All review strategies exhausted with no results (tried: {', '.join(tried)})")
    return total


# =============================================================================
# Normalization: RawReview -> stable output schema
# =============================================================================

def normalize_review(review: RawReview, app: AppCandidate) -> dict:
    """Produce the contractual output dict for one review. This is the only
    place the output schema is defined -- fetch strategies never build
    output dicts directly."""
    return {
        "review_id": review.review_id,
        "title": review.title,
        "body": review.body,
        "rating": review.rating,
        "date": review.date,
        "author": review.author,
        "platform": PLATFORM_NAME,
        "app_id": app.app_id,
        "app_name": app.track_name,
        "app_version": review.app_version,
        "country": app.country,
        "source": review.source,  # extra metadata: which strategy sourced this review
    }


# =============================================================================
# Platform adapter -- unchanged public interface for the scraper registry
# =============================================================================

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scrapers.base import BaseScraper, ScraperConfig, ScraperResult


class AppStoreScraper(BaseScraper):
    """App Store platform adapter. Unchanged public surface (`platform`,
    `items_key`, `validate_config`, `scrape`) -- all resolution/fetch/
    normalize logic lives above; this class only orchestrates it."""

    platform = PLATFORM_NAME
    items_key = "reviews"

    def validate_config(self, config: ScraperConfig) -> None:
        if not config.keyword.strip():
            raise ValueError("keyword is required")
        if config.limit <= 0:
            raise ValueError("limit must be positive")

    def scrape(self, config: ScraperConfig) -> ScraperResult:
        self.validate_config(config)
        preferred_country = config.extra.get("country") if config.extra else None

        try:
            app = resolve_app(config.keyword, preferred_country=preferred_country)
        except AppResolutionError as exc:
            raise ValueError(f"Could not resolve an App Store app for {config.keyword!r}: {exc}") from exc

        log.info(
            "Resolved %r -> %r (id=%s, country=%s, confidence=%.1f)",
            config.keyword, app.track_name, app.app_id, app.country, app.confidence,
        )

        items: list[dict[str, Any]] = []

        def emit(raw_review: RawReview) -> None:
            items.append(self.normalize_item(normalize_review(raw_review, app)))

        try:
            count = fetch_reviews(app, config.limit, emit)
        except ReviewFetchError as exc:
            # A resolved app with genuinely zero reviews is legitimate --
            # return an empty, valid result rather than raising.
            log.warning("No reviews collected for %r: %s", app.track_name, exc)
            count = 0

        return ScraperResult(query=config.keyword, platform=self.platform, count=count, items=items)


# =============================================================================
# CLI
# =============================================================================

def _run_cli(keyword: str, limit: int, country: Optional[str], output: Optional[str]) -> None:
    log.info("Starting App Store review scrape for %r (limit=%d)", keyword, limit)

    try:
        app = resolve_app(keyword, preferred_country=country)
    except AppResolutionError as exc:
        log.error("Could not resolve an App Store app: %s", exc)
        sys.exit(1)

    log.info(
        "Resolved %r -> %r (id=%s, country=%s, confidence=%.1f)",
        keyword, app.track_name, app.app_id, app.country, app.confidence,
    )

    collected: list[dict[str, Any]] = []

    def emit_to_list(raw_review: RawReview) -> None:
        collected.append(normalize_review(raw_review, app))

    def emit_to_stdout(raw_review: RawReview) -> None:
        print(json.dumps(normalize_review(raw_review, app), ensure_ascii=False))
        sys.stdout.flush()

    emit = emit_to_list if output else emit_to_stdout

    try:
        count = fetch_reviews(app, limit, emit)
    except ReviewFetchError as exc:
        log.warning("No reviews collected: %s", exc)
        count = 0

    if output:
        result = {"query": keyword, "platform": PLATFORM_NAME, "count": count, "reviews": collected}
        with open(output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        log.info("Wrote %d review(s) to %s", count, output)
    else:
        log.info("Done. %d review(s) streamed.", count)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape App Store reviews with automatic strategy fallback.")
    parser.add_argument("keyword", help="App name/keyword to search for (e.g. 'Nike')")
    parser.add_argument("--limit", type=int, default=50, help="Max reviews to collect (hard cap ~500/strategy)")
    parser.add_argument(
        "--country", default=None,
        help="Preferred App Store storefront/country code, tried before the automatic fallback list",
    )
    parser.add_argument("--output", default=None, help="Optional file path to save JSON output")
    args = parser.parse_args()
    _run_cli(args.keyword, args.limit, args.country, args.output)


if __name__ == "__main__":
    main()