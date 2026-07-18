#!/usr/bin/env python3
"""Trustpilot scraper — enter a company name or keyword, get reviews as JSON.

Usage:
    python scrapers/trustpilot_fetch.py "Brand24"
    python scrapers/trustpilot_fetch.py "Brand24" --limit 20
    python scrapers/trustpilot_fetch.py "Brand24" --output results.json
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
from bs4 import BeautifulSoup

def search(keyword: str, limit: int = 50) -> list[dict]:
    """
    Search for a keyword on Trustpilot, find the top company, and scrape its reviews.
    """
    print(f"Searching Trustpilot for '{keyword}'...", file=sys.stderr)
    
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed.", file=sys.stderr)
        return []

    reviews = []
    
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        # Trustpilot may be sensitive to bots, use a standard user agent
        page = browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="en-US"
        )
        
        # Block unnecessary resources to make search insanely fast
        page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media", "font", "stylesheet"] else route.continue_())
        
        # Step 1: Search for the company
        search_url = f"https://www.trustpilot.com/search?query={urllib.parse.quote(keyword)}"
        page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_selector('a[href*="/review/"]', timeout=5000)
        except Exception:
            page.wait_for_timeout(2000)
        
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        
        # Find the first business link
        company_link = None
        for a in soup.find_all("a", href=True):
            if "/review/" in a["href"] and a.get("name") == "business-unit-card":
                company_link = a["href"]
                break
                
        if not company_link:
            # Try a broader matching if specific name attribute is missing
            for a in soup.find_all("a", href=True):
                if "/review/" in a["href"] and not a["href"].endswith("/review/"):
                    company_link = a["href"]
                    break
        
        if not company_link:
            print("Could not find a company profile matching the keyword.", file=sys.stderr)
            browser.close()
            return []
            
        if not company_link.startswith("http"):
            company_link = "https://www.trustpilot.com" + company_link
            
        print(f"Found company profile: {company_link}", file=sys.stderr)
        
        # Step 2: Extract reviews with pagination
        current_page = 1
        
        while len(reviews) < limit:
            page_url = f"{company_link}?page={current_page}"
            if current_page > 1:
                print(f"Fetching page {current_page}...", file=sys.stderr)
            
            page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_selector("article", timeout=5000)
            except Exception:
                page.wait_for_timeout(2000)
                
            soup = BeautifulSoup(page.content(), "html.parser")
            review_articles = soup.find_all("article")
            
            # Filter articles that are actually reviews
            valid_reviews = [art for art in review_articles if "data-service-review-card-paper" in art.attrs or art.find(attrs={"data-service-review-rating-typography": True})]
            
            if not valid_reviews:
                break
                
            for article in valid_reviews:
                if len(reviews) >= limit:
                    break
                    
                # Title - typically in h2 tag
                title_el = article.find("h2")
                title = title_el.get_text(strip=True) if title_el else None
                
                # Text - typically in a p tag that is not wrapped in aside or company reply
                body = None
                p_tags = article.find_all("p")
                valid_p = []
                for p in p_tags:
                    # Ignore paragraphs inside reply or aside blocks
                    if p.find_parent(attrs={"data-service-review-company-reply-typography": "true"}) or p.find_parent(class_=re.compile("reply", re.I)):
                        continue
                    if p.find_parent("aside"):
                        continue
                    text = p.get_text(strip=True)
                    if text:
                        valid_p.append(text)
                
                if valid_p:
                    # Usually the main review body is the longest or primary remaining paragraph
                    body = "\n".join(valid_p)
                
                # Rating
                rating = None
                img = article.find("img", alt=re.compile(r"Rated \d out of 5 stars"))
                if img and img.get("alt"):
                    m = re.search(r"Rated (\d)", img["alt"])
                    if m:
                        rating = int(m.group(1))
                else:
                    # Alternative rating extraction
                    div_rating = article.find(attrs={"data-service-review-rating-typography": True})
                    if div_rating:
                        m = re.search(r"(\d)", div_rating.get_text())
                        if m:
                            rating = int(m.group(1))
                
                # Date
                date = None
                time_el = article.find("time")
                if time_el and time_el.get("datetime"):
                    date = time_el["datetime"]
                    
                # Author
                author = None
                author_el = article.find(attrs={"data-consumer-name-typography": "true"})
                if author_el:
                    author = author_el.get_text(strip=True)
                    
                # Location & Review Count
                author_location = None
                author_reviews = None
                aside = article.find("aside")
                if aside:
                    # Usually location is in a span inside a div next to the name, same for review count
                    spans = aside.find_all("span")
                    for span in spans:
                        text = span.get_text(strip=True)
                        if "review" in text.lower():
                            m = re.search(r"(\d+)", text)
                            if m:
                                author_reviews = int(m.group(1))
                        elif len(text) == 2 or text.istitle():  # Simple heuristic for country codes/names like 'US', 'GB', 'Germany'
                            author_location = text if not author_location else author_location
                            
                # Verified status
                is_verified = bool(article.find(string=re.compile("Verified", re.IGNORECASE)))
                
                # Date of Experience
                experience_date = None
                exp_bold = article.find("b", string=re.compile("Date of experience:", re.IGNORECASE))
                if exp_bold and exp_bold.next_sibling:
                    experience_date = str(exp_bold.next_sibling).strip()
                
                # Company Reply
                company_reply = None
                reply_div = article.find(attrs={"data-service-review-company-reply-typography": "true"}) or article.find(class_=re.compile("reply", re.I))
                if reply_div:
                    reply_text_div = reply_div.find("p") or reply_div
                    company_reply = {"text": reply_text_div.get_text(strip=True), "date": None}
                    
                    time_reply = reply_div.find("time")
                    if time_reply and time_reply.get("datetime"):
                        company_reply["date"] = time_reply["datetime"]

                reviews.append({
                    "title": title,
                    "body": body,
                    "rating": rating,
                    "date": date,
                    "experience_date": experience_date,
                    "author": author,
                    "author_location": author_location,
                    "author_review_count": author_reviews,
                    "is_verified": is_verified,
                    "company_reply": company_reply,
                    "platform": "trustpilot",
                    "company_url": company_link
                })
                
            # Check if there is a next page
            next_btn = soup.find("a", attrs={"name": "pagination-button-next"})
            if not next_btn or "disabled" in next_btn.attrs.get("class", []):
                break
                
            current_page += 1
            
        browser.close()
        
    return reviews


import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scrapers.base import BaseScraper, ScraperConfig, ScraperResult


class TrustpilotScraper(BaseScraper):
    platform = "trustpilot"
    items_key = "reviews"

    def validate_config(self, config: ScraperConfig) -> None:
        if not config.keyword.strip():
            raise ValueError("keyword is required")

    def scrape(self, config: ScraperConfig) -> ScraperResult:
        self.validate_config(config)
        reviews = search(config.keyword, config.limit)
        items = [self.normalize_item(review) for review in reviews]
        return ScraperResult(
            query=config.keyword,
            platform=self.platform,
            count=len(items),
            items=items,
        )


def main():
    parser = argparse.ArgumentParser(
        description="Scrape Trustpilot reviews for a company or keyword."
    )
    parser.add_argument("keyword", nargs="+", help="Company name or keyword, e.g. Brand24")
    parser.add_argument(
        "--limit", type=int, default=50, help="Max reviews (default: 50)"
    )
    parser.add_argument("--output", help="Save JSON to file (default: print to stdout)")
    args = parser.parse_args()

    full_keyword = " ".join(args.keyword)
    results = search(full_keyword, args.limit)

    if not results:
        print("No reviews found.", file=sys.stderr)
        sys.exit(1)

    output = json.dumps(
        {
            "query": full_keyword,
            "platform": "trustpilot",
            "count": len(results),
            "reviews": results
        },
        ensure_ascii=False,
        indent=2
    )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Saved {len(results)} reviews → {args.output}", file=sys.stderr)
    else:
        print(output)
        print(len(results))

if __name__ == "__main__":
    main()
