# Scraper Constraints & Limits

Quick reference for what each scraper can do, what limits apply, and whether it is suitable for production (Brand24-style monitoring).

**Legend**
- **MVP** = good for testing and small demos
- **Production** = suitable for a real monitoring product at scale
- **API** = official or third-party API
- **Scrape** = browser/HTML scraping (no official API)

---

## Summary table

| Scraper | Method | Auth | Main limit | Pagination | MVP | Production |
|---------|--------|------|------------|------------|-----|------------|
| `reddit_fetch.py` | API (PRAW) | OAuth app creds | 100 req/min | Yes | Yes | Maybe* |
| `youtube_fetch.py` | API | API key | 10,000 units/day | Yes | Yes | Limited |
| `vimeo_fetch.py` | API | Access token | Rate-limited per app | Yes | Yes | Maybe |
| `bluesky_fetch.py` | API (AT Protocol) | Account login | 3,000 req/5 min; search cursor often blocked | Partial | Yes | Limited |
| `newsapi_fetch.py` | API | API key | ~100 req/day (free) | Yes | Yes | No |
| `gmaps_fetch.py` | API (SerpAPI) | API key | 250 searches/month (free) | Yes | Yes | No |
| `mastodon_fetch.py` | Public API | None | Instance-dependent (~300–600 req/5 min typical) | Yes | Yes | Maybe |
| `wordpress_fetch.py` | Public API | None | WordPress.com rate limits | Yes | Yes | Maybe |
| `googleplaystore_fetch.py` | Library (`google-play-scraper`) | None | Unofficial; can break or get blocked | Yes | Yes | No |
| `bbc_fetch.py` | RSS + Scrape | None | No hard API cap; be polite (delays built in) | Yes | Yes | Limited |
| `blogger_fetch.py` | RSS + Scrape | None | Same as above | Yes | Yes | Limited |
| `substack_fetch.py` | Scrape | None | Bot detection risk; slow | Yes | Yes | No |
| `booking_fetch.py` | Scrape (Playwright) | None | Slow; DOM changes; ToS risk | Partial | Yes | No |
| `trustpilot_fetch.py` | Scrape (Playwright) | None | Bot detection; layout changes | Yes | Yes | No |
| `amazon_fetch.py` | Scrape (Playwright) | None | CAPTCHA; blocks; ToS risk | Yes | Yes | No |
| `tripadvisor_fetch.py` | Scrape (Playwright) | None | Bot detection; layout changes | Yes | Yes | No |
| `g2_fetch.py` | Scrape (Playwright) | None | Same as above | Yes | Yes | No |
| `capterra_fetch.py` | Scrape (Playwright) | None | Same as above | Yes | Yes | No |
| `appstore_fetch.py` | Scrape (Playwright) | None | Same as above | Yes | Yes | No |
| `tiktok_fetch.py` | Scrape (Playwright) | None | Heavy anti-bot; very unstable | Partial | Yes | No |
| `social_media/stackexchange_fetch.py` | API | None | 300 req/day (no key); 10,000/day (with key) | Yes | Yes | Maybe |

\* Reddit commercial use may require a paid enterprise agreement.

---

## API-based scrapers (with quotas)

### Reddit — `social_media/reddit_fetch.py`

