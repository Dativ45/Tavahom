"""Experimental technology desk using desk_core; not scheduled for production yet."""
import hashlib
import html
import json
import os
import re
import time
from pathlib import Path

from desk_core import (
    DeskError, DeskState, DeliveryUncertain, fetch_feed, format_rss_notice,
    gemini_json, item_key, telegram_send, telegram_send_photo,
)

FEEDS = [
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("TechCrunch", "https://techcrunch.com/feed/"),
    ("Engadget", "https://www.engadget.com/rss.xml"),
    ("Android Authority", "https://www.androidauthority.com/feed/"),
    ("GSMArena", "https://www.gsmarena.com/rss-news-reviews.php3"),
    ("Wired", "https://www.wired.com/feed/rss"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("9to5Google", "https://9to5google.com/feed/"),
    ("9to5Mac", "https://9to5mac.com/feed/"),
    ("Tom's Hardware", "https://www.tomshardware.com/feeds.xml"),
]
STATE_PATH = Path("data/state/tech_v2_state.json")
LEGACY_STATE_PATH = Path("tech_state.json")
ALLOWED = {"گوشی هوشمند", "تبلت", "گجت و پوشیدنی", "هوش مصنوعی",
           "نرم‌افزار", "سخت‌افزار و تراشه", "امنیت سایبری", "فناوری مصرفی"}


def _legacy_key(title, link):
    return hashlib.sha256((title + link).encode("utf-8")).hexdigest()


def collect():
    items = []
    for source, url in FEEDS:
        try:
            for item in fetch_feed(url):
                if re.search(r"\b(poll|survey)\b", item["title"], re.I):
                    continue
                stamp = item.get("published_at", 0)
                if stamp and time.time() - stamp > 72 * 3600:
                    continue
                item.update(source=source, timestamp=stamp,
                            legacy_key=_legacy_key(item["title"], item.get("raw_link", item["link"])),
                            key=item_key(item["title"], item["link"]))
                items.append(item)
        except Exception as exc:
            print(f"Feed error [{source}]: {type(exc).__name__}: {str(exc)[:180]}")
    return items


def translate(item):
    prompt = (
        "You are the technology editor of a professional bilingual news channel. "
        "Return JSON only with title_fa, summary_fa, category. Translate into natural Iranian Persian. "
        "Do not invent specifications, prices, release dates, test results, or claims. "
        "If the RSS excerpt is empty, explicitly say that the feed supplied only a headline. "
        "Keep the summary to 2-3 concise sentences. Choose category from: "
        + "، ".join(sorted(ALLOWED)) + ".\n"
        + json.dumps({"source": item["source"], "headline": item["title"],
                      "excerpt": item["summary"]}, ensure_ascii=False)
    )
    result = gemini_json(prompt)
    category = str(result.get("category", "فناوری مصرفی")).strip()
    if category not in ALLOWED:
        category = "فناوری مصرفی"
    title = re.sub(r"\s+", " ", str(result.get("title_fa", "")).strip()) or item["title"]
    summary = re.sub(r"\s+", " ", str(result.get("summary_fa", "")).strip())
    if not summary:
        summary = "خوراک خبری جزئیات بیشتری ارائه نکرده است؛ برای اطلاعات کامل به گزارش اصلی مراجعه کنید."
    return title[:300], summary[:700], category


def _footer():
    configured = os.getenv("STATUS_ROOM_CHANNEL_URL", "").strip()
    if configured.startswith("https://t.me/"):
        url = configured
    else:
        chat = os.getenv("TELEGRAM_CHAT_ID", "")
        url = "https://t.me/" + chat[1:] if chat.startswith("@") else ""
    return ('\n\n📣 <a href="' + html.escape(url, quote=True) +
            '">عضویت در اتاق وضعیت</a>') if url else ""


def format_post(item, title_fa, summary_fa, category):
    timestamp = (time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(item["timestamp"]))
                 if item["timestamp"] else "زمان انتشار در خوراک مشخص نیست")
    notice = format_rss_notice(item["summary"])
    body = (
        "📱 <b>اتاق وضعیت | فناوری جهان</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🏷 <b>{html.escape(category)}</b>\n\n"
        f"🇮🇷 <b>{html.escape(title_fa)}</b>\n"
        f"{html.escape(summary_fa)}\n"
        f"ℹ️ {html.escape(notice)}\n\n"
        "🌐 <b>Original (English)</b>\n"
        f"{html.escape(item['title'][:180])}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🗞 منبع: {html.escape(item['source'])}\n"
        f"🕒 زمان خوراک: {html.escape(timestamp)}\n"
        f"🔗 <a href=\"{html.escape(item['link'], quote=True)}\">گزارش اصلی</a>"
        + _footer()
    )
    return body


def format_photo_caption(item, title_fa, summary_fa, category):
    # Build a separate, bounded caption; never slice HTML markup mid-tag.
    source = html.escape(item["source"])
    title = html.escape(title_fa[:180])
    summary = html.escape(summary_fa[:220])
    link = html.escape(item["link"], quote=True)
    return (
        "📱 <b>اتاق وضعیت | فناوری جهان</b>\n"
        f"🏷 <b>{html.escape(category)}</b>\n"
        f"<b>{title}</b>\n{summary}\n"
        f"ℹ️ {html.escape(format_rss_notice(item['summary']))}\n"
        f"🗞 {source} | <a href=\"{link}\">گزارش اصلی</a>"
    )[:1000]


def main():
    dry_run = os.getenv("TECH_V2_DRY_RUN", "1").strip().lower() not in {"0", "false", "no"}
    state = DeskState(STATE_PATH)
    if LEGACY_STATE_PATH.exists():
        try:
            old = json.loads(LEGACY_STATE_PATH.read_text(encoding="utf-8"))
            state.import_legacy_seen(old.get("seen", []) if isinstance(old, dict) else [])
        except (OSError, json.JSONDecodeError, AttributeError):
            raise DeskError("Legacy technology state could not be read; migration stopped safely")

    items = collect()
    eligible = [
        item for item in items
        if state.status(item["key"]) == "new"
        and state.status("legacy:" + item["legacy_key"]) == "new"
    ]
    eligible.sort(key=lambda x: x["timestamp"], reverse=True)
    if not eligible:
        print("[tech-v2] no new eligible stories")
        return
    item = eligible[0]
    title_fa, summary_fa, category = translate(item)
    message = format_post(item, title_fa, summary_fa, category)
    if dry_run:
        print(f"[tech-v2] DRY RUN; no Telegram send and no state mutation: {item['source']} | {item['title']}")
        print(message[:1800])
        return

    state.begin(item["key"], item["title"])
    try:
        if item.get("image"):
            try:
                photo_caption = format_photo_caption(item, title_fa, summary_fa, category)
                if len(photo_caption) <= 1024:
                    result = telegram_send_photo(item["image"], photo_caption)
                else:
                    result = telegram_send(message)
            except DeliveryUncertain:
                raise
            except DeskError:
                # A definite photo rejection is safe to fall back to a text message.
                result = telegram_send(message)
        else:
            result = telegram_send(message)
    except DeliveryUncertain as exc:
        state.mark_unknown(item["key"], str(exc))
        raise
    except DeskError as exc:
        state.mark_failed(item["key"], str(exc))
        raise
    state.mark_sent(item["key"], result["message_id"])
    print(f"[tech-v2] published message_id={result['message_id']} source={item['source']}")


if __name__ == "__main__":
    main()
