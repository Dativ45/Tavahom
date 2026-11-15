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
MAX_AGE_SECONDS = 3 * 60 * 60
MAX_PER_RUN = 3
MAX_PER_HOUR = 12
MAX_SEEN = 5000

# Direct publisher feeds are attempted first; Google News queries broaden discovery.
DIRECT_FEEDS = [
    ("ایرنا", "https://www.irna.ir/rss"),
    ("ایسنا", "https://www.isna.ir/rss"),
    ("مهر", "https://www.mehrnews.com/rss"),
    ("باشگاه خبرنگاران جوان", "https://www.yjc.ir/fa/rss/allnews"),
    ("تابناک", "https://www.tabnak.ir/fa/rss/allnews"),
    ("تسنیم", "https://www.tasnimnews.ir/fa/rss/feed/0/8/0/"),
    ("فارس", "https://www.farsnews.ir/rss"),
    ("ایلنا", "https://www.ilna.ir/rss"),
    ("خبرآنلاین", "https://www.khabaronline.ir/rss"),
]
SEARCHES = [
    ("سیاست و حکمرانی", '(Iran OR Iranian) (government OR parliament OR policy OR election OR president)'),
    ("امنیت و تحولات منطقه", '(Iran OR Iranian) (military OR missile OR drone OR Israel OR Gulf OR Hormuz OR nuclear)'),
    ("اقتصاد و بازار", '(Iran OR Iranian) (economy OR currency OR rial OR inflation OR oil OR stock market OR prices)'),
    ("معیشت و کار", '(Iran OR Iranian) (workers OR wages OR pensions OR subsidies OR minimum wage OR social security)'),
    ("جامعه، سلامت و آموزش", '(Iran OR Iranian) (health OR medicine OR hospital OR schools OR universities OR education)'),
    ("مهاجرت و کنسولی", '(Iran OR Iranian) (visa OR embassy OR immigration OR consular OR residence permit)'),
    ("استان‌ها و مدیریت شهری", '(Iran OR Iranian) (Tehran OR Mashhad OR Isfahan OR Shiraz OR Tabriz OR Ahvaz OR Rasht OR Zahedan OR province OR mayor)'),
    ("آب، برق و انرژی و حمل‌ونقل", '(Iran OR Iranian) (water OR electricity OR power outage OR gas OR fuel OR railway OR flights OR roads)'),
    ("مسکن و کسب‌وکار", '(Iran OR Iranian) (housing OR rent OR business OR industry OR jobs OR manufacturing OR taxes)'),
    ("محیط زیست و آب و هوا", '(Iran OR Iranian) (environment OR pollution OR dust storm OR drought OR flood OR rainfall)'),
    ("گزارش‌های مردمی و محلی", '(Iran OR Iranian) (local news OR community OR residents OR city services OR incident)'),
    ("تحولات ایران در رسانه‌های جهانی", '(Iran OR Iranian) (politics OR security OR economy OR sanctions OR nuclear OR society OR health)'),
]
CATEGORY_RULES = [
    ("امنیت و منطقه", r"موشک|پهپاد|حمله|انفجار|پدافند|نظامی|اسرائیل|جنگ|آتش.?بس|هشدار امنیتی|تنگه هرمز|خلیج فارس|تحریم"),
    ("اقتصاد و بازار", r"دلار|ارز|طلا|سکه|بورس|تورم|بانک مرکزی|نفت|بنزین|قیمت|بازار|اقتصاد"),
    ("معیشت و کار", r"حقوق|دستمزد|کارگر|بازنشسته|یارانه|کالابرگ|معیشت|تامین اجتماعی|بیمه"),
    ("سیاست و حکمرانی", r"دولت|مجلس|وزیر|رئیس.?جمهور|قوه قضاییه|مصوبه|قانون|انتخابات|وزارت"),
    ("مهاجرت و کنسولی", r"مهاجرت|ویزا|ویز|سفارت|اقامت|پناهندگی|کنسولی"),
    ("آب و برق و انرژی", r"خاموشی|قطعی برق|برق|آب.?و.?فاضلاب|سد|نیروگاه|گازرسانی|بنزین|گازوئیل|انرژی"),
    ("مسکن و اجاره", r"مسکن|اجاره|مستاجر|وام مسکن|نهضت ملی"),
    ("آموزش و دانشگاه", r"آموزش.?و.?پرورش|مدرسه|دانشگاه|دانشجو|کنکور|امتحان|معلم"),
    ("سلامت و دارو", r"دارو|کمبود دارو|وزارت بهداشت|بیمارستان|درمان|سلامت|پزشکی"),
    ("حمل و نقل", r"راه.?آهن|قطار|فرودگاه|پرواز|هواپیمایی|مترو|ترافیک|جاده"),
    ("محیط زیست", r"محیط زیست|گرد.?و.?غبار|آلودگی هوا|خشکسالی|بارندگی|ریزگرد"),
    ("قضایی و حقوق شهروندی", r"قوه قضاییه|دادگاه|دادگستری|حقوق شهروندی|پرونده قضایی"),
    ("ارتباطات و اینترنت", r"اینترنت|فیلترینگ|مخابرات|اپراتور|ارتباطات"),
    ("جامعه و حوادث", r"حادثه|تصادف|زلزله|سیل|آلودگی|سلامت|بیمارستان"),
]

def clean(value):
    # Normalize actual whitespace after removing markup; keep Persian text intact.
    text = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    return re.sub(r"\s+", " ", text).strip()

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

