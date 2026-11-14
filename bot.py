import os
import json
import hashlib
import html
import re
import time
from pathlib import Path
from difflib import SequenceMatcher
from urllib.parse import urlparse

import feedparser
import requests

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL = os.getenv("TELEGRAM_CHAT_ID")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
STATE_FILE = Path("state.json")
CONFIG_FILE = Path("editorial_config.json")
MAX_POSTS_PER_RUN = 1
MAX_SEEN = 5000

if not BOT_TOKEN or not CHANNEL:
    raise SystemExit("Missing GitHub Actions secrets: TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID")
if not OPENAI_KEY:
    raise SystemExit("Missing OPENAI_API_KEY: bilingual publishing requires translation.")

# A broad source mix: international newsrooms and primary institutional feeds.
FEEDS = [
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "media"),
    ("Deutsche Welle", "https://rss.dw.com/rdf/rss-en-all", "media"),
    ("NPR World", "https://feeds.npr.org/1004/rss.xml", "media"),
    ("The Guardian World", "https://www.theguardian.com/world/rss", "media"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "media"),
    ("France 24", "https://www.france24.com/en/rss", "media"),
    ("UN News", "https://news.un.org/feed/subscribe/en/news/all/rss.xml", "official"),
    ("WHO", "https://www.who.int/rss-feeds/news-english.xml", "official"),
    ("NASA", "https://www.nasa.gov/feed/", "official"),
    ("European Central Bank", "https://www.ecb.europa.eu/rss/press.html", "official"),
    ("World Bank", "https://www.worldbank.org/en/news/all?format=rss", "official"),
]

DESK_ALIASES = {
    "ورزش": "ضربان",
    "سیاست": "آینه",
    "اقتصاد": "تراز",
    "فناوری": "پیکسل",
    "فرهنگ و جامعه": "ماه‌رخ",
    "علم و سلامت": "پیکسل",
    "محیط زیست": "اطلس",
    "بین‌الملل": "اطلس",
}

def clean(value):
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", value or ""))).strip()

def load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("seen", [])
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"seen": []}

