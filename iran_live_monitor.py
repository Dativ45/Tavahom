import os
import re
import json
import time
import html
import hashlib
import calendar
from pathlib import Path
from urllib.parse import quote, urlparse

import feedparser
import requests

BOT = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
DRY_RUN = os.getenv("IRAN_NEWS_DRY_RUN", "false").lower() == "true"
STATE_FILE = Path("iran_live_state.json")
MAX_AGE_SECONDS = 90 * 60
MAX_PER_RUN = 2
MAX_PER_HOUR = 8
MAX_SEEN = 5000

# Direct publisher feeds are attempted first; Google News queries broaden discovery.
DIRECT_FEEDS = [
    ("ایرنا", "https://www.irna.ir/rss"),
    ("ایسنا", "https://www.isna.ir/rss"),
    ("مهر", "https://www.mehrnews.com/rss"),
    ("باشگاه خبرنگاران جوان", "https://www.yjc.ir/fa/rss/allnews"),
    ("تابناک", "https://www.tabnak.ir/fa/rss/allnews"),
    ("تسنیم", "https://www.tasnimnews.com/fa/rss/feed/0/8/0/"),
    ("فارس", "https://www.farsnews.ir/rss"),
    ("ایلنا", "https://www.ilna.ir/rss"),
]
SEARCHES = [
    ("سیاست و حکمرانی", "ایران دولت مجلس سیاست انتخابات تصمیمات حکومتی"),
    ("امنیت و تحولات منطقه", "ایران امنیت نظامی حمله اسرائیل آمریکا خلیج فارس"),
    ("اقتصاد و بازار", "ایران اقتصاد ارز دلار طلا بورس قیمت بازار"),
    ("معیشت و کار", "ایران حقوق کارگران بازنشستگان یارانه کالابرگ معیشت"),
    ("جامعه و حوادث", "ایران جامعه حوادث آموزش سلامت محیط زیست"),
    ("مهاجرت و کنسولی", "ایران مهاجرت ویزا سفارت قوانین اقامت"),
    ("استان‌ها", "اخبار استان های ایران استانداری حوادث تعطیلی"),
    ("گزارش‌های مردمی", '"گزارش مردمی" OR "شهروند خبرنگار" OR "ویدئوی ارسالی شهروندان" OR "گزارش شهروندان" ایران'),
    ("صدای محله‌ها", '"گزارش محلی" OR "ارسالی مخاطبان" OR "روایت شهروندان" ایران حادثه قطعی آب برق آلودگی'),
]
CATEGORY_RULES = [
    ("امنیت و منطقه", r"موشک|پهپاد|حمله|انفجار|پدافند|نظامی|اسرائیل|آمریکا|جنگ|آتش.?بس|تنگه هرمز|خلیج فارس|تحریم"),
    ("اقتصاد و بازار", r"دلار|ارز|طلا|سکه|بورس|تورم|بانک مرکزی|نفت|بنزین|قیمت|بازار|اقتصاد"),
    ("معیشت و کار", r"حقوق|دستمزد|کارگر|بازنشسته|یارانه|کالابرگ|معیشت|تامین اجتماعی|بیمه"),
    ("سیاست و حکمرانی", r"دولت|مجلس|وزیر|رئیس.?جمهور|قوه قضاییه|مصوبه|قانون|انتخابات|وزارت"),
    ("مهاجرت و کنسولی", r"مهاجرت|ویزا|ویز|سفارت|اقامت|پناهندگی|کنسولی"),
    ("جامعه و حوادث", r"حادثه|تصادف|زلزله|سیل|آلودگی|مدرسه|دانشگاه|سلامت|بیمارستان"),
]

def clean(value):
    return html.unescape(re.sub(r"\\s+", " ", re.sub(r"<[^>]+>", "", value or ""))).strip()

def load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("seen", [])
            data.setdefault("published", [])
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"seen": [], "published": []}

