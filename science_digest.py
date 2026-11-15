import os
import json
import re
import html
import hashlib
import time
import calendar
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests

BOT = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")
KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
STATE = Path("science_state.json")
MAX_SEEN = 3000
MAX_AGE = 96 * 3600
HEADERS = {"User-Agent": "StatusRoomNewsBot/1.0 (+https://t.me/StatusRoomNews)"}

# ScienceDaily is a press-release aggregator; preprint servers are explicitly labelled.
FEEDS = [
    ("Nature", "https://www.nature.com/nature.rss", "editorial"),
    ("Science (AAAS)", "https://www.science.org/rss/news_current.xml", "editorial"),
    ("STAT News", "https://www.statnews.com/feed/", "editorial"),
    ("Quanta Magazine", "https://www.quantamagazine.org/feed/", "editorial"),
    ("ScienceDaily", "https://www.sciencedaily.com/rss/top/science.xml", "press_release_aggregator"),
    ("ScienceDaily Health", "https://www.sciencedaily.com/rss/top/health.xml", "press_release_aggregator"),
    ("bioRxiv preprints", "https://connect.biorxiv.org/biorxiv_xml.php?subject=all", "preprint"),
    ("medRxiv preprints", "https://connect.medrxiv.org/medrxiv_xml.php?subject=all", "preprint"),
]

def clean(value):
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or ""))).strip()

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
    for source, url, kind in FEEDS:
        try:
            feed = feedparser.parse(url, request_headers=HEADERS)
            if getattr(feed, "bozo", False):
                print(f"Feed warning [{source}]: {str(getattr(feed, 'bozo_exception', 'invalid feed'))[:160]}")
            for entry in feed.entries[:35]:
                title = clean(entry.get("title"))
                link = entry.get("link", "")
                if not title or not link.startswith(("http://", "https://")):
                    continue
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                timestamp = calendar.timegm(published) if published else 0
                if timestamp and (now - timestamp > MAX_AGE or timestamp > now + 3600):
                    continue
                summary = clean(entry.get("summary", entry.get("description", "")))[:2400]
                key = hashlib.sha256((title + link).encode("utf-8")).hexdigest()
                items.append({
                    "source": source, "kind": kind, "title": title[:450],
                    "summary": summary, "link": link, "timestamp": timestamp, "key": key,
                })
        except Exception as exc:
            print(f"Feed error [{source}]: {str(exc)[:180]}")
    return items

