"""Shared conservative HTTP POST retry helper."""
import time
from urllib.parse import urlsplit

import requests

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def post_with_retry(url, attempts=3, **kwargs):
    """Retry transient POST responses without risking duplicate Telegram sends.

    Telegram 429 responses are explicit rate-limit rejections and can be retried
    after retry_after. Telegram 5xx responses are treated as ambiguous: the API
    may have accepted a send before the upstream error was returned, so never
    resend them automatically. Network exceptions are also never retried.
    """
    host = (urlsplit(url).hostname or "").lower()
    is_telegram = host == "api.telegram.org"
    retryable = {429} if is_telegram else RETRYABLE_STATUS_CODES

    last_response = None
    for attempt in range(1, max(1, attempts) + 1):
        response = requests.post(url, **kwargs)
        last_response = response
        if response.status_code not in retryable or attempt == attempts:
            if is_telegram and response.status_code in {500, 502, 503, 504}:
                print(
                    f"Telegram HTTP {response.status_code}; not retrying because delivery may be ambiguous."
                )
            return response

        delay = min(5 * attempt, 120)
        if response.status_code == 429:
            try:
                delay = int(response.json().get("parameters", {}).get("retry_after", delay))
            except (ValueError, TypeError, AttributeError):
                pass
        delay = max(1, min(delay, 120))
        print(f"HTTP {response.status_code}; retrying POST in {delay}s ({attempt}/{attempts}).")
        time.sleep(delay)

    return last_response
