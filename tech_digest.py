import os
import json
import re
import html
import hashlib
import time
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests

BOT = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")
KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
STATE = Path("tech_state.json")
MAX_SEEN = 3000

FEEDS = [
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("TechCrunch", "https://techcrunch.com/feed/"),
    ("Engadget", "https://www.engadget.com/rss.xml"),
    ("Android Authority", "https://www.androidauthority.com/feed/"),
    ("GSMArena", "https://www.gsmarena.com/rss-news-reviews.php3"),
    ("Wired", "https://www.wired.com/feed/rss"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("9to5Google", "https://9to5google.com/feed/"),
]
HEADERS = {"User-Agent": "StatusRoomNewsBot/1.0 (+https://t.me/StatusRoomNews)"}


def clean(value):
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or ""))).strip()


def image_url(entry):
    candidates = []
    for key in ("media_content", "media_thumbnail"):
        for item in entry.get(key, []) or []:
            if isinstance(item, dict):
                candidates.append(item.get("url") or item.get("href"))
    for item in entry.get("enclosures", []) or []:
        if isinstance(item, dict) and (item.get("type", "").startswith("image/") or re.search(r"\.(jpg|jpeg|png|webp)(?:\?|$)", item.get("href", ""), re.I)):
            candidates.append(item.get("href"))
    for link in entry.get("links", []) or []:
        if isinstance(link, dict) and (link.get("type", "").startswith("image/") or link.get("rel") == "enclosure"):
            candidates.append(link.get("href"))
    raw = entry.get("summary", "") or entry.get("description", "")
    candidates.extend(re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', raw, re.I))
    for candidate in candidates:
        if not candidate:
            continue
        candidate = candidate.strip()
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        parsed = urlparse(candidate)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return candidate
    return None


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
    for source, url in FEEDS:
        try:
            feed = feedparser.parse(url, request_headers=HEADERS)
            if getattr(feed, "bozo", False):
                print(f"Feed warning [{source}]: {str(getattr(feed, 'bozo_exception', 'invalid feed'))[:160]}")
            for entry in feed.entries[:30]:
                title = clean(entry.get("title"))
                link = entry.get("link", "")
                if not title or not link.startswith(("http://", "https://")):
                    continue
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                timestamp = int(time.mktime(published)) if published else 0
                # Avoid stale feed backlog; undated items remain eligible once.
                if timestamp and time.time() - timestamp > 72 * 3600:
                    continue
                key = hashlib.sha256((title + link).encode("utf-8")).hexdigest()
                items.append({
                    "source": source,
                    "title": title[:450],
                    "summary": clean(entry.get("summary", entry.get("description", "")))[:2200],
                    "link": link,
                    "image": image_url(entry),
                    "timestamp": timestamp,
                    "key": key,
                })
        except Exception as exc:
            print(f"Feed error [{source}]: {str(exc)[:200]}")
    return items


def translate(item):
    prompt = (
        "You are the technology editor of a professional bilingual world-news channel. "
        "Return valid JSON only with keys title_fa, summary_fa, category. "
        "Translate into natural Iranian Persian. Summarize faithfully; never invent specs, "
        "prices, release dates, test results, or claims absent from the supplied text. "
        "Choose category from: گوشی هوشمند، تبلت، گجت و پوشیدنی، هوش مصنوعی، نرم‌افزار، "
        "سخت‌افزار و تراشه، امنیت سایبری، فناوری مصرفی. If the source excerpt lacks details, "
        "say so plainly. Keep summary to 2-3 concise sentences.\n"
        + json.dumps({"source": item["source"], "headline": item["title"], "excerpt": item["summary"]}, ensure_ascii=False)
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    response = requests.post(
        url,
        headers={"x-goog-api-key": KEY, "Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json", "maxOutputTokens": 700},
        },
        timeout=45,
    )
    if not response.ok:
        try:
            detail = response.json().get("error", {}).get("message", "Gemini request failed")
        except ValueError:
            detail = "Gemini request failed"
        raise RuntimeError(f"Gemini API error ({response.status_code}): {str(detail)[:300]}")
    parts = response.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
    raw = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    data = json.loads(raw)
    category = clean(str(data.get("category", "فناوری مصرفی")))
    allowed = {"گوشی هوشمند", "تبلت", "گجت و پوشیدنی", "هوش مصنوعی", "نرم‌افزار", "سخت‌افزار و تراشه", "امنیت سایبری", "فناوری مصرفی"}
    if category not in allowed:
        category = "فناوری مصرفی"
    title = clean(str(data.get("title_fa", ""))) or item["title"]
    summary = clean(str(data.get("summary_fa", ""))) or "خوراک خبری جزئیات بیشتری ارائه نکرده است؛ برای اطلاعات کامل به گزارش اصلی مراجعه کنید."
    return title[:320], summary[:900], category


def footer():
    configured = os.getenv("STATUS_ROOM_CHANNEL_URL", "").strip()
    if configured.startswith("https://t.me/"):
        link = configured
    elif str(CHAT).startswith("@"):
        link = "https://t.me/" + str(CHAT)[1:]
    else:
        return ""
    return '\n\n📣 <a href="' + html.escape(link, quote=True) + '">عضویت در اتاق وضعیت</a>'


def post(item, title_fa, summary_fa, category):
    timestamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(item["timestamp"])) if item["timestamp"] else "زمان انتشار در خوراک مشخص نیست"
    caption = (
        "📱 <b>اتاق وضعیت | فناوری جهان</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🏷 <b>{html.escape(category)}</b>\n\n"
        f"🇮🇷 <b>{html.escape(title_fa)}</b>\n"
        f"{html.escape(summary_fa)}\n\n"
        "🌐 <b>Original (English)</b>\n"
        f"{html.escape(item['title'][:500])}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🗞 منبع: {html.escape(item['source'])}\n"
        f"🕒 زمان خوراک: {html.escape(timestamp)}\n"
        f"🔗 <a href=\"{html.escape(item['link'], quote=True)}\">گزارش کامل و مشخصات اصلی</a>"
        + footer()
    )
    # Telegram captions are limited to 1024 characters after entity parsing.
    caption = caption[:1000]
    if item.get("image"):
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{BOT}/sendPhoto",
                json={"chat_id": CHAT, "photo": item["image"], "caption": caption, "parse_mode": "HTML"},
                timeout=40,
            )
            payload = response.json()
            if response.ok and payload.get("ok"):
                return payload.get("result", {}).get("message_id"), True
            print("Photo delivery unavailable; falling back to text:", str(payload.get("description", payload))[:250])
        except (requests.RequestException, ValueError) as exc:
            print("Photo delivery error; falling back to text:", str(exc)[:200])
    text_message = caption + "\n\n🖼 تصویر در خوراک منبع موجود نبود یا تلگرام نتوانست آن را دریافت کند."
    response = requests.post(
        f"https://api.telegram.org/bot{BOT}/sendMessage",
        json={"chat_id": CHAT, "text": text_message, "parse_mode": "HTML", "disable_web_page_preview": False},
        timeout=35,
    )
    payload = response.json()
    if not response.ok or not payload.get("ok"):
        raise RuntimeError("Telegram send failed: " + str(payload.get("description", payload))[:300])
    return payload.get("result", {}).get("message_id"), False


def main():
    if not all([BOT, CHAT, KEY]):
        raise SystemExit("Missing Telegram or Gemini secrets.")
    state = load_state()
    seen = set(state.get("seen", []))
    items = [item for item in collect() if item["key"] not in seen]
    if not items:
        print("No new global technology stories.")
        return
    items.sort(key=lambda item: item["timestamp"], reverse=True)
    # One carefully selected story per run; the workflow runs every three hours.
    item = items[0]
    title_fa, summary_fa, category = translate(item)
    message_id, with_image = post(item, title_fa, summary_fa, category)
    seen.add(item["key"])
    state["seen"] = list(seen)
    save_state(state)
    print(f"Published technology story message_id={message_id}, image={with_image}, source={item['source']}, title={item['title']}")


if __name__ == "__main__":
    main()
