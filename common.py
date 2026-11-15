"""Shared conservative HTTP helpers for Status Room publishers."""
import html
import time
from urllib.parse import urlsplit

import requests

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def safe_channel_footer(bot_token, chat_id, configured_url=""):
    """Return an optional footer link; Telegram lookup failures never block posts."""
    configured_url = (configured_url or "").strip()
    if configured_url.startswith("https://t.me/"):
        link = configured_url
    elif str(chat_id or "").startswith("@"):
        link = "https://t.me/" + str(chat_id)[1:]
    elif bot_token and chat_id:
        try:
            response = requests.get(
                f"https://api.telegram.org/bot{bot_token}/getChat",
                params={"chat_id": chat_id},
                timeout=5,
            )
            payload = response.json()
            if not response.ok or not payload.get("ok"):
                print("WARNING: getChat could not resolve optional channel footer.")
                return ""
            chat = payload.get("result", {})
            username = chat.get("username")
            link = "https://t.me/" + username if username else (chat.get("invite_link") or "")
        except (requests.RequestException, ValueError) as exc:
            print(f"WARNING: optional channel footer lookup failed: {type(exc).__name__}")
            return ""
    else:
        return ""

    if not link.startswith("https://t.me/"):
        print("WARNING: no channel URL available; publishing without optional footer.")
        return ""
    return (
        '\n\n📣 <b>دنبال‌کردن اتاق وضعیت:</b> '
        '<a href="' + html.escape(link, quote=True) + '">عضویت در کانال</a>'
    )


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
