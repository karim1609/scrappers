import os
import unittest
from unittest.mock import MagicMock, patch
import requests

from scrapers.utils.env import get_required_env
from scrapers.utils.http import request_with_retry, is_retryable_status, exponential_backoff

class TestUtils(unittest.TestCase):
    
    def test_env_utils_success(self):
        os.environ["MOCK_API_KEY"] = "supersecret"
        val = get_required_env("MOCK_API_KEY")
        self.assertEqual(val, "supersecret")
        
    def test_env_utils_failure(self):
        if "MISSING_KEY" in os.environ:
            del os.environ["MISSING_KEY"]
            
        with self.assertRaises(RuntimeError) as context:
            get_required_env("MISSING_KEY", hint="Go to example.com")
            
        err = str(context.exception)
        self.assertIn("MISSING_KEY is not set", err)
        self.assertIn("Go to example.com", err)

    def test_http_retryable_codes(self):
        self.assertTrue(is_retryable_status(429))
        self.assertTrue(is_retryable_status(502))
        self.assertFalse(is_retryable_status(200))
        self.assertFalse(is_retryable_status(401))
        
    def test_http_exponential_backoff(self):
        self.assertEqual(exponential_backoff(0), 1)
        self.assertEqual(exponential_backoff(1), 2)
        self.assertEqual(exponential_backoff(2), 4)

    @patch('time.sleep')
    def test_request_with_retry_timeout_recovery(self, mock_sleep):
        mock_method = MagicMock()
        
        # We mock the sequence: 1 timeout, then 1 success
        mock_resp = requests.Response()
        mock_resp.status_code = 200
        mock_method.side_effect = [requests.Timeout("Oops"), mock_resp]
        
        result = request_with_retry(mock_method, "http://test.com")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(mock_method.call_count, 2, "Should have retried exactly once after timeout")

    @patch('time.sleep')
    def test_request_with_retry_401_no_retry(self, mock_sleep):
        mock_method = MagicMock()
        mock_resp = requests.Response()
        mock_resp.status_code = 401
        
        # 401 is NOT a retryable status. It should return instantly so the scraper handles auth logic.
        mock_method.return_value = mock_resp
        
        result = request_with_retry(mock_method, "http://test.com", max_retries=3)
        self.assertEqual(result.status_code, 401)
        self.assertEqual(mock_method.call_count, 1, "Should NOT retry on 401")

if __name__ == '__main__':
    unittest.main()