| | |
|---|---|
| **Credentials** | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` |
| **Rate limit** | **100 requests/minute** (OAuth) |
| **Daily cap** | No fixed daily number |
| **Cost per run** | ~1 search + **1–5+ API calls per post** (comments are expensive) |
| **Example** | `--limit 50 --comment-limit 20` → roughly 50–250+ API calls |
| **Notes** | PRAW handles backoff automatically. Best API in this repo for frequent testing. |

### YouTube — `social_media/youtube_fetch.py`

| | |
|---|---|
| **Credentials** | `YOUTUBE_API_KEY` (Google Cloud) |
| **Rate limit** | **10,000 quota units/day** (resets midnight PT) |
| **Cost per run** | `search.list` = **100 units** + `videos.list` = **1 unit** per batch |
| **Example** | 50 videos ≈ **101 units** → ~**99 runs/day** max |
| **Notes** | Official API. No paid quota top-up; must request extension from Google. |

### Vimeo — `social_media/vimeo_fetch.py`

| | |
|---|---|
| **Credentials** | `VIMEO_ACCESS_TOKEN` (personal access token from developer.vimeo.com) |
| **Rate limit** | App-dependent; Vimeo enforces per-app limits |
| **Cost per run** | **1 request per page** (up to 100 videos/page) |
| **Example** | 50 videos = 1 request; 150 videos = 2 requests |
| **Notes** | Official API. Generate token under **Authenticated (you)** with `public` scope. Do not use the client secret as the access token. |

### Bluesky — `social_media/bluesky_fetch.py`

| | |
|---|---|
| **Credentials** | `BSKY_HANDLE`, `BSKY_APP_PASSWORD` |
| **Rate limit** | **3,000 requests / 5 minutes** (IP); login **300/day** per account |
| **Search limit** | **Cursor pagination often returns 403** — usually only first page (~25 posts) |
| **Cost per run** | 1 login + 1+ search calls |
| **Notes** | Free. Search is not meant for deep/historical monitoring. Use Jetstream/firehose for production. |

### NewsAPI — `news/newsapi_fetch.py`

| | |
|---|---|
| **Credentials** | `NEWSAPI_KEY` |
| **Rate limit** | **~100 requests/day** (free developer plan) |
| **Cost per run** | **1 request per page** (up to 100 articles/page) |
| **Example** | 50 articles = 1 request; 150 articles = 2 requests |
| **Notes** | Articles on free tier are often limited to recent history. |

### Google Maps — `review_sites/gmaps_fetch.py`

| | |
|---|---|
| **Credentials** | `SERPAPI_KEY` |
| **Rate limit** | **250 successful searches/month** (free SerpAPI plan) |
| **Cost per place** | **1** search (find place) + **1 per reviews page** |
| **Example** | 1 place, 3 review pages ≈ **4 searches** → ~**60 places/month** on free tier |
| **Notes** | Not official Google API. For production use **Google Places API** instead (max ~5 reviews per call). |

### Mastodon — `social_media/mastodon_fetch.py`

| | |
|---|---|
| **Credentials** | None |
| **Rate limit** | Depends on instance (often **300–600 req / 5 min**) |
| **Cost per run** | 1+ API calls depending on mode (hashtag vs search) |
| **Notes** | Public API, no key. Each instance has its own rules. |

### WordPress — `news/wordpress_fetch.py`

| | |
|---|---|
| **Credentials** | None |
| **Rate limit** | WordPress.com public search API (undocumented hard cap) |
| **Notes** | Good for blog discovery. Not all WordPress sites are indexed. |

### Stack Exchange — `social_media/stackexchange_fetch.py`

| | |
|---|---|
| **Credentials** | None (optional API key for higher quota) |
| **Rate limit** | **300 requests/day** (no key); **10,000 requests/day** (with key) |
| **Cost per run** | 1+ API calls depending on `--limit` and pagination |
| **Notes** | Official REST API. Respects `backoff` in responses. Supports `--site` (e.g. stackoverflow, superuser) and `--strict` relevance filter. |

### Google Play Store — `review_sites/googleplaystore_fetch.py`

| | |
|---|---|
| **Credentials** | None |
| **Rate limit** | **Unofficial** library — no guaranteed limits; can be blocked |
| **Notes** | Uses `google-play-scraper` package. Can break when Google changes internals. |

---

## Scraping-based scrapers (no API key, but fragile)

These use **Playwright** or **HTTP + BeautifulSoup**. They have **no fixed API quota**, but face other constraints:

| Risk | Impact |
|------|--------|
| **Bot detection / CAPTCHA** | Run fails or returns empty results |
| **DOM changes** | Selectors break; scraper needs maintenance |
| **Terms of Service** | Many sites forbid automated scraping |
| **Speed** | Slow (browser startup, page loads, delays) |
| **IP blocking** | Possible at high volume without proxies |

### Affected scrapers

- `review_sites/booking_fetch.py` — Booking.com guest reviews
- `review_sites/trustpilot_fetch.py` — Trustpilot company reviews
- `review_sites/amazon_fetch.py` — Amazon product reviews
- `review_sites/tripadvisor_fetch.py` — Tripadvisor place reviews
- `review_sites/g2_fetch.py` — G2 product reviews
- `review_sites/capterra_fetch.py` — Capterra reviews
- `review_sites/appstore_fetch.py` — Apple App Store reviews
- `social_media/tiktok_fetch.py` — TikTok videos + comments
- `news/bbc_fetch.py` — BBC News articles (RSS + article scrape)
- `blogs/blogger_fetch.py` — Blogger posts
- `blogs/substack_fetch.py` — Substack posts

**Booking-specific:** tries up to 5 properties if the first has no reviews; prefers listings with review scores.

---

## Cost comparison (one typical run)

Assumes default `--limit 50` where applicable:

| Scraper | Approx. cost of 1 run |
|---------|----------------------|
| Reddit (with comments) | 50–250+ API calls |
| YouTube | ~101 quota units |
| Bluesky | ~2 API calls (may stop at ~25 posts) |
| NewsAPI | 1 request |
| GMaps (SerpAPI) | 2–5 searches |
| Scrapers (Playwright) | Time + bot risk (no API counter) |

---

## Brand24-scale verdict

| Tier | Scrapers |
|------|----------|
| **Best starting points** | Reddit, YouTube, Vimeo, Bluesky, Mastodon, NewsAPI |
| **OK for demos only** | GMaps (SerpAPI), Google Play, WordPress, BBC, Blogger |
| **Not for production monitoring** | Booking, Amazon, Trustpilot, Tripadvisor, G2, Capterra, App Store, TikTok, Substack |

For a real Brand24-like product you would need:
- Official APIs where they exist
- Paid tiers or enterprise agreements (Reddit, Google)
- Streaming/firehose for Bluesky (not search)
- Queues, caching, and rate-limit tracking across all sources
- Secrets in environment variables (not hardcoded in code)

---

## Environment variables

| Variable | Used by |
|----------|---------|
| `REDDIT_CLIENT_ID` | reddit_fetch |
| `REDDIT_CLIENT_SECRET` | reddit_fetch |
| `REDDIT_USER_AGENT` | reddit_fetch |
| `YOUTUBE_API_KEY` | youtube_fetch |
| `BSKY_HANDLE` | bluesky_fetch |
| `BSKY_APP_PASSWORD` | bluesky_fetch |
| `NEWSAPI_KEY` | newsapi_fetch |
| `SERPAPI_KEY` | gmaps_fetch |
| `VIMEO_ACCESS_TOKEN` | vimeo_fetch |

All scrapers support `--limit` and most support `--output` for JSON file export.
