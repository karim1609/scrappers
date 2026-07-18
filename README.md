# Multi-Platform Scraper Collection

A Python toolkit for searching keywords across **22 platforms** — social media, news, blogs, and review sites. Each scraper normalizes results into a consistent JSON shape so you can compare mentions, reviews, and articles from one place.

Built for **demos, prototyping, and small-scale monitoring** (similar to a lightweight Brand24-style workflow). For production limits, rate caps, and API quotas, see [`scrapers/CONSTRAINTS.md`](scrapers/CONSTRAINTS.md).

---

## Features

- **22 scrapers** across four categories: social media, news, blogs, and review sites
- **Unified interface** via `BaseScraper`, `ScraperConfig`, and `ScraperResult`
- **Central registry** — pick any scraper by name without importing modules manually
- **Interactive CLI** (`test_scraper.py`) or direct per-scraper scripts
- **CSV export** from the test runner; JSON/JSONL from individual scrapers
- **Docker support** with Playwright and Chromium pre-installed

---

## Project structure

```
test scrap/
├── test_scraper.py          # Interactive menu + unified runner
├── Dockerfile               # Container image (Python 3.12 + Playwright)
├── entrypoint.sh            # Docker helper — resolves legacy script paths
├── output/                  # CSV results from test_scraper.py
└── scrapers/
    ├── base.py              # BaseScraper, ScraperConfig, ScraperResult
    ├── registry.py          # Scraper registration and lookup
    ├── CONSTRAINTS.md       # Rate limits, auth, production suitability
    ├── requirements.txt
    ├── social_media/        # Reddit, YouTube, Bluesky, Mastodon, TikTok, …
    ├── news/                # BBC, Guardian, NewsAPI, WordPress
    ├── blogs/               # Blogger, Substack
    └── review_sites/        # Amazon, Trustpilot, Booking, G2, …
```

### Architecture

Every scraper extends `BaseScraper` and implements:

| Method | Purpose |
|--------|---------|
| `validate_config(config)` | Validate keyword and scraper-specific `config.extra` options |
| `scrape(config)` | Fetch data and return a `ScraperResult` |

```python
from scrapers.registry import get_scraper
from scrapers.base import ScraperConfig

scraper = get_scraper("reddit")
config = ScraperConfig(
    keyword="adidas",
    limit=10,
    extra={"subreddit": "all", "comment_limit": 20},
)
result = scraper.scrape(config)

print(result.platform)  # "reddit"
print(result.count)     # number of items
print(result.items)     # list of normalized dicts
```

`ScraperConfig` fields:

| Field | Default | Description |
|-------|---------|-------------|
| `keyword` | — | Search term or topic (required) |
| `limit` | `10` | Maximum number of items to return |
| `strict` | `False` | Enable relevance filtering where supported |
| `output_path` | `None` | Optional JSON output path (per-scraper CLI) |
| `extra` | `{}` | Scraper-specific options (subreddit, site, domain, etc.) |

---

## Installation

### Local setup

**Requirements:** Python 3.12+ recommended

```bash
cd "test scrap"
pip install -r scrapers/requirements.txt
python -m playwright install chromium
```

### Docker

Build the image once (or after adding new scrapers):

```powershell
docker build -t scrapers_engine .
```

**Interactive menu** (mounts local `test_scraper.py` and `output/`):

```powershell
docker run -it --rm `
  -v "$($PWD.Path)\test_scraper.py:/app/test_scraper.py" `
  -v "$($PWD.Path)\output:/app/output" `
  --entrypoint python scrapers_engine /app/test_scraper.py
```

**One-shot run** with API credentials:

```powershell
docker run -it --rm `
  -v "$($PWD.Path)\test_scraper.py:/app/test_scraper.py" `
  -v "$($PWD.Path)\output:/app/output" `
  -e VIMEO_ACCESS_TOKEN=your_token_here `
  --entrypoint python scrapers_engine /app/test_scraper.py vimeo adidas 5
