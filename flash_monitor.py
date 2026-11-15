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

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
DRY_RUN = os.getenv("FLASH_DRY_RUN", "false").lower() == "true"
STATE_FILE = Path("flash_state.json")
MAX_AGE_SECONDS = 4 * 60 * 60
MAX_ALERTS_PER_RUN = 5
MAX_SEEN = 6000

# Separate targeted searches keep urgent regional events out of the general-news queue.
SEARCHES = [
    ("Iran / Israel escalation", '(Iran OR Israel OR "Persian Gulf" OR Hormuz) (strike OR missile OR drone OR attack OR military OR escalation OR ceasefire)'),
    ("US / CENTCOM / Gulf", '(CENTCOM OR Pentagon OR "US forces" OR "US Navy") (Iran OR Gulf OR Israel OR Iraq OR Syria)'),
    ("Maritime security", '("Strait of Hormuz" OR "Gulf of Oman" OR "Red Sea") (ship OR tanker OR attack OR seizure OR closure OR naval)'),
    ("Regional military activity", '(Israel OR Lebanon OR Syria OR Iraq OR Yemen) (airstrike OR missile OR drone OR interception OR military)'),
    ("Iran diplomacy / nuclear", '(Iran OR IAEA) (nuclear OR enrichment OR sanctions OR negotiations OR talks OR agreement)'),
    ("Energy / Gulf disruption", '(oil OR LNG OR OPEC OR tanker OR shipping) (Iran OR Gulf OR Hormuz OR disruption OR attack)'),
]

EVENT_PATTERNS = [
    (3, r'\b(airstrike|air strike|missile|drone strike|rocket attack|explosion|explosions|bombing|bombed|invasion|intercepted|interception|shot down|killed|casualties|warship|naval attack|seized tanker|ship seized|hormuz closed|strait closed)\b'),
    (2, r'\b(deploys?|deployment|military buildup|troops|carrier|sanctions|ceasefire|ultimatum|evacuation|embassy|nuclear deal|nuclear talks|enrichment|mobiliz\w*|alert level|airspace closed|shipping disruption|oil disruption)\b'),
]
REGION_PATTERN = re.compile(r'\b(iran|israel|iraq|syria|lebanon|yemen|gulf|hormuz|centcom|persian gulf|gulf of oman|red sea|saudi arabia|qatar|bahrain|uae|united arab emirates|pentagon)\b', re.I)


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
    headers = {"User-Agent": "StatusRoomFlashMonitor/1.0 (+https://github.com/Dativ45/Tavahom)"}
    for label, query in SEARCHES:
        url = "https://news.google.com/rss/search?q=" + quote(query + " when:1d") + "&hl=en-US&gl=US&ceid=US:en"
        try:
            response = requests.get(url, headers=headers, timeout=20)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            for entry in feed.entries[:35]:
                raw_title = clean(entry.get("title"))
                link = entry.get("link", "")
                if not raw_title or not link.startswith("http"):
                    continue
                source_meta = entry.get("source", {}) or {}
                publisher = clean(source_meta.get("title", "")) if hasattr(source_meta, "get") else ""
                title = raw_title
                # Google News commonly appends " - Publisher" to the headline.
                if publisher and title.lower().endswith(" - " + publisher.lower()):
                    title = title[:-(len(publisher) + 3)].strip()
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                published_ts = calendar.timegm(published) if published else 0
                summary = clean(entry.get("summary", entry.get("description", "")))
                canonical = re.sub(r"[^a-z0-9\u0600-\u06ff]+", " ", title.lower()).strip()
                key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                domain = urlparse(link).netloc.lower().removeprefix("www.")
                items.append({
                    "query": label,
                    "source": publisher or domain or label,
                    "domain": domain,
                    "title": title[:500],
                    "summary": summary[:1800],
                    "link": link,
                    "published_ts": published_ts,
                    "key": key,
                })
        except Exception as exc:
            print(f"Feed error [{label}]: {str(exc)[:250]}")
    # Same headline can appear in several query feeds. Keep the freshest copy and count appearances.
    merged = {}
    for item in items:
        old = merged.get(item["key"])
        if old is None:
            item["appearances"] = 1
            merged[item["key"]] = item
        else:
            old["appearances"] += 1
            if item["published_ts"] > old["published_ts"]:
                item["appearances"] = old["appearances"]
                merged[item["key"]] = item
    return list(merged.values())


def event_score(item):
    text = item["title"] + " " + item["summary"]
    if not REGION_PATTERN.search(text):
        return 0
    score = 0
    for points, pattern in EVENT_PATTERNS:
        if re.search(pattern, text, re.I):
            score = max(score, points)
    return score