def label_is_citizen(source):
    return source in ("گزارش‌های مردمی", "صدای محله‌ها", "گزارش‌های مردمی و محلی")


def make_item(source, title, summary, link, published_ts, source_kind):
    title = clean(title)
    if not title or not link.startswith("http") or not published_ts:
        return None
    age = int(time.time()) - int(published_ts)
    if age < -300 or age > MAX_AGE_SECONDS:
        return None
    canonical = re.sub(r"[^0-9a-z\u0600-\u06ff]+", " ", title.lower()).strip()
    key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    body = (title + " " + clean(summary)).lower()
    # Avoid unrelated global stories returned by broad news feeds.
    if not re.search(
        r"ایران|ایرانی|تهران|مشهد|اصفهان|شیراز|تبریز|اهواز|کرج|قم|رشت|کرمان|یزد|"
        r"زاهدان|بندرعباس|سنندج|ارومیه|اردبیل|قزوین|گرگان|ساری|بوشهر|خرمشهر|"
        r"آذربایجان شرقی|آذربایجان غربی|چهارمحال و بختیاری|خراسان شمالی|خراسان جنوبی|"
        r"خراسان رضوی|سیستان و بلوچستان|کهگیلویه و بویراحمد|کرمانشاه|کردستان|"
        r"لرستان|مازندران|گلستان|هرمزگان|همدان|مرکزی|ایلام|زنجان|سمنان|فارس|البرز|"
        r"گیلان|قزوین|قم|بیرجند|بجنورد|یاسوج|اراک|کاشان|مراغه|مرند|آمل|بابل|قائمشهر|"
        r"خلیج فارس|تنگه هرمز|دریای عمان|iran|tehran|iranian",
        body, re.I
    ):
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
        "citizen_report": bool(re.search(r"گزارش مردمی|شهروند.?خبرنگار|ارسالی مخاطبان|ویدئو.?ی ارسالی|روایت شهروندان|گزارش محلی", (title + " " + clean(summary)).lower()) or source_kind == "Google News RSS" and label_is_citizen(source)),
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
        url = "https://news.google.com/rss/search?q=" + quote(query + " when:3h") + "&hl=en-US&gl=US&ceid=US:en"
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
                    desk_categories = {
                        "سیاست و حکمرانی": "سیاست و حکمرانی",
                        "امنیت و تحولات منطقه": "امنیت و منطقه",
                        "اقتصاد و بازار": "اقتصاد و بازار",
                        "معیشت و کار": "معیشت و کار",
                        "جامعه، سلامت و آموزش": "جامعه و حوادث",
                        "مهاجرت و کنسولی": "مهاجرت و کنسولی",
                        "استان‌ها و مدیریت شهری": "جامعه و حوادث",
                        "آب، برق و انرژی و حمل‌ونقل": "آب و برق و انرژی",
                        "مسکن و کسب‌وکار": "مسکن و اجاره",
                        "محیط زیست و آب و هوا": "محیط زیست",
                        "گزارش‌های مردمی و محلی": "جامعه و حوادث",
                    }
                    if item["category"] == "ایران" and label in desk_categories:
                        item["category"] = desk_categories[label]
                    item["citizen_report"] = item.get("citizen_report", False) or label_is_citizen(label)
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
    desk_label = "صدای مردم | " + item["category"] if item.get("citizen_report") else item["category"]
    lines = [
        "🇮🇷 <b>اتاق وضعیت | خبر ایران</b>",
        "━━━━━━━━━━━━━━━━━━",
        "🗂 <b>" + html.escape(desk_label) + "</b>",
        "⚠️ <b>تأیید مستقل نشده</b>",
        "",
        "<b>" + html.escape(title_fa) + "</b>",
        html.escape(summary_fa),
        "",
        "🗞 منبع: " + html.escape(item["source"]),
        "🕒 انتشار منبع: " + html.escape(published),
        '<a href="' + html.escape(item["link"], quote=True) + '">مشاهده منبع</a>',
    ]
    return chr(10).join(lines)


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
    # Broader Iran coverage: balance urgency, public impact and freshness.
    priority = {
        "امنیت و منطقه": 7, "اقتصاد و بازار": 6, "معیشت و کار": 6,
        "سیاست و حکمرانی": 5, "مهاجرت و کنسولی": 5, "آب و برق و انرژی": 5,
        "سلامت و دارو": 5, "حمل و نقل": 4, "مسکن و اجاره": 4,
        "آموزش و دانشگاه": 4, "محیط زیست": 4, "قضایی و حقوق شهروندی": 4,
        "ارتباطات و اینترنت": 4, "جامعه و حوادث": 4, "ایران": 2,
    }
    candidates.sort(
        key=lambda x: (priority.get(x["category"], 1),
                       -(now - x["published_ts"]),
                       x["source_kind"] == "خوراک مستقیم"),
        reverse=True
    )
    limit = min(MAX_PER_RUN, room)
    selected = []
    selected_categories = set()
    # Prefer a mix of public-interest desks in one run; fill remaining slots if needed.
    for item in candidates:
        if item["category"] not in selected_categories:
            selected.append(item)
            selected_categories.add(item["category"])
            if len(selected) >= limit:
                break
    if len(selected) < limit:
        selected_keys = {item["key"] for item in selected}
        selected.extend(item for item in candidates if item["key"] not in selected_keys) 
        selected = selected[:limit]
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