def save_state(state):
    state["seen"] = state.get("seen", [])[-MAX_SEEN:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def fetch_items():
    items = []
    for source, url, source_type in FEEDS:
        try:
            feed = feedparser.parse(url)
            if getattr(feed, "bozo", False):
                print(f"Feed warning [{source}]: {getattr(feed, 'bozo_exception', 'invalid feed')}")
            for entry in feed.entries[:30]:
                title = clean(entry.get("title"))
                link = entry.get("link", "")
                summary = clean(entry.get("summary", entry.get("description", "")))
                if not title or not link.startswith("http"):
                    continue
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                published_ts = int(time.mktime(published)) if published else 0
                key = hashlib.sha256((title + link).encode("utf-8")).hexdigest()
                items.append({
                    "source": source,
                    "source_type": source_type,
                    "domain": urlparse(link).netloc.lower().removeprefix("www."),
                    "title": title[:500],
                    "link": link,
                    "summary": summary[:2600],
                    "published_ts": published_ts,
                    "key": key,
                })
        except Exception as exc:
            print(f"Feed error [{source}]: {exc}")
    return items

def related_source_count(item, all_items):
    words = lambda s: {w for w in re.findall(r"[a-z0-9]{4,}", s.lower())}
    base = words(item["title"])
    domains = {item["domain"]}
    for other in all_items:
        if other["key"] == item["key"] or other["domain"] == item["domain"]:
            continue
        other_words = words(other["title"])
        if not base or not other_words:
            continue
        overlap = len(base & other_words) / max(1, min(len(base), len(other_words)))
        similarity = SequenceMatcher(None, item["title"].lower(), other["title"].lower()).ratio()
        if overlap >= 0.55 or similarity >= 0.62:
            domains.add(other["domain"])
    return len(domains)

def load_personas():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("personas", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def translate_and_classify(item):
    prompt = (
        "You are the bilingual desk editor of Status Room. Return valid JSON only with keys "
        "title_fa, translation_fa, summary_fa, desk. Translate the supplied English headline "
        "and feed excerpt faithfully into natural, professional Iranian Persian. Do not add facts, "
        "quotes, causes, numbers, or certainty absent from the source. translation_fa must be a "
        "faithful Persian translation of the supplied excerpt, not an invented full-article translation. "
        "summary_fa is a concise 1-2 sentence Persian digest. Choose exactly one desk from: "
        "ورزش، سیاست، اقتصاد، فناوری، فرهنگ و جامعه، علم و سلامت، محیط زیست، بین‌الملل. "
        "If the excerpt is empty, say that the source feed provided only a headline.\n\n"
        + json.dumps({"source": item["source"], "headline": item["title"], "excerpt": item["summary"]}, ensure_ascii=False)
    )
    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_KEY}"},
        json={
            "model": OPENAI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        },
        timeout=60,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    data = json.loads(content)
    allowed = set(DESK_ALIASES)
    if data.get("desk") not in allowed:
        data["desk"] = "بین‌الملل"
    for field in ("title_fa", "translation_fa", "summary_fa"):
        data[field] = clean(str(data.get(field, "")))[:2600]
    if not data["title_fa"]:
        data["title_fa"] = item["title"]
    if not data["translation_fa"]:
        data["translation_fa"] = "متن قابل ترجمه در خوراک منبع موجود نبود."
    if not data["summary_fa"]:
        data["summary_fa"] = data["translation_fa"][:500]
    return data

def build_post(item, translated, source_count):
    alias = DESK_ALIASES.get(translated["desk"], "اطلس")
    if source_count >= 2:
        status = "🔎 <b>چندمنبعی | در حال بررسی تحریریه</b>"
        status_note = f"بازتاب مشابه در {source_count} دامنه مستقلِ خوراک‌ها پیدا شد؛ این به‌تنهایی تأیید قطعی خبر نیست."
    elif item["source_type"] == "official":
        status = "⚠️ <b>تأییدنشده | در انتظار بررسی مستقل</b>"
        status_note = "منبع منتشرکننده رسمی/نهادی است، اما تأیید مستقل کافی در خوراک‌های بررسی‌شده پیدا نشد."
    else:
        status = "⚠️ <b>تأییدنشده | در حال بررسی منابع</b>"
        status_note = "فعلاً فقط یک منبع در خوراک‌های بررسی‌شده دیده شده است؛ خبر قطعی تلقی نشود."
    published = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(item["published_ts"])) if item["published_ts"] else "زمان انتشار در خوراک مشخص نیست"
    original = item["title"]
    if item["summary"]:
        original += "\n\n" + item["summary"][:900]
    return (
        "🛰️ <b>اتاق وضعیت | گزارش بین‌الملل</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        + status + "\n"
        + html.escape(status_note) + "\n\n"
        + "🇮🇷 <b>ترجمه فارسی</b>\n"
        + "<b>" + html.escape(translated["title_fa"][:350]) + "</b>\n"
        + html.escape(translated["translation_fa"][:1350]) + "\n\n"
        + "🌐 <b>Original (English)</b>\n"
        + "<b>" + html.escape(original[:850]) + "</b>\n\n"
        + "🧭 <b>یادداشت تحریریه</b>\n"
        + html.escape(translated["summary_fa"][:450]) + "\n\n"
        + "━━━━━━━━━━━━━━━━━━\n"
        + f"🗞 منبع: {html.escape(item['source'])}\n"
        + f"🕒 زمان منبع: {html.escape(published)}\n"
        + f"🔗 <a href=\"{html.escape(item['link'], quote=True)}\">مشاهده منبع اصلی</a>\n\n"
        + f"— <b>{html.escape(alias)}</b> | تحریریه {html.escape(translated['desk'])}\n"
        + "<i>اتاق وضعیت؛ نام‌ها پنهان می‌مانند، حقیقت نه.</i>"
    )

def publish(message):
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHANNEL,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=35,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"description": response.text[:1000]}
    if not response.ok or not payload.get("ok"):
        raise RuntimeError(f"Telegram API error ({response.status_code}): {payload.get('description', payload)}")

def main():
    state = load_state()
    seen = set(state.get("seen", []))
    all_items = fetch_items()
    fresh = [item for item in all_items if item["key"] not in seen]
    if not fresh:
        print("No unseen stories in the available feeds.")
        return
    # No publication-age cutoff: old but previously unseen feed entries remain eligible.
    fresh.sort(key=lambda item: item.get("published_ts", 0), reverse=True)
    published_count = 0
    for item in fresh[:MAX_POSTS_PER_RUN]:
        translated = translate_and_classify(item)
        source_count = related_source_count(item, all_items)
        publish(build_post(item, translated, source_count))
        seen.add(item["key"])
        published_count += 1
        print(f"Published: {item['title']} [{item['source']}]")
    state["seen"] = list(seen)
    save_state(state)
    print(f"Published {published_count} item(s).")

if __name__ == "__main__":
    main()
