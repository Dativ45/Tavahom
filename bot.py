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
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
STATE_FILE = Path("state.json")
CONFIG_FILE = Path("editorial_config.json")
MAX_POSTS_PER_RUN = 3  # Channel-local archive index verified
MAX_SEEN = 5000

def channel_footer():
    """Return a clickable channel join/follow link when Telegram exposes one."""
    configured = os.getenv("STATUS_ROOM_CHANNEL_URL", "").strip()
    if configured.startswith("https://t.me/"):
        link = configured
    else:
        if str(CHANNEL).startswith("@"):
            link = f"https://t.me/{str(CHANNEL)[1:]}"
        else:
            try:
                response = requests.get(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/getChat",
                    params={"chat_id": CHANNEL},
                    timeout=5,
                )
                payload = response.json()
                if not response.ok or not payload.get("ok"):
                    print("WARNING: optional getChat footer lookup failed; continuing without footer.")
                    return ""
                chat = payload.get("result", {})
                username = chat.get("username")
                invite_link = chat.get("invite_link")
                if username:
                    link = f"https://t.me/{username}"
                elif invite_link:
                    link = invite_link
                else:
                    print("WARNING: Channel is private and Telegram did not expose an invite link; continuing without footer.")
                    return ""
            except (requests.RequestException, ValueError) as exc:
                print(f"WARNING: optional channel footer lookup failed ({exc}); continuing without footer.")
    return '\n\n📣 <b>دنبال‌کردن اتاق وضعیت:</b> <a href="' + html.escape(link, quote=True) + '">عضویت در کانال</a>'

if not BOT_TOKEN or not CHANNEL:
    raise SystemExit("Missing GitHub Actions secrets: TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID")
if not GEMINI_KEY:
    raise SystemExit("Missing GEMINI_API_KEY: bilingual publishing requires translation.")

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
    "جنگ و امنیت": "سایه‌نویس",
    "مهاجرت": "اطلس",
    "معیشت": "تراز",
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
        "ورزش، سیاست، اقتصاد، معیشت، مهاجرت، جنگ و امنیت، فناوری، فرهنگ و جامعه، علم و سلامت، محیط زیست، بین‌الملل. "
        "If the excerpt is empty, say that the source feed provided only a headline.\\n\n"
        + json.dumps({"source": item["source"], "headline": item["title"], "excerpt": item["summary"]}, ensure_ascii=False)
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    response = requests.post(
        url,
        headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
                "maxOutputTokens": 1400,
            },
        },
        timeout=60,
    )
    if not response.ok:
        # Do not print the request URL or API key.
        try:
            detail = response.json().get("error", {}).get("message", "Gemini API request failed")
        except (ValueError, AttributeError):
            detail = "Gemini API request failed"
        raise RuntimeError(f"Gemini API error ({response.status_code}): {str(detail)[:500]}")
    payload = response.json()
    parts = payload.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    content = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
    if not content.strip():
        raise RuntimeError("Gemini returned an empty translation response.")
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
    archive_category = archive_category_for_desk(translated["desk"])
    tag = "🗂 <b>آرشیو: " + html.escape(archive_category or translated["desk"]) + "</b>"
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
        "🛰️ <b>اتاق وضعیت | گزارش " + html.escape(translated["desk"]) + "</b>\n"
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
        + tag + "\n"
        + f"🕒 زمان منبع: {html.escape(published)}\n"
        + f"🔗 <a href=\"{html.escape(item['link'], quote=True)}\">مشاهده منبع اصلی</a>\n\n"
        + f"— <b>{html.escape(alias)}</b> | تحریریه {html.escape(translated['desk'])}\n"
        + "<i>اتاق وضعیت؛ نام‌ها پنهان می‌مانند، حقیقت نه.</i>"
        + channel_footer()
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
    return payload.get("result", {}).get("message_id")


