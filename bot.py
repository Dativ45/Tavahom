import os
import json
import hashlib
import re
import time
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
CONFIG_FILE = Path("editorial_config.json")

def signature_for(item):
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        config = {"personas": []}
    text = (item.get("title", "") + " " + item.get("summary", "")).lower()
    rules = [
        ("ورزش", ["فوتبال", "ورزش", "لیگ", "جام", "بازیکن", "تیم ملی", "بسکتبال", "والیبال"]),
        ("اقتصاد", ["اقتصاد", "تورم", "بورس", "ارز", "دلار", "طلا", "بانک", "نفت", "قیمت", "بازار"]),
        ("فناوری", ["فناوری", "هوش مصنوعی", "اینترنت", "موبایل", "گوشی", "اپلیکیشن", "ربات", "تراشه", "سایبری"]),
        ("سیاست", ["انتخابات", "پارلمان", "دولت", "رئیس جمهور", "وزیر", "مجلس", "تحریم", "سیاست"]),
        ("فرهنگ و جامعه", ["سینما", "فیلم", "موسیقی", "کتاب", "هنر", "فرهنگ", "جامعه"]),
    ]
    desk = next((name for name, words in rules if any(word in text for word in words)), "بین‌الملل")
    alias = next((p["alias"] for p in config.get("personas", []) if p.get("desk") == desk), "سایه‌نویس")
    return f"\n\n— {alias} | اتاق وضعیت"

FEEDS = [
    ("BBC فارسی", "https://feeds.bbci.co.uk/persian/rss.xml"),
    ("DW فارسی", "https://rss.dw.com/xml/rss-fa-all"),
    ("NPR World", "https://feeds.npr.org/1004/rss.xml"),
]

def clean(value):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", value or "")).strip()

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
                    published = entry.get("published_parsed") or entry.get("updated_parsed")
                    published_ts = int(time.mktime(published)) if published else 0
                    key = hashlib.sha256((title + link).encode("utf-8")).hexdigest()
                    items.append({
                        "source": source,
                        "title": title,
                        "link": link,
                        "summary": summary[:1200],
                        "published_ts": published_ts,
                        "key": key,
                    })
        except Exception as exc:
            print(f"Feed error [{source}]: {exc}")
    return items

def make_post(item):
    signature = signature_for(item)
    if not OPENAI_KEY:
        excerpt = item["summary"][:650].strip()
        body = f"📰 {item['title']}\n\n"
        if excerpt:
            body += excerpt + "\n\n"
        return (
            body
            + f"منبع: {item['source']}\n"
            + f"🔗 {item['link']}"
            + signature
        )

    prompt = (
        "برای کانال فارسی تلگرام «اتاق وضعیت» یک خلاصه کوتاه و بی‌طرف بنویس. "
        "فقط از داده‌های ارائه‌شده استفاده کن و هیچ عدد یا واقعیتی نساز. "
        "اگر اطلاعات کافی نیست، صرفاً تیتر را روشن‌تر بازنویسی کن. "
        "حداکثر ۷۰ کلمه. لحن حرفه‌ای و روشن باشد. "
        "نام مستعار فقط امضای تحریریه است و منبع خبر محسوب نمی‌شود.\n\n"
        f"منبع: {item['source']}\n"
        f"عنوان: {item['title']}\n"
        f"متن خوراک: {item['summary']}\n"
        f"لینک: {item['link']}"
    )
    try:
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
        summary = response.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        print(f"AI summary unavailable; using feed text: {exc}")
        summary = item["summary"][:650] or item["title"]

    return (
        summary[:2600]
        + f"\n\nمنبع: {item['source']}\n🔗 {item['link']}"
        + signature
    )

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
    try:
        payload = response.json()
    except ValueError:
        payload = {"description": response.text[:1000]}
    if not response.ok or not payload.get("ok"):
        description = payload.get("description", payload)
        raise RuntimeError(
            f"Telegram API error ({response.status_code}): {description}"
        )

def main():
    state = load_state()
    seen = set(state.get("seen", []))
    fresh = [item for item in fetch_items() if item["key"] not in seen]

    if not fresh:
        print("No new stories.")
        return

    # Choose the newest dated item across all feeds, not simply the first feed.
    fresh.sort(key=lambda item: item.get("published_ts", 0), reverse=True)
    item = fresh[0]
    publish(make_post(item))
    seen.add(item["key"])
    state["seen"] = list(seen)
    save_state(state)
    print(f"Published: {item['title']}")

if __name__ == "__main__":
    main()
