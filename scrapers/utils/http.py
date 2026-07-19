import sys
import time
import requests
from typing import Callable, Optional

# Standard retryable HTTP status codes (Rate limits & Server faults)
RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

def is_retryable_status(status_code: int) -> bool:
    """Returns True if the HTTP status indicates a temporary fault."""
    return status_code in RETRY_STATUS_CODES

def exponential_backoff(attempt: int) -> int:
    """Calculates backoff duration (2^attempt)"""
    return 2 ** attempt

def build_session() -> requests.Session:
    """Creates a fresh requests Session for connection pooling."""
    return requests.Session()

def request_with_retry(
    method: Callable,
    url: str,
    max_retries: int = 5,
    timeout: int = 30,
    **kwargs
) -> requests.Response:
    """
    Executes a network request (GET, POST) using an explicit callable with automatic
    exponential backoff for Timeouts and Retryable HTTP exact status codes.
    
    IMPORTANT: This utility intentionally does NOT parse JSON, does NOT throw 
    on 401/403/404, and does NOT manipulate authentication headers.
    The caller (Scraper) remains 100% responsible for business logic.
    """
    last_error: Optional[Exception] = None
    
    # Guarantee timeout is injected
    if "timeout" not in kwargs:
        kwargs["timeout"] = timeout
        
    for attempt in range(max_retries):
        try:
            response = method(url, **kwargs)
            
        except requests.Timeout as exc:
            last_error = exc
            wait = exponential_backoff(attempt)
            print(f"[HTTP Utils] Timeout — retrying {url} in {wait}s ({attempt + 1}/{max_retries})", file=sys.stderr)
            time.sleep(wait)
            continue
            
        except requests.RequestException as exc:
            raise RuntimeError(f"Network error on {url}: {exc}") from exc

        # Check explicitly generic rate limiting / temporary issues
        if is_retryable_status(response.status_code):
            wait = exponential_backoff(attempt)
            print(
                f"[HTTP Utils] {response.status_code} — retrying {url} in {wait}s "
                f"({attempt + 1}/{max_retries})", 
                file=sys.stderr
            )
            time.sleep(wait)
            continue
            
        # Success (2xx) or definitive Client Fatal (400, 401, etc). Return immediately.
        return response

    if last_error:
        raise RuntimeError(f"Request to {url} timed out perpetually after {max_retries} attempts.") from last_error
    raise RuntimeError(f"Request to {url} failed perpetually after {max_retries} attempts due to temporary HTTP blocks.")