def translate(item):
    prompt = (
        "You are the cautious science and health editor of a bilingual Persian news channel. "
        "Return valid JSON only with keys title_fa, summary_fa, field. Write natural Iranian Persian. "
        "Never invent results, sample sizes, causal claims, clinical recommendations, DOI, or quotes. "
        "Preserve study limitations, uncertainty, species (animal vs human), sample size and whether "
        "the evidence is observational or experimental when stated. Never use sensational words such "
        "as breakthrough, cure, reverse or درمان قطعی unless the supplied source explicitly supports "
        "the exact wording; prefer measured language. Do not turn association into causation. "
        "For preprints, state prominently that the study has not yet undergone peer review. "
        "For ScienceDaily, state that this is a university/institution press-release-based report, "
        "not independent reporting. If the excerpt is sparse, say the feed provides limited detail. "
        "Choose field from: پزشکی و سلامت، زیست‌شناسی، فیزیک و فضا، اقلیم و محیط زیست، "
        "ریاضیات و علوم رایانه، پژوهش دانشگاهی، سایر علوم. "
        "Keep summary to 2-3 concise sentences.\n"
        + json.dumps({"source": item["source"], "source_kind": item["kind"],
                      "headline": item["title"], "excerpt": item["summary"]}, ensure_ascii=False)
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    response = requests.post(
        url, headers={"x-goog-api-key": KEY, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json", "maxOutputTokens": 850}},
        timeout=50,
    )
    if not response.ok:
        try:
            detail = response.json().get("error", {}).get("message", "Gemini request failed")
        except ValueError:
            detail = "Gemini request failed"
        raise RuntimeError(f"Gemini API error ({response.status_code}): {str(detail)[:300]}")
    parts = response.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
    raw = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    if not raw.strip():
        raise RuntimeError("Gemini returned an empty science translation.")
    data = json.loads(raw)
    allowed = {"پزشکی و سلامت", "زیست‌شناسی", "فیزیک و فضا", "اقلیم و محیط زیست",
               "ریاضیات و علوم رایانه", "پژوهش دانشگاهی", "سایر علوم"}
    field = clean(str(data.get("field", "")))
    if field not in allowed:
        field = "پژوهش دانشگاهی"
    title = clean(str(data.get("title_fa", ""))) or item["title"]
    summary = clean(str(data.get("summary_fa", ""))) or "خوراک منبع جزئیات کافی برای خلاصه‌سازی ارائه نکرده است."
    return title[:300], summary[:1100], field

def footer():
    configured = os.getenv("STATUS_ROOM_CHANNEL_URL", "").strip()
    if configured.startswith("https://t.me/"):
        return '\n\n📣 <a href="' + html.escape(configured, quote=True) + '">عضویت در اتاق وضعیت</a>'
    if str(CHAT).startswith("@"):
        link = "https://t.me/" + str(CHAT)[1:]
        return '\n\n📣 <a href="' + html.escape(link, quote=True) + '">عضویت در اتاق وضعیت</a>'
    return ""

def post(item, title_fa, summary_fa, field):
    timestamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(item["timestamp"])) if item["timestamp"] else "زمان انتشار در خوراک مشخص نیست"
    labels = []
    if item["kind"] == "preprint":
        labels.append("⚠️ <b>پیش‌چاپ؛ هنوز داوری همتا نشده</b>")
    elif item["kind"] == "press_release_aggregator":
        labels.append("ℹ️ <b>بر پایه بیانیه مطبوعاتی/اطلاعیه پژوهشی؛ نه گزارش مستقل</b>")
    else:
        labels.append("ℹ️ <b>خلاصه بر پایه خوراک منبع؛ جزئیات و روش پژوهش را در اصل گزارش بررسی کنید</b>")
    doi_match = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", item["summary"] + " " + item["link"], re.I)
    doi_line = ("\n🔎 DOI: " + html.escape(doi_match.group(0).rstrip(".,;")) if doi_match else "")
    caption = (
        "🔬 <b>اتاق وضعیت | علم و سلامت</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        + labels[0] + "\n"
        + f"🏷 <b>{html.escape(field)}</b>\n\n"
        + f"🇮🇷 <b>{html.escape(title_fa[:220])}</b>\n"
        + html.escape(summary_fa[:700]) + "\n\n"
        + "🌐 <b>Original (English)</b>\n"
        + html.escape(item["title"][:240])
        + ("\n" + html.escape(item["summary"][:700]) if item["summary"] else "")
        + "\n\n━━━━━━━━━━━━━━━━━━\n"
        + f"🗞 منبع: {html.escape(item['source'])}\n"
        + f"🕒 زمان خوراک: {html.escape(timestamp)}"
        + doi_line + "\n"
        + f"🔗 <a href=\"{html.escape(item['link'], quote=True)}\">گزارش/پژوهش اصلی</a>"
        + footer()
    )
    response = requests.post(
        f"https://api.telegram.org/bot{BOT}/sendMessage",
        json={"chat_id": CHAT, "text": caption, "parse_mode": "HTML", "disable_web_page_preview": False},
        timeout=35,
    )
    payload = response.json()
    if not response.ok or not payload.get("ok"):
        raise RuntimeError("Telegram send failed: " + str(payload.get("description", payload))[:300])
    return payload.get("result", {}).get("message_id")

def main():
    if not all([BOT, CHAT, KEY]):
        raise SystemExit("Missing Telegram or Gemini secrets.")
    state = load_state()
    seen = set(state.get("seen", []))
    items = [x for x in collect() if x["key"] not in seen]
    if not items:
        print("No new science stories.")
        return
    # Prefer newsroom/research-editorial reporting; use preprints only when no editorial item is available.\n    items.sort(key=lambda x: (x["kind"] == "preprint", -x["timestamp"]))
    item = items[0]
    title_fa, summary_fa, field = translate(item)
    message_id = post(item, title_fa, summary_fa, field)
    seen.add(item["key"])
    state["seen"] = list(seen)
    save_state(state)
    print(f"Published science story message_id={message_id}, source={item['source']}, kind={item['kind']}, title={item['title']}")

if __name__ == "__main__":
    main()