```

Pass environment variables for API-based scrapers (`REDDIT_CLIENT_ID`, `YOUTUBE_API_KEY`, `VIMEO_ACCESS_TOKEN`, etc.).

After adding or editing scraper modules locally, either **rebuild the image** or mount the scrapers folder:

```powershell
-v "$($PWD.Path)\scrapers:/app/scrapers"
```

---

## Usage

### 1. Unified test runner (recommended)

**Interactive menu:**

```bash
python test_scraper.py
```

**Direct invocation:**

```bash
python test_scraper.py <scraper> <keyword> <limit>
```

Examples:

```bash
python test_scraper.py reddit adidas 5
python test_scraper.py bbc technology 3
python test_scraper.py trustpilot Brand24 10
```

Results are printed to the console and saved to `output/<scraper>_results.csv`. Nested fields (lists, dicts) are JSON-encoded in CSV cells.

### 2. Individual scraper scripts

Each module can also be run standalone with its own CLI:

```bash
python scrapers/social_media/reddit_fetch.py adidas --limit 20 --subreddit all
python scrapers/news/bbc_fetch.py Morocco --limit 10 --output results.json
python scrapers/review_sites/trustpilot_fetch.py "Brand24" --limit 50
```

Most scrapers accept:

- `keyword` — positional argument(s)
- `--limit` — max items (default varies by scraper, often 50)
- `--output` — write JSON to a file instead of stdout

### 3. Programmatic use

```python
from scrapers.registry import get_scraper, SCRAPERS
from scrapers.base import ScraperConfig

# List all registered scrapers
print(sorted(SCRAPERS.keys()))

