import unittest
from unittest.mock import patch
from scrapers.orchestrator import ScraperOrchestrator
from scrapers.exporter import CSVExporter
from scrapers.base import ScraperResult, ScraperConfig

class DummyScraper:
    platform = "dummy"
    items_key = "items"
    
    def scrape(self, config: ScraperConfig):
        # Simulate a crash if keyword is 'fail'
        if config.keyword == "fail":
            raise Exception("Simulated scraper runtime crash")
        
        return ScraperResult(
            query=config.keyword, 
            platform=self.platform, 
            count=1,
            items=[{
                "url": f"http://test.com/{config.keyword}", 
                "title": f"Title {config.keyword}",
                "description": "Some payload content"
            }]
        )
    
    def to_json(self, result):
        return {
            "query": result.query, 
            "platform": result.platform, 
            "count": result.count,
            self.items_key: result.items
        }

class TestArchitectureRefactor(unittest.TestCase):
    
    @patch('scrapers.orchestrator.get_scraper')
    @patch('scrapers.orchestrator.SCRAPERS', {'dummy': True})
    def test_orchestrator_multiple_keywords(self, mock_get):
        mock_get.return_value = DummyScraper()
        orch = ScraperOrchestrator()
        
        # Test executing 1 platform * 2 keywords
        results = orch.orchestrate_batch(["kw1", "kw2"], ["dummy"], limit_per_search=5)
        
        self.assertEqual(len(results), 2, "Should return results from both keywords")
        self.assertEqual(results[0]["_search_platform"], "dummy")
        
        # Context arrays (since they run in threads, order isn't guaranteed)
        returned_kws = [r["_search_keyword"] for r in results]
        self.assertIn("kw1", returned_kws)
        self.assertIn("kw2", returned_kws)

    @patch('scrapers.orchestrator.get_scraper')
    @patch('scrapers.orchestrator.SCRAPERS', {'dummy': True})
    def test_orchestrator_error_handling(self, mock_get):
        """Verifies that if one scraper fails, the others continue."""
        mock_get.return_value = DummyScraper()
        orch = ScraperOrchestrator()
        
        # "fail" will throw exception, "pass" will succeed
        results = orch.orchestrate_batch(["fail", "pass"], ["dummy"], limit_per_search=5)
        
        self.assertEqual(len(results), 1, "Should gracefully skip the failure and return the successful result")
        self.assertEqual(results[0]["_search_keyword"], "pass")
        
    def test_csv_normalization_and_dedup(self):
        # We test both heterogeneity of dicts (missing fields) AND duplication logic
        raw_items = [
            {"_search_platform": "rd", "_search_keyword": "A", "url": "duplicate_link", "title": "T1", "body": "content 1", "author": "user1"},
            {"_search_platform": "yt", "_search_keyword": "A", "url": "duplicate_link", "title": "T2"}, # Should be skipped due to same URL
            {"_search_platform": "bbc", "_search_keyword": "B", "url": "unique", "title": "T3", "channel": "admin"} # channel maps to author
        ]
        
        csv_str = CSVExporter.generate_csv(raw_items)
        lines = csv_str.strip().split('\n')
        
        # Assert format
        self.assertEqual(len(lines), 3, "Header + 2 unique rows (one duplicate dropped)")
        self.assertTrue(lines[0].startswith("platform,keyword,title,content,author,url,date,language,sentiment,source"))
        
        # Row 1 (mapped 'body' -> 'content', 'author' -> 'author')
        self.assertIn("rd,A,T1,content 1,user1,duplicate_link", lines[1].replace("\r", ""))
        
        # Row 2 (mapped 'channel' -> 'author')
        self.assertIn("bbc,B,T3,,admin,unique", lines[2].replace("\r", ""))

if __name__ == '__main__':
    unittest.main()
