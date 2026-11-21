import os
import json
import html
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
CONFIG_FILE = Path("editorial_config.json")
TEHRAN = ZoneInfo("Asia/Tehran")

FEEDS = [
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Deutsche Welle", "https://rss.dw.com/rdf/rss-en-all"),
    ("NPR World", "https://feeds.npr.org/1004/rss.xml"),
    ("The Guardian World", "https://www.theguardian.com/world/rss"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("France 24", "https://www.france24.com/en/rss"),
    ("UN News", "https://news.un.org/feed/subscribe/en/news/all/rss.xml"),
    ("WHO", "https://www.who.int/rss-feeds/news-english.xml"),
    ("NASA", "https://www.nasa.gov/feed/"),
    ("European Central Bank", "https://www.ecb.europa.eu/rss/press.html"),
    ("World Bank", "https://www.worldbank.org/en/news/all?format=rss"),
]

CAR_TIPS = [
    "فشار باد هر چهار لاستیک و زاپاس را طبق برچسب خودرو بررسی کن؛ فشار نامناسب هم ایمنی را کم می‌کند و هم مصرف سوخت را بالا می‌برد.",
    "پیش از حرکت، چراغ‌های جلو، ترمز، راهنما و چراغ ترمز را امتحان کن؛ سوختن یک لامپ کوچک می‌تواند دردسر بزرگی بسازد.",
    "سطح روغن موتور را روی سطح صاف و طبق دستور دفترچه بررسی کن؛ کمبود روغن را با حدس و انتخاب روغن نامناسب جبران نکن.",
    "سطح مایع خنک‌کننده را فقط وقتی موتور کاملاً سرد است بررسی کن؛ درِ رادیاتور یا مخزن داغ را باز نکن.",
    "برف‌پاک‌کن‌ها و شیشه‌شور را بررسی کن؛ دید خوب در باران و گردوغبار بخشی از ایمنی رانندگی است.",
    "به صدای غیرعادی ترمز، لرزش فرمان یا کشیدن خودرو به یک سمت بی‌تفاوت نباش؛ در صورت تداوم، خودرو باید بررسی فنی شود.",
    "قطب‌های باتری را از نظر شل‌شدن یا سولفاته‌شدن ظاهری بررسی کن؛ برای تمیزکاری یا تعمیر، اتصال کوتاه ایجاد نکن.",
    "تاریخ انقضای کپسول آتش‌نشانی، سلامت مثلث خطر و جعبه کمک‌های اولیه را بررسی کن.",
    "اگر باران یا یخبندان پیش‌بینی شده، آج لاستیک و سلامت چراغ‌ها را بررسی و فاصله طولی را بیشتر کن.",
    "زیر خودرو را پس از توقف برای نشتی تازه روغن یا مایعات نگاه کن؛ نشتی قابل‌توجه را قبل از ادامه مسیر پیگیری کن.",
]

def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

def fetch_last_24h():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    collected = []
    for source, url in FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:35]:
                title = html.unescape(str(entry.get("title", ""))).strip()
                link = str(entry.get("link", "")).strip()
                parsed = entry.get("published_parsed") or entry.get("updated_parsed")
                if not title or not link.startswith("http") or not parsed:
                    continue
                published = datetime.fromtimestamp(time.mktime(parsed), timezone.utc)
                if published < cutoff:
                    continue
                summary = html.unescape(str(entry.get("summary", entry.get("description", ""))))
                summary = " ".join(__import__("re").sub(r"<[^>]+>", " ", summary).split())
                collected.append({
                    "source": source,
                    "title": title[:350],
                    "summary": summary[:1100],
                    "url": link,
                    "published_utc": published.isoformat(),
                    "published_ts": int(published.timestamp()),
                })
        except Exception as exc:
            print(f"Feed unavailable [{source}]: {exc}")
    # Deduplicate by canonical URL and headline, then newest first.
    unique = {}
    for item in collected:
        unique.setdefault(item["url"], item)
    return sorted(unique.values(), key=lambda x: x["published_ts"], reverse=True)[:45]

def select_and_summarize(items):
    if not items:
        return []
    prompt = (
        "نقش دبیر ارشد خبر را داری. از میان خبرهای ورودی فقط ۵ تا ۷ خبر واقعاً مهم و اثرگذار "
        "در ۲۴ ساعت اخیر را انتخاب کن؛ تکراری‌ها را حذف کن و تنوع جغرافیایی و موضوعی را رعایت کن. "
        "برای هر خبر یک خلاصه فارسی روان و دقیق در ۲ جمله بنویس. هیچ واقعیت، عدد، علت یا نقل‌قولی "
        "که در عنوان یا چکیده منبع نیست اضافه نکن. اگر خبر به‌تنهایی تأیید نشده، در متن ادعای قطعیت نکن. "
        "فقط JSON معتبر با ساختار {\"stories\":[{\"index\":0,\"summary_fa\":\"...\"}]} برگردان. "
        "index باید دقیقاً شماره یکی از ورودی‌ها باشد و حداکثر ۷ مورد انتخاب شود.\n"
        + json.dumps(items, ensure_ascii=False)
    )
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    response = requests.post(
        endpoint,
        headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.15,
                "responseMimeType": "application/json",
                "maxOutputTokens": 2200,
            },
        },
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(f"Gemini request failed: HTTP {response.status_code}")
    parts = response.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
    raw = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    parsed = json.loads(raw)
    result = []
    used = set()
    for row in parsed.get("stories", []):
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= len(items) or index in used:
            continue
        summary = " ".join(str(row.get("summary_fa", "")).split()).strip()
        if not summary:
            continue
        used.add(index)
        result.append((items[index], summary[:360]))
        if len(result) == 5:
            break
    return result