scraper = get_scraper("youtube")
result = scraper.scrape(ScraperConfig(keyword="OpenAI", limit=5))
```

---

## Available scrapers

| Registry name | Module | Method | Auth required |
|---------------|--------|--------|---------------|
| **Social media** | | | |
| `reddit` | `social_media/reddit_fetch.py` | PRAW API | Yes |
| `youtube` | `social_media/youtube_fetch.py` | Google API | Yes |
| `bluesky` | `social_media/bluesky_fetch.py` | AT Protocol | Yes |
| `mastodon` | `social_media/mastodon_fetch.py` | Public API | No |
| `tiktok` | `social_media/tiktok_fetch.py` | Playwright | No |
| `stackexchange` | `social_media/stackexchange_fetch.py` | REST API | Optional key |
| `vimeo` | `social_media/vimeo_fetch.py` | Vimeo API | Yes |
| **News** | | | |
| `bbc` | `news/bbc_fetch.py` | RSS + HTTP scrape | No |
| `guardian` | `news/guardian_fetch.py` | Guardian API | Yes |
| `newsapi` | `news/newsapi_fetch.py` | NewsAPI | Yes |
| `wordpress` | `news/wordpress_fetch.py` | WordPress.com API | No |
| **Blogs** | | | |
| `blogger` | `blogs/blogger_fetch.py` | RSS + scrape | No |
| `substack` | `blogs/substack_fetch.py` | HTTP scrape | No |
| **Review sites** | | | |
| `amazon` | `review_sites/amazon_fetch.py` | Playwright | No |
| `appstore` | `review_sites/appstore_fetch.py` | Playwright | No |
| `booking` | `review_sites/booking_fetch.py` | Playwright | No |
| `capterra` | `review_sites/capterra_fetch.py` | Playwright | No |
| `g2` | `review_sites/g2_fetch.py` | Playwright | No |
| `gmaps` | `review_sites/gmaps_fetch.py` | SerpAPI | Yes |
| `google_play` | `review_sites/googleplaystore_fetch.py` | `google-play-scraper` | No |
| `trustpilot` | `review_sites/trustpilot_fetch.py` | Playwright | No |
| `tripadvisor` | `review_sites/tripadvisor_fetch.py` | Playwright | No |

### Scraper-specific options (`config.extra`)

| Scraper | Example `extra` | Description |
|---------|-----------------|-------------|
| `reddit` | `{"subreddit": "all", "sort": "relevance", "time_filter": "all", "comment_limit": 20}` | Subreddit, sort, time window, comments per post |
| `stackexchange` | `{"site": "stackoverflow"}` | Stack Exchange site (default: stackoverflow) |
| `amazon` | `{"domain": "com"}` | Amazon TLD (`com`, `co.uk`, `de`, …) |
| `guardian` | `{"order_by": "relevance"}` | Sort order for Guardian API |
| `vimeo` | `{"sort": "relevant", "direction": "desc"}` | Sort order for Vimeo search |
| `mastodon` | `{"instance": "https://mastodon.social", "mode": "auto"}` | Fediverse instance and search mode |

The test runner pre-sets extras for `stackexchange`, `amazon`, and `guardian`. For other scrapers, pass `extra` when using the Python API.

---

## Environment variables

Set these before running API-based scrapers:

| Variable | Used by |
|----------|---------|
| `REDDIT_CLIENT_ID` | `reddit` |
| `REDDIT_CLIENT_SECRET` | `reddit` |
| `REDDIT_USER_AGENT` | `reddit` |
| `YOUTUBE_API_KEY` | `youtube` |
| `BSKY_HANDLE` | `bluesky` |
| `BSKY_APP_PASSWORD` | `bluesky` |
| `NEWSAPI_KEY` | `newsapi` |
| `SERPAPI_KEY` | `gmaps` |
| `GUARDIAN_API_KEY` | `guardian` |
| `VIMEO_ACCESS_TOKEN` | `vimeo` |
| `TIKTOK_PROXY` | `tiktok` (optional) |

Some scrapers ship with in-code API key defaults for quick testing. For anything beyond demos, use your own keys via environment variables and rotate any keys that were committed to the repo.

On Windows (PowerShell):

```powershell
$env:REDDIT_CLIENT_ID = "your_client_id"
$env:REDDIT_CLIENT_SECRET = "your_secret"
$env:REDDIT_USER_AGENT = "my-scraper/1.0"
```

On Linux/macOS:

```bash
export REDDIT_CLIENT_ID=your_client_id
export REDDIT_CLIENT_SECRET=your_secret
export REDDIT_USER_AGENT=my-scraper/1.0
```

---

## Output format

### `ScraperResult` envelope

```json
{
  "query": "adidas",
  "platform": "reddit",
  "count": 2,
  "items": [ ... ]
}
```

Each scraper defines its own item schema (posts, reviews, articles, etc.) but includes a `platform` field and normalized metadata (author, URL, timestamps, scores, etc.).

### CSV export (`test_scraper.py`)

Files are written to `output/<scraper>_results.csv`. Complex nested values are stored as JSON strings.

---

## Dependencies

From `scrapers/requirements.txt`:

| Package | Purpose |
|---------|---------|
| `playwright` + `playwright-stealth` | Browser-based scrapers (Amazon, Trustpilot, TikTok, …) |
| `requests` + `beautifulsoup4` | HTTP scraping and HTML parsing |
| `praw` | Reddit API |
| `google-api-python-client` | YouTube Data API |
| `atproto` | Bluesky AT Protocol |
| `google-play-scraper` | Google Play Store reviews |
| `rapidfuzz` | Fuzzy matching / relevance filtering |

---

## Limitations and production notes

Scrapers fall into three tiers:

| Tier | Scrapers | Notes |
|------|----------|-------|
| **Best for frequent use** | Reddit, YouTube, Bluesky, Mastodon, NewsAPI | Official or stable APIs; still subject to quotas |
| **OK for demos** | GMaps (SerpAPI), Google Play, WordPress, BBC, Blogger, Stack Exchange | Limited quotas or unofficial access |
| **Fragile / demo only** | Booking, Amazon, Trustpilot, Tripadvisor, G2, Capterra, App Store, TikTok, Substack | Playwright scraping; bot detection, DOM changes, ToS risk |

Playwright-based scrapers have no API quota but can fail due to CAPTCHAs, layout changes, or IP blocks. See [`scrapers/CONSTRAINTS.md`](scrapers/CONSTRAINTS.md) for per-scraper rate limits, cost per run, and Brand24-scale guidance.

---

## Adding a new scraper

1. Create `scrapers/<category>/my_fetch.py` with a `search()` function and a `MyScraper(BaseScraper)` class.
2. Implement `validate_config()` and `scrape()`.
3. Register in `scrapers/registry.py`:

   ```python
   from scrapers.category.my_fetch import MyScraper
   _register("my_scraper", MyScraper)
   ```

4. Optionally add a `main()` CLI block with `argparse` for standalone use.
5. Document limits in `scrapers/CONSTRAINTS.md`.

---

## Legal and ethical use

- Respect each platform's **Terms of Service** and **robots.txt**.
- Use **official APIs** where available; scraping may violate site policies.
- Store credentials in **environment variables**, never in source code.
- Add **delays** and reasonable request volumes; many scrapers include built-in backoff.
- Reddit commercial monitoring may require a **paid enterprise agreement**.

This project is intended for **research, testing, and personal use**. You are responsible for compliance with applicable laws and platform policies.

---

## Quick reference

```bash
# Install
pip install -r scrapers/requirements.txt && python -m playwright install chromium

# Interactive
python test_scraper.py

# One-shot
python test_scraper.py youtube "OpenAI" 5

# Standalone
python scrapers/news/bbc_fetch.py technology --limit 5 --output bbc.json

# Docker (PowerShell)
docker build -t scrapers_engine .
docker run -it --rm -v "$($PWD.Path)\test_scraper.py:/app/test_scraper.py" -v "$($PWD.Path)\output:/app/output" --entrypoint python scrapers_engine /app/test_scraper.py
```
