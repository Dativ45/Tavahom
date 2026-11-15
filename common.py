"""Shared conservative HTTP POST retry helper."""
import time
import requests
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
def post_with_retry(url, attempts=3, **kwargs):
    """Retry transient HTTP responses, never timeout exceptions (duplicate-send risk)."""
    last_response = None
    for attempt in range(1, attempts + 1):
        response = requests.post(url, **kwargs)
        last_response = response
        if response.status_code not in RETRYABLE_STATUS_CODES or attempt == attempts:
            return response
        delay = min(5 * attempt, 120)
        if response.status_code == 429:
            try: delay = int(response.json().get("parameters", {}).get("retry_after", delay))
            except (ValueError, TypeError, AttributeError): pass
        delay = max(1, min(delay, 120))
        print(f"HTTP {response.status_code}; retrying POST in {delay}s ({attempt}/{attempts}).")
        time.sleep(delay)
    return last_response
