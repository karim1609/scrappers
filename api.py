import logging
import time
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

import dotenv
dotenv.load_dotenv()

# Project imports
from scrapers.registry import SCRAPERS, get_scraper
from scrapers.base import ScraperConfig

# New Integration
from scrapers.orchestrator import ScraperOrchestrator
from scrapers.exporter import CSVExporter
from fastapi.responses import Response

# Same extras used in test_scraper.py to ensure identical CLI behavior
SCRAPER_EXTRAS = {
    "stackexchange": {"site": "stackoverflow"},
    "amazon": {"domain": "com"},
    "guardian": {"order_by": "relevance"},
    "vimeo": {"sort": "relevant", "direction": "desc"},
}

app = FastAPI(
    title="SOLID Scrapers API",
    version="1.0.0",
    description="A thin REST API layer built over the local OOP Scrapers suite."
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("api")

# Models 
class ScrapeRequest(BaseModel):
    platform: str
    keyword: str
    limit: int = 20

class MultiScrapeRequest(BaseModel):
    keywords: List[str]
    platforms: List[str]
    total_limit: int = 50

@app.middleware("http")
async def measure_execution_time(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    logger.info(f"{request.method} {request.url.path} completed in {process_time:.2f}s")
    return response

@app.get("/")
def get_status():
    return {
        "status": "running",
        "service": "SOLID Scrapers API"
    }

@app.get("/scrapers")
def list_scrapers():
    """Return all available scraper names from registry.py"""
    names = list(SCRAPERS.keys())
    names.sort()
    return {"available_scrapers": names, "count": len(names)}

@app.post("/scrape")
def trigger_scrape(req: ScrapeRequest):
    logger.info(f"Incoming scrape request: Platform={req.platform}, Keyword='{req.keyword}', Limit={req.limit}")
    
    # Validation
    if req.platform not in SCRAPERS:
        logger.warning(f"Platform 404: {req.platform} not found.")
        raise HTTPException(status_code=404, detail=f"Unknown platform '{req.platform}'. Use GET /scrapers to view available platforms.")
    
    # Config builder matching test_scraper.py
    config = ScraperConfig(keyword=req.keyword, limit=req.limit)
    extra = SCRAPER_EXTRAS.get(req.platform)
    if extra:
        config.extra = extra
        
    try:
        scraper = get_scraper(req.platform)
        start = time.time()
        results = scraper.scrape(config)
        elapsed = time.time() - start
        logger.info(f"Scraping successful. Retrieved {results.count} items in {elapsed:.2f}s")
        
        # Standard to_json transformation requested by Tasks
        transformed = scraper.to_json(results)
        
        return {
            "success": True,
            "platform": req.platform,
            "keyword": req.keyword,
            "count": results.count,
            "results": transformed.get(scraper.items_key, [])
        }
        
    except Exception as e:
        logger.error(f"Scraper '{req.platform}' crashed due to: {e}")
        return {
            "success": False,
            "error": str(e)
        }

@app.post("/scrape/batch/csv")
def trigger_batch_scrape(req: MultiScrapeRequest):
    logger.info(f"Incoming batch request: Platforms={len(req.platforms)}, Keywords={len(req.keywords)}, Total Limit={req.total_limit}")
    
    # Validation
    valid_platforms = [p for p in req.platforms if p in SCRAPERS]
    if not valid_platforms:
        raise HTTPException(status_code=400, detail="None of the requested platforms are valid.")
        
    start = time.time()
    
    # Dynamically distribute the "total_limit" evenly across all searches
    total_combinations = len(valid_platforms) * len(req.keywords)
    limit_per_search = max(1, req.total_limit // total_combinations)
    
    orchestrator = ScraperOrchestrator(scraper_extras=SCRAPER_EXTRAS)
    aggregated_results = orchestrator.orchestrate_batch(
        keywords=req.keywords,
        platforms=valid_platforms,
        limit_per_search=limit_per_search
    )
    
    csv_string = CSVExporter.generate_csv(aggregated_results)
    
    elapsed = time.time() - start
    logger.info(f"Batch completed in {elapsed:.2f}s. Extracted {len(aggregated_results)} total items.")
    
    return Response(
        content=csv_string,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="extraction.csv"'}
    )
