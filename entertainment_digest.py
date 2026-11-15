import os
import json
import re
import html
import hashlib
import time
import calendar
from pathlib import Path

import feedparser
import requests

BOT = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
STATE = Path("entertainment_state.json")
MAX_AGE = 72 * 3600
MAX_SEEN = 2500
HEADERS = {"User-Agent": "StatusRoomCultureDesk/1.0 (+https://github.com/Dativ45/Tavahom)"}

# Editorial entertainment and culture desks only. No gossip forums or paparazzi feeds.
FEEDS = [
    ("Variety", "https://variety.com/feed/"),
    ("The Hollywood Reporter", "https://www.hollywoodreporter.com/feed/"),
    ("Deadline", "https://deadline.com/feed/"),
    ("BBC Culture", "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml"),
    ("NPR Movies", "https://feeds.npr.org/1045/rss.xml"),
]
RUMOR_TERMS = re.compile(
    r"rumou?r|unconfirmed|alleged(ly)?|speculat(e|ion)|sources claim|"
    r"insider claims|dating rumou?r|paparazzi|blind item",
    re.I,
)
LOW_SIGNAL = re.compile(
    r"horoscope|quiz|style tips|what to watch tonight|daily puzzle|"
    r"box office prediction|trailer reaction",
    re.I,
)


def clean(value):
    return html.unescape(re.sub(r"\\s+", " ", re.sub(r"<[^>]+>", " ", value or ""))).strip()


def load_state():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"seen": []}
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": []}


def save_state(state):
    state["seen"] = state.get("seen", [])[-MAX_SEEN:]
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def collect():
    items = []
    now = time.time()
    for source, url in FEEDS:
        try:
            response = requests.get(url, headers=HEADERS, timeout=18)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            if getattr(feed, "bozo", False):
                print(f"Feed warning [{source}]: {str(getattr(feed, 'bozo_exception', 'invalid feed'))[:140]}")
            for entry in feed.entries[:35]:
                title = clean(entry.get("title"))
                link = entry.get("link", "")
                summary = clean(entry.get("summary", entry.get("description", "")))[:1800]
                if not title or not link.startswith(("http://", "https://")):
                    continue
                if LOW_SIGNAL.search(title) or RUMOR_TERMS.search(title + " " + summary):
                    continue
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                timestamp = calendar.timegm(published) if published else 0
                if timestamp and (now - timestamp > MAX_AGE or timestamp > now + 3600):
                    continue
                key = hashlib.sha256((title.lower() + link).encode("utf-8")).hexdigest()
                items.append({
                    "source": source, "title": title[:400], "summary": summary,
                    "link": link, "timestamp": timestamp, "key": key,
                })
        except Exception as exc:
            print(f"Feed error [{source}]: {str(exc)[:180]}")
    return items


def translate(item):
    prompt = (
        "You are the careful culture and entertainment editor of a Persian news channel. "
        "Return valid JSON only with keys publish, title_fa, summary_fa, category. "
        "Set publish=false for gossip, rumors, anonymous allegations, paparazzi speculation, "
        "unverified personal-life claims, clickbait, trivial promotional items, or stories "
        "that are not materially about film, television, music, books, festivals, awards, "
        "or widely relevant popular culture. Do not infer misconduct or private facts. "
        "For factual industry reporting, summarize only the supplied excerpt and never invent "
        "release dates, cast, box-office figures, award results or quotations. "
        "Use natural Iranian Persian, neutral tone and 2 concise sentences. "
        "Choose category from: سینما، تلویزیون و سریال، موسیقی، جشنواره و جوایز، "
        "فرهنگ عامه، کتاب و هنر.\n"
        + json.dumps({"source": item["source"], "headline": item["title"], "excerpt": item["summary"]}, ensure_ascii=False)
    )
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    response = requests.post(
        endpoint, headers={"x-goog-api-key": KEY, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json", "maxOutputTokens": 700}},
        timeout=45,
    )
    if not response.ok:
        raise RuntimeError(f"Gemini API {response.status_code}: {response.text[:250]}")
    parts = response.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
    raw = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    data = json.loads(raw)
    if data.get("publish") is False:
        return None
    allowed = {"سینما", "تلویزیون و سریال", "موسیقی", "جشنواره و جوایز", "فرهنگ عامه", "کتاب و هنر"}
    category = clean(str(data.get("category", "")))
    if category not in allowed:
        category = "فرهنگ عامه"
    title = clean(str(data.get("title_fa", ""))) or item["title"]
    summary = clean(str(data.get("summary_fa", ""))) or "خوراک منبع جزئیات کافی برای خلاصه‌سازی ارائه نکرده است."
    return title[:260], summary[:850], category


def footer():
    configured = os.getenv("STATUS_ROOM_CHANNEL_URL", "").strip()
    if configured.startswith("https://t.me/"):
        link = configured
    elif CHAT.startswith("@"):
        link = "https://t.me/" + CHAT[1:]
    else:
        return ""
    return '\\n\\n📣 <a href="' + html.escape(link, quote=True) + '">عضویت در اتاق وضعیت</a>'


def publish(item, translated):
    title_fa, summary_fa, category = translated
    timestamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(item["timestamp"])) if item["timestamp"] else "در خوراک ثبت نشده"
    text = (
        "🎬 <b>اتاق وضعیت | سرگرمی و فرهنگ</b>\\n"
        "━━━━━━━━━━━━━━━━━━\\n"
        f"🏷 <b>{html.escape(category)}</b>\\n\\n"
        f"🇮🇷 <b>{html.escape(title_fa)}</b>\\n"
        f"{html.escape(summary_fa)}\\n\\n"
        "🌐 <b>Original (English)</b>\\n"
        f"{html.escape(item['title'][:220])}"
        + (("\\n" + html.escape(item["summary"][:500])) if item["summary"] else "")
        + "\\n\\n━━━━━━━━━━━━━━━━━━\\n"
        + f"🗞 منبع: {html.escape(item['source'])}\\n"
        + f"🕒 زمان خوراک: {html.escape(timestamp)}\\n"
        + f'🔗 <a href="{html.escape(item["link"], quote=True)}">گزارش اصلی</a>'
        + footer()
    )
    response = requests.post(
        f"https://api.telegram.org/bot{BOT}/sendMessage",
        json={"chat_id": CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False},
        timeout=35,
    )
    payload = response.json()
    if not response.ok or not payload.get("ok"):
        raise RuntimeError("Telegram send failed: " + str(payload.get("description", payload))[:250])
    return payload.get("result", {}).get("message_id")


def main():
    if not all([BOT, CHAT, KEY]):
        raise SystemExit("Missing Telegram/Gemini secrets.")
    state = load_state()
    seen = set(state.get("seen", []))
    items = [item for item in collect() if item["key"] not in seen]
    items.sort(key=lambda x: x["timestamp"], reverse=True)
    if not items:
        print("No fresh entertainment or culture stories.")
        return
    # Scan a few candidates so a low-signal item does not block the desk.
    for item in items[:8]:
        translated = translate(item)
        seen.add(item["key"])
        if translated is None:
            continue
        message_id = publish(item, translated)
        state["seen"] = list(seen)
        save_state(state)
        print(f"Published culture story message_id={message_id}, source={item['source']}, title={item['title']}")
        return
    state["seen"] = list(seen)
    save_state(state)
    print("No candidate passed the culture desk editorial filter.")


if __name__ == "__main__":
    main()
