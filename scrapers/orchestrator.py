import logging
import concurrent.futures
from typing import List, Dict, Any

from scrapers.registry import get_scraper, SCRAPERS
from scrapers.base import ScraperConfig

logger = logging.getLogger(__name__)

class ScraperOrchestrator:
    """
    Manages horizontal scaling by fanning out across multiple platforms and keywords
    concurrently. Follows SOLID by separating orchestration concerns from actual
    target-oriented parsing.
    """
    
    def __init__(self, scraper_extras: dict = None):
        self.scraper_extras = scraper_extras or {}

    def _execute_single(self, platform: str, keyword: str, limit: int) -> List[Dict[str, Any]]:
        """Worker function for the ThreadPool."""
        if platform not in SCRAPERS:
            logger.warning(f"Orchestrator skipping unknown platform: {platform}")
            return []

        try:
            logger.info(f"Orchestration starting: {platform} -> '{keyword}'")
            # Pull singleton/instance mapping from registry
            scraper = get_scraper(platform)

            # Build config targeting standard contract
            config = ScraperConfig(keyword=keyword, limit=limit)
            
            extra = self.scraper_extras.get(platform)
            if extra:
                config.extra = extra

            # Execute blocking scrape cycle safely contained in this thread
            result = scraper.scrape(config)
            
            # Enforce transformation and inject search context metadata
            transformed = scraper.to_json(result)
            items = transformed.get(scraper.items_key, [])
            
            augmented_items = []
            for item in items:
                item_copy = dict(item)
                item_copy["_search_platform"] = platform
                item_copy["_search_keyword"] = keyword
                augmented_items.append(item_copy)
                
            return augmented_items
            
        except Exception as e:
            logger.error(f"Orchestrator caught handled exception in scraper '{platform}' for '{keyword}': {e}", exc_info=True)
            # Return empty list instead of crashing batch process
            return []

    def orchestrate_batch(self, keywords: List[str], platforms: List[str], limit_per_search: int) -> List[Dict[str, Any]]:
        """
        Calculates combination of keyword * platform and extracts them in parallel.
        """
        combinations = []
        for p in platforms:
            for k in keywords:
                combinations.append((p, k))

        all_results = []
        
        # Concurrency prevents 10 searches taking 10x long
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            # Map combinations to futures
            future_to_combo = {
                executor.submit(self._execute_single, combo[0], combo[1], limit_per_search): combo
                for combo in combinations
            }
            
            for future in concurrent.futures.as_completed(future_to_combo):
                combo = future_to_combo[future]
                try:
                    data = future.result()
                    all_results.extend(data)
                    logger.debug(f"Combo completed: {combo[0]} / '{combo[1]}' yielded {len(data)} items.")
                except Exception as exc:
                    logger.error(f"Combination {combo} resulted in executor crash: {exc}")

        return all_results