def save_state(state):
    state["seen"] = state.get("seen", [])[-MAX_SEEN:]
    state["published"] = state.get("published", [])[-500:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def category(text):
    for label, pattern in CATEGORY_RULES:
        if re.search(pattern, text, re.I):
            return label
    return "ایران"

def make_item(source, title, summary, link, published_ts, source_kind):
    title = clean(title)
    if not title or not link.startswith("http") or not published_ts:
        return None
    age = int(time.time()) - int(published_ts)
    if age < -300 or age > MAX_AGE_SECONDS:
        return None
    canonical = re.sub(r"[^0-9a-z\\u0600-\\u06ff]+", " ", title.lower()).strip()
    key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    body = (title + " " + clean(summary)).lower()
    # Avoid unrelated global stories returned by broad news feeds.
    if not re.search(r"ایران|تهران|ایرانی|کشور|استان|خلیج فارس|تنگه هرمز|iran|tehran", body, re.I):
        return None
    return {
        "source": clean(source)[:100],
        "title": title[:500],
        "summary": clean(summary)[:1800],
        "link": link,
        "published_ts": int(published_ts),
        "key": key,
        "category": category(body),
        "source_kind": source_kind,
        "citizen_report": bool(re.search(r"گزارش مردمی|شهروند.?خبرنگار|ارسالی مخاطبان|ویدئو.?ی ارسالی|روایت شهروندان|گزارش محلی", (title + " " + clean(summary)).lower())),
    }

def fetch_all():
    headers = {"User-Agent": "StatusRoomIranDesk/1.0 (+https://github.com/Dativ45/Tavahom)"}
    items = []
    for source, url in DIRECT_FEEDS:
        try:
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            for entry in feed.entries[:35]:
                parsed = entry.get("published_parsed") or entry.get("updated_parsed")
                ts = calendar.timegm(parsed) if parsed else 0
                item = make_item(source, entry.get("title", ""), entry.get("summary", entry.get("description", "")), entry.get("link", ""), ts, "خوراک مستقیم")
                if item:
                    items.append(item)
            print(f"Feed {source}: {len(feed.entries)} entries")
        except Exception as exc:
            print(f"Feed warning [{source}]: {str(exc)[:180]}")

    for label, query in SEARCHES:
        url = "https://news.google.com/rss/search?q=" + quote(query + " when:2h") + "&hl=fa-IR&gl=IR&ceid=IR:fa"
        try:
            response = requests.get(url, headers=headers, timeout=18)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            for entry in feed.entries[:35]:
                parsed = entry.get("published_parsed") or entry.get("updated_parsed")
                ts = calendar.timegm(parsed) if parsed else 0
                meta = entry.get("source", {}) or {}
                publisher = clean(meta.get("title", "")) if hasattr(meta, "get") else ""
                title = clean(entry.get("title", ""))
                if publisher and title.lower().endswith(" - " + publisher.lower()):
                    title = title[:-(len(publisher) + 3)].strip()
                item = make_item(publisher or label, title, entry.get("summary", entry.get("description", "")), entry.get("link", ""), ts, "Google News RSS")
                if item:
                    item["query_desk"] = label
                    items.append(item)
            print(f"Search {label}: {len(feed.entries)} entries")
        except Exception as exc:
            print(f"Search warning [{label}]: {str(exc)[:180]}")

    # Deduplicate identical headlines while retaining the most useful source record.
    merged = {}
    for item in items:
        old = merged.get(item["key"])
        if old is None or (item["source_kind"] == "خوراک مستقیم" and old["source_kind"] != "خوراک مستقیم"):
            merged[item["key"]] = item
    return sorted(merged.values(), key=lambda x: x["published_ts"], reverse=True)

def translate(item):
    prompt = (
        "برای رسانه خبری فارسی ایران، فقط JSON معتبر با title_fa و summary_fa تولید کن. "
        "تیتر را دقیق و طبیعی بنویس. خلاصه حداکثر دو جمله و فقط بر پایه داده ورودی باشد. "
        "هیچ جزئیات یا قطعیتی اضافه نکن. اگر متن ناقص است، محدودیت را روشن بگو. "
        "اگر گزارش مردمی است، آن را ادعای شهروند/گزارش بازنشرشده معرفی کن، نه واقعیت قطعی. "
        "نام، شماره تماس و نشانی یا مشخصات افراد عادی را تکرار نکن؛ افراد عادی را ناشناس نگه دار. "
        "لحن خنثی و حرفه‌ای باشد.\n" +
        json.dumps({"title": item["title"], "excerpt": item["summary"], "desk": item["category"]}, ensure_ascii=False)
    )
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    response = None
    for attempt in range(4):
        response = requests.post(
            endpoint,
            headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json", "maxOutputTokens": 500}},
            timeout=40,
        )
        if response.ok:
            break
        if response.status_code not in (429, 500, 502, 503, 504) or attempt == 3:
            raise RuntimeError(f"Gemini API {response.status_code}: {response.text[:250]}")
        time.sleep(2 ** (attempt + 1))
    parts = response.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
    data = json.loads("".join(p.get("text", "") for p in parts if isinstance(p, dict)))
    return clean(data.get("title_fa", ""))[:300] or item["title"], clean(data.get("summary_fa", ""))[:850] or "خوراک منبع جزئیات بیشتری ارائه نکرده است."

def build_message(item, title_fa, summary_fa):
    published = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(item["published_ts"]))
    if item.get("citizen_report"):\n        confidence = "گزارش مردمی بازنشرشده؛ تأیید مستقل نشده و صرفاً برای اطلاع‌رسانی است."\n        desk_label = "صدای مردم | " + item["category"]\n    else:\n        confidence = "گزارش اولیه؛ تأیید مستقل در این سامانه احراز نشده است."\n        desk_label = item["category"]\n    return (
        "🇮🇷 <b>اتاق وضعیت | خبر ایران</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🗂 <b>" + html.escape(item["category"]) + "</b>\n"
        "⚠️ " + confidence + "\n\n"
        "<b>" + html.escape(title_fa) + "</b>\n"
        + html.escape(summary_fa) + "\n\n"
        "🗞 منبع: " + html.escape(item["source"]) + "\n"
        "🕒 انتشار منبع: " + html.escape(published) + "\n"
        "🔗 <a href=\"" + html.escape(item["link"], quote=True) + "\">متن و جزئیات منبع</a>\n\n"
        "<i>زمان انتشار منبع با زمان دریافت خبر یکسان نیست.</i>"
    )