def archive_category_for_desk(desk):
    if desk == "مهاجرت":
        return "مهاجرت"
    if desk in ("معیشت", "اقتصاد"):
        return "معیشت"
    if desk == "جنگ و امنیت":
        return "جنگ و امنیت"
    if desk == "ورزش":
        return "ورزش"
    return None


def channel_message_link(message_id):
    channel = str(CHANNEL).strip()
    if channel.startswith("@"):
        return f"https://t.me/{channel[1:]}/{message_id}"
    numeric = channel.removeprefix("-100")
    if numeric.isdigit():
        return f"https://t.me/c/{numeric}/{message_id}"
    raise RuntimeError("TELEGRAM_CHAT_ID must be a public @channel username or a numeric channel ID for archive links.")


def update_navigation(state):
    message_id = state.get("navigation_message_id")
    if not message_id:
        raise RuntimeError("Archive navigation message has not been created.")
    archive = state.get("archive", [])
    sections = [
        ("مهاجرت", "🌍"),
        ("معیشت", "🧺"),
        ("جنگ و امنیت", "⚠️"),
        ("ورزش", "🏅"),
    ]
    lines = [
        "📌 <b>آرشیو موضوعی اتاق وضعیت</b>",
        "━━━━━━━━━━━━━━━━━━",
        "برای دسترسی مستقیم به مطالب هر بخش، روی عنوان خبر بزنید.",
        "پیوندها مستقیماً به پیام‌های همین کانال می‌روند؛ جست‌وجوی سراسری تلگرام نیست.",
        "",
    ]
    for category, emoji in sections:
        lines.append(f"{emoji} <b>{category}</b>")
        entries = [item for item in archive if item.get("category") == category][-6:]
        if not entries:
            lines.append("   هنوز مطلبی در این بخش ثبت نشده است.")
        else:
            for item in reversed(entries):
                title = html.escape(item.get("title", "مشاهده مطلب")[:110])
                link = channel_message_link(item["message_id"])
                lines.append(f'   • <a href="{html.escape(link, quote=True)}">{title}</a>')
        lines.append("")
    text = "\n".join(lines).strip()
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText",
        json={
            "chat_id": CHANNEL,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=35,
    )
    payload = response.json()
    if not response.ok or not payload.get("ok"):
        description = str(payload.get("description", payload))
        if "message is not modified" not in description.lower():
            raise RuntimeError("Telegram could not update archive index: " + description)


def ensure_navigation(state):
    if state.get("navigation_message_id"):
        return
    message_id = publish("📌 <b>آرشیو موضوعی اتاق وضعیت</b>\nدر حال آماده‌سازی فهرست پیوندهای داخلی کانال…")
    if not message_id:
        raise RuntimeError("Telegram returned no message ID for archive index.")
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/pinChatMessage",
        json={"chat_id": CHANNEL, "message_id": message_id, "disable_notification": True},
        timeout=35,
    )
    payload = response.json()
    if not response.ok or not payload.get("ok"):
        raise RuntimeError("Telegram could not pin archive index: " + str(payload.get("description", payload)))
    state["navigation_message_id"] = message_id
    state.setdefault("archive", [])
    save_state(state)
    print("Pinned archive navigation message: " + str(message_id))


def main():
    state = load_state()
    ensure_navigation(state)
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
        message_id = publish(build_post(item, translated, source_count))
        category = archive_category_for_desk(translated["desk"])
        if category and message_id:
            state.setdefault("archive", []).append({
                "category": category,
                "title": translated["title_fa"],
                "message_id": message_id,
            })
            state["archive"] = state["archive"][-500:]
        seen.add(item["key"])
        published_count += 1
        print(f"Published: {item['title']} [{item['source']}] message_id={message_id}")
    state["seen"] = list(seen)
    save_state(state)
    update_navigation(state)
    print(f"Published {published_count} item(s). Archive index updated.")

if __name__ == "__main__":
    main()