def translate(item):
    prompt = (
        "به‌عنوان دبیر فوری یک رسانه فارسی‌زبان عمل کن. فقط JSON معتبر با کلیدهای "
        "title_fa و summary_fa برگردان. تیتر را به فارسی طبیعی ایران ترجمه کن و خلاصه‌ای "
        "حداکثر دو جمله‌ای فقط بر اساس متن داده‌شده بنویس. هیچ علت، جزئیات، نقل‌قول یا قطعیتی "
        "اضافه نکن. اگر خوراک فقط تیتر دارد، صریحاً بگو جزئیات بیشتری در خوراک نیست. "
        "از زبان هیجانی و ادعای تأییدنشده پرهیز کن.\n"
        + json.dumps({"headline": item["title"], "excerpt": item["summary"]}, ensure_ascii=False)
    )
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    response = None
    for attempt in range(4):
        response = requests.post(
            endpoint,
            headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json", "maxOutputTokens": 650},
            },
            timeout=45,
        )
        if response.ok:
            break
        if response.status_code not in (429, 500, 502, 503, 504) or attempt == 3:
            try:
                detail = response.json().get("error", {}).get("message", "Gemini request failed")
            except (ValueError, AttributeError):
                detail = "Gemini request failed"
            raise RuntimeError(f"Gemini API error ({response.status_code}): {str(detail)[:300]}")
        delay = 2 ** (attempt + 1)
        print(f"Gemini temporary error {response.status_code}; retrying in {delay}s.")
        time.sleep(delay)
    parts = response.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
    raw = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    data = json.loads(raw)
    title = clean(str(data.get("title_fa", "")))[:350] or item["title"]
    summary = clean(str(data.get("summary_fa", "")))[:900]
    if not summary:
        summary = "خوراک خبری جزئیات بیشتری ارائه نکرده است."
    return title, summary


def build_message(item, title_fa, summary_fa, score):
    priority = "🚨 <b>هشدار فوری</b>" if score >= 3 else "🟠 <b>تحول مهم</b>"
    confidence = "بازتاب در چند جست‌وجو؛ تأیید مستقل نشده" if item.get("appearances", 1) >= 2 else "تأیید مستقل نشده"
    published = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(item["published_ts"])) if item["published_ts"] else "زمان منبع مشخص نیست"
    source_link = html.escape(item["link"], quote=True)
    return (
        "🛰️ <b>اتاق وضعیت | پایش زنده تحولات</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        + priority + "\n"
        + "⚠️ " + confidence + "\n\n"
        + "🇮🇷 <b>" + html.escape(title_fa) + "</b>\n"
        + html.escape(summary_fa) + "\n\n"
        + "🗞 منبع: " + html.escape(item["source"][:100]) + "\n"
        + "🕒 زمان انتشار منبع: " + html.escape(published) + "\n"
        + f'🔗 <a href="{source_link}">پیگیری منبع</a>'
    )

def publish(message):
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHANNEL, "text": message, "parse_mode": "HTML", "disable_web_page_preview": False},
        timeout=30,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"description": response.text[:500]}
    if not response.ok or not payload.get("ok"):
        raise RuntimeError(f"Telegram API error ({response.status_code}): {payload.get('description', payload)}")
    return payload.get("result", {}).get("message_id")


def main():
    if not DRY_RUN and not all([BOT_TOKEN, CHANNEL, GEMINI_KEY]):
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, or GEMINI_API_KEY.")
    state = load_state()
    seen = set(state.get("seen", []))
    now = int(time.time())
    items = fetch_items()
    candidates = []
    for item in items:
        age = now - item["published_ts"] if item["published_ts"] else MAX_AGE_SECONDS + 1
        score = event_score(item)
        if item["key"] in seen or age < -300 or age > MAX_AGE_SECONDS or score < 2:
            continue
        candidates.append((score, item))
    candidates.sort(key=lambda pair: (pair[0], pair[1]["published_ts"]), reverse=True)
    if not candidates:
        print(f"No new qualifying flash events. Scanned {len(items)} feed entries.")
        return
    published_keys = []
    for score, item in candidates[:MAX_ALERTS_PER_RUN]:
        title_fa, summary_fa = translate(item)
        message = build_message(item, title_fa, summary_fa, score)
        if DRY_RUN:
            print("[DRY RUN] " + message.replace("\n", " ")[:1800])
        else:
            message_id = publish(message)
            print(f"Published flash alert: {item['title'][:160]} message_id={message_id}")
        published_keys.append(item["key"])
    if not DRY_RUN:
        state["seen"] = list(seen.union(published_keys))[-MAX_SEEN:]
        save_state(state)
    print(f"Processed {len(published_keys)} flash alert(s); dry_run={DRY_RUN}.")


if __name__ == "__main__":
    main()