def birthday_lines(config, now):
    dates = config.get("birthday_dates", {})
    today = now.strftime("%m-%d")
    matches = []
    if isinstance(dates, dict):
        for name, date_value in dates.items():
            if str(date_value).strip() == today:
                matches.append(str(name)[:60])
    if matches:
        return "🎂 <b>تولد امروز:</b> " + "، ".join(html.escape(n) for n in matches) + "\n"
    return "🎂 تولد عزیزان را در تقویم شخصی بررسی کن؛ تاریخ تولدی در تنظیمات کانال ثبت نشده است.\n"

def channel_footer():
    link = os.getenv("STATUS_ROOM_CHANNEL_URL", "").strip()
    if not link.startswith("https://t.me/"):
        if str(CHANNEL).startswith("@"):
            link = "https://t.me/" + str(CHANNEL)[1:]
        else:
            return ""
    return '\n\n📣 <a href="' + html.escape(link, quote=True) + '">عضویت در کانال اتاق وضعیت</a>'

def main():
    if not BOT_TOKEN or not CHANNEL or not GEMINI_KEY:
        raise SystemExit("Missing required Telegram or Gemini secrets.")
    now = datetime.now(TEHRAN)
    items = fetch_last_24h()
    selected = select_and_summarize(items) if items else []

    lines = [
        "🛰️ <b>اتاق وضعیت | تقویم صبحگاهی</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"🌅 <b>صبح به‌خیر | {now:%Y-%m-%d}</b>",
        "<i>مرور مهم‌ترین خبرها و یادآوری‌های کاربردی روز</i>",
        "",
        "📰 <b>مهم‌ترین اخبار ۲۴ ساعت گذشته</b>",
    ]
    if selected:
        for i, (item, summary) in enumerate(selected, 1):
            stamp = datetime.fromtimestamp(item["published_ts"], timezone.utc).astimezone(TEHRAN)
            lines.extend([
                f"\n<b>{i}. {html.escape(item['title'][:260])}</b>",
                html.escape(summary),
                f"🗞 {html.escape(item['source'])} | 🕒 {stamp:%H:%M} تهران",
                f'🔗 <a href="{html.escape(item["url"], quote=True)}">منبع اصلی</a>',
            ])
    else:
        lines.append("⚠️ در خوراک‌های بررسی‌شده خبر تازه و قابل‌استفاده‌ای با زمان انتشار مشخص در ۲۴ ساعت اخیر پیدا نشد؛ خبر قدیمی جایگزین نمی‌شود.")

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━",
        "🌦 <b>آب‌وهوای محل شما</b>",
        'برای دیدن پیش‌بینی محل خودتان، <a href="https://www.windy.com/">نقشه جهانی Windy</a> را باز کنید و گزینه Find my location را بزنید؛ در صورت ندادن مجوز مکان‌یابی، شهر را جست‌وجو کنید.',
        'منبع جایگزین اطلاعات رسمی شهرها: <a href="https://worldweather.wmo.int/en/home.html">سرویس جهانی هواشناسی سازمان جهانی هواشناسی (WMO)</a>.',
        "",
        "🚗 <b>یادآوری مراقبت از خودرو</b>",
        html.escape(CAR_TIPS[now.timetuple().tm_yday % len(CAR_TIPS)]),
        "",
        "📌 <b>کارهای مهم امروز</b>",
        "▫️ سه اولویت اصلی امروز را مشخص کن و کارهای فوری را از کارهای قابل‌تعویق جدا کن.",
        "▫️ قرارها، مهلت‌ها، پیام‌های بی‌پاسخ و پرداخت‌های سررسیدشده را مرور کن.",
        "▫️ یک کار عقب‌افتاده را انتخاب کن و همان امروز برای تمام‌کردنش قدم اول را بردار.",
        "",
        "❤️ <b>خانواده و عزیزان</b>",
        "📞 امروز حال پدر و مادر را بپرس؛ یک تماس کوتاه هم می‌تواند حال دلشان را خوب کند.",
        birthday_lines(load_config(), now).rstrip(),
        "🌷 به یکی از عزیزانی که مدتی از او بی‌خبر بوده‌ای پیام بده.",
        "",
        "━━━━━━━━━━━━━━━━━━",
        "— <b>سایه‌نویس</b> | تحریریه اتاق وضعیت",
        "<i>روزت را با خبر درست و دلِ آسوده شروع کن.</i>",
    ])
    message = "\n".join(lines) + channel_footer()
    # Telegram message limit is 4096 characters. Keep the core sections intact.
    if len(message) > 3900:
        raise RuntimeError(f"Morning briefing exceeds safe Telegram caption length: {len(message)}")
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHANNEL,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=40,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"description": response.text[:500]}
    if not response.ok or not payload.get("ok"):
        raise RuntimeError(f"Telegram send failed: {payload.get('description', payload)}")
    print(f"Morning briefing published. message_id={payload['result']['message_id']}; source_items={len(items)}; selected={len(selected)}")

if __name__ == "__main__":
    main()
