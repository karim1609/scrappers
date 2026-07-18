import csv
import io
from typing import List, Dict, Any

class CSVExporter:
    """
    Handles CSV transformation to normalize mixed schema results from disparate 
    scrapers into one standardized CSV projection.
    """
    
    COLUMNS = [
        "platform",
        "keyword",
        "title",
        "content",
        "author",
        "url",
        "date",
        "language",
        "sentiment",
        "source"
    ]

    @classmethod
    def normalize_item(cls, platform: str, keyword: str, item: Dict[str, Any]) -> Dict[str, Any]:
        """Maps specific payload keys to rigid columns, leaning heavily on fallbacks."""
        return {
            "platform": platform,
            "keyword": keyword,
            "title": item.get("title", ""),
            "content": item.get("content") or item.get("description") or item.get("body") or item.get("text") or "",
            "author": item.get("author") or item.get("channel") or item.get("username") or "",
            "url": item.get("url") or item.get("link") or "",
            "date": item.get("published") or item.get("date") or item.get("created_time") or item.get("published_at") or "",
            "language": item.get("language", ""),
            "sentiment": item.get("sentiment", ""),
            "source": item.get("source", "")
        }

    @classmethod
    def generate_csv(cls, results_pool: List[Dict[str, Any]]) -> str:
        """
        Receives combinations of outputs (augmented with context) and returns a CSV string.
        """
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=cls.COLUMNS, extrasaction='ignore', strict=False)
        writer.writeheader()

        seen_urls = set()

        for res in results_pool:
            platform = res.get("_search_platform", "")
            keyword = res.get("_search_keyword", "")
            norm = cls.normalize_item(platform, keyword, res)
            
            # Simple Deduplication strategy
            url = norm["url"]
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
                
            writer.writerow(norm)

        return output.getvalue()
