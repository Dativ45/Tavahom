import os
import json
import hashlib
import re
from pathlib import Path

import feedparser
import requests

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL = os.getenv("TELEGRAM_CHAT_ID")
if not BOT_TOKEN or not CHANNEL:
    raise SystemExit("Missing GitHub Actions secrets: TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
STATE_FILE = Path("state.json")

FEEDS = [
    ("BBC فارسی", "https://feeds.bbci.co.uk/persian/rss.xml"),
    ("DW فارسی", "https://rss.dw.com/xml/rss-fa-all"),
    ("NPR World", "https://feeds.npr.org/1004/rss.xml"),
]

def clean(value):
    return re.sub(r"\\s+", " ", re.sub(r"<[^>]+>", "", value or "")).strip()

def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": []}

def save_state(state):
    state["seen"] = state.get("seen", [])[-500:]
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def fetch_items():
    items = []
    for source, url in FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:20]:
                title = clean(entry.get("title"))
                link = entry.get("link", "")
                summary = clean(entry.get("summary", ""))
                if title and link.startswith("http"):
                    key = hashlib.sha256((title + link).encode("utf-8")).hexdigest()
                    items.append({
                        "source": source,
                        "title": title,
                        "link": link,
                        "summary": summary[:1200],
                        "key": key,
                    })
        except Exception as exc:
            print(f"Feed error [{source}]: {exc}")
    return items

def make_post(item):
    if not OPENAI_KEY:
        return (
            f"📰 {item['title']}\n\n"
            f"منبع: {item['source']}\n"
            f"🔗 {item['link']}\n\n"
            "اتاق وضعیت | خبر با منبع"
        )

    prompt = (
        "برای کانال فارسی تلگرام «اتاق وضعیت» یک خلاصه کوتاه و بی‌طرف بنویس. "
        "فقط از داده‌های ارائه‌شده استفاده کن و هیچ عدد یا واقعیتی نساز. "
        "اگر اطلاعات کافی نیست، صرفاً تیتر را روشن‌تر بازنویسی کن. "
        "حداکثر ۷۰ کلمه. در پایان نام منبع و لینک را بیاور.\n\n"
        f"منبع: {item['source']}\n"
        f"عنوان: {item['title']}\n"
        f"متن خوراک: {item['summary']}\n"
        f"لینک: {item['link']}"
    )
    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_KEY}"},
        json={
            "model": OPENAI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        },
        timeout=45,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()

def publish(message):
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHANNEL,
            "text": message,
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")

def main():
    state = load_state()
    seen = set(state.get("seen", []))
    fresh = [item for item in fetch_items() if item["key"] not in seen]

    if not fresh:
        print("No new stories.")
        return

    # Publish the newest available item; source and original link are retained.
    item = fresh[0]
    publish(make_post(item))
    seen.add(item["key"])
    state["seen"] = list(seen)
    save_state(state)
    print(f"Published: {item['title']}")

if __name__ == "__main__":
    main()