def publish(message):
    response = requests.post(
        f"https://api.telegram.org/bot{BOT}/sendMessage",
        json={"chat_id": CHAT, "text": message, "parse_mode": "HTML", "disable_web_page_preview": False},
        timeout=30,
    )
    payload = response.json()
    if not response.ok or not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload.get('description', payload)}")
    return payload.get("result", {}).get("message_id")

def main():
    if not DRY_RUN and not all([BOT, CHAT, GEMINI_KEY]):
        raise SystemExit("Missing Telegram/Gemini secrets.")
    state = load_state()
    seen = set(state.get("seen", []))
    now = int(time.time())
    published_recent = [ts for ts in state.get("published", []) if now - int(ts) < 3600]
    room = max(0, MAX_PER_HOUR - len(published_recent))
    if room == 0:
        print("Hourly Iran desk limit reached.")
        return
    items = fetch_all()
    candidates = [x for x in items if x["key"] not in seen]
    candidates.sort(key=lambda x: (x["published_ts"], x["source_kind"] == "خوراک مستقیم"), reverse=True)
    selected = candidates[:min(MAX_PER_RUN, room)]
    if not selected:
        print(f"No fresh Iran stories. scanned={len(items)}")
        return
    posted_keys = []
    for item in selected:
        title_fa, summary_fa = translate(item)
        message = build_message(item, title_fa, summary_fa)
        if DRY_RUN:
            print("[DRY RUN] " + message.replace("\n", " ")[:1500])
        else:
            mid = publish(message)
            print(f"Published [{item['category']}] {item['title'][:130]} message_id={mid}")
        posted_keys.append(item["key"])
    if not DRY_RUN:
        state["seen"] = list(seen.union(posted_keys))[-MAX_SEEN:]
        state["published"] = (published_recent + [now] * len(posted_keys))[-500:]
        save_state(state)
    print(f"Processed {len(posted_keys)} Iran stories; dry_run={DRY_RUN}; scanned={len(items)}")

if __name__ == "__main__":
    main()
