import os
import re
import json
import html
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote

import feedparser
import requests
from common import post_with_retry

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
STATE_FILE = Path("trend_state.json")
TEHRAN = ZoneInfo("Asia/Tehran")
GOOGLE_TRENDS = "https://trends.google.com/trending/rss?geo={}"
REGIONS = {
    "US": "آمریکا", "GB": "بریتانیا", "DE": "آلمان",
    "JP": "ژاپن", "IN": "هند", "CA": "کانادا",
    "AU": "استرالیا", "FR": "فرانسه",
}
MAX_POSTS = 3
MAX_CANDIDATES = 18
TIMEOUT = 20


def channel_footer():
    configured = os.getenv("STATUS_ROOM_CHANNEL_URL", "").strip()
    if configured.startswith("https://t.me/"):
        link = configured
    else:
        response = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getChat",
            params={"chat_id": CHANNEL}, timeout=TIMEOUT,
        )
        payload = response.json()
        if not response.ok or not payload.get("ok"):
            raise RuntimeError("Telegram getChat failed: " + str(payload.get("description", payload)))
        chat = payload.get("result", {})
        link = ("https://t.me/" + chat["username"]) if chat.get("username") else chat.get("invite_link", "")
        if not link and str(CHANNEL).startswith("@"):
            link = "https://t.me/" + str(CHANNEL)[1:]
    if not link:
        print("WARNING: channel follow link unavailable; set STATUS_ROOM_CHANNEL_URL.")
        return ""
    return '\n\n📣 <b>دنبال‌کردن اتاق وضعیت:</b> <a href="' + html.escape(link, quote=True) + '">عضویت در کانال</a>'


def clean(value):
    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    return html.unescape(re.sub(r"\s+", " ", value)).strip()


def load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("posted", [])
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"posted": []}


def save_state(state):
    state["posted"] = state.get("posted", [])[-1200:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_google_trends():
    by_topic = {}
    for code, country in REGIONS.items():
        url = GOOGLE_TRENDS.format(code)
        try:
            response = requests.get(url, headers={"User-Agent": "StatusRoomTrendRadar/1.0"}, timeout=TIMEOUT)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            for entry in feed.entries[:20]:
                title = clean(entry.get("title"))
                if not title:
                    continue
                key = re.sub(r"[^a-z0-9\u0600-\u06ff]+", " ", title.lower()).strip()
                if not key:
                    continue
                item = by_topic.setdefault(key, {
                    "key": key, "topic": title, "regions": [], "traffic": [],
                    "trend_links": [], "published": [],
                })
                if country not in item["regions"]:
                    item["regions"].append(country)
                traffic = clean(entry.get("ht_approx_traffic", ""))
                if traffic and traffic not in item["traffic"]:
                    item["traffic"].append(traffic)
                link = entry.get("link", "")
                if link.startswith("http") and link not in item["trend_links"]:
                    item["trend_links"].append(link)
                published = clean(entry.get("published", ""))
                if published and published not in item["published"]:
                    item["published"].append(published)
        except Exception as exc:
            print(f"Google Trends feed unavailable [{code}]: {exc}")
    return list(by_topic.values())


def gdelt_coverage(topic):
    # GDELT is used as a news-coverage cross-check, not as proof that a claim is true.
    query = quote('"' + topic[:100] + '"')
    url = (
        "https://api.gdeltproject.org/api/v2/doc/doc"
        f"?query={query}&mode=ArtList&format=json&maxrecords=15&timespan=24h&sort=DateDesc"
    )
    try:
        response = requests.get(url, headers={"User-Agent": "StatusRoomTrendRadar/1.0"}, timeout=TIMEOUT)
        response.raise_for_status()
        data = response.json()
        articles = data.get("articles", []) if isinstance(data, dict) else []
        domains = set()
        examples = []
        for article in articles:
            domain = clean(article.get("domain", "")).lower()
            if domain:
                domains.add(domain.removeprefix("www."))
            title = clean(article.get("title", ""))
            link = article.get("url", "")
            if title and link.startswith("http") and len(examples) < 3:
                examples.append({"title": title[:180], "url": link, "domain": domain})
        return {"count": len(domains), "examples": examples}
    except Exception as exc:
        print(f"GDELT enrichment unavailable [{topic[:60]}]: {exc}")
        return {"count": 0, "examples": []}


def rank_candidates(items):
    now = datetime.now(timezone.utc)
    for item in items:
        # Repeated appearances across countries are a stronger signal; traffic is retained as context.
        item["score"] = len(item["regions"]) * 10 + min(len(item["traffic"]), 5)
        item["fingerprint"] = hashlib.sha256(item["key"].encode("utf-8")).hexdigest()
        item["observed_at_utc"] = now.strftime("%Y-%m-%d %H:%M UTC")
    return sorted(items, key=lambda x: (x["score"], len(x["regions"])), reverse=True)


def select_and_write(candidates):
    compact = [{
        "topic": x["topic"], "regions": x["regions"],
        "traffic_signals": x["traffic"][:4],
        "google_trends_links": x["trend_links"][:2],
        "news_coverage_domains_last_24h": x["gdelt"]["count"],
        "news_examples": x["gdelt"]["examples"][:2],
    } for x in candidates]
    prompt = (
        "You are the careful Persian trend editor for Status Room, an Iranian family-friendly news channel. "
        "Select at most 3 genuinely useful current trend topics from the supplied signals. The output field topic MUST copy the candidate topic text verbatim, character-for-character; never translate or paraphrase it. "
        "Do not invent facts, causes, numbers, quotes, virality, or context. A Google Trends item is a search-interest "
        "signal, NOT proof of a viral event or public opinion. If GDELT coverage is zero, keep the wording explicitly "
        "about search interest only. Prefer cross-region signals, meaningful public-interest topics, technology, "
        "science, sport, culture and safe entertainment. Exclude sexual content, gambling, drugs, graphic violence, "
        "hate, harassment, dangerous challenges and unverified political/military claims. "
        "Return JSON only: {items:[{topic, category, headline_fa, explanation_fa, signal_fa, caution_fa}]}. "
        "category must be one of: جهان، فناوری، ورزش، علم، فرهنگ، سرگرمی، اقتصاد. "
        "headline_fa max 90 Persian characters; explanation_fa 1-2 concise sentences and only what the signals support; "
        "signal_fa must name the countries and say this is a Google search trend; caution_fa may be empty. "
        "If no candidate is safe and meaningful, return an empty items array.\n\n"
        + json.dumps(compact, ensure_ascii=False)
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    response = post_with_retry(
        url,
        headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.15, "responseMimeType": "application/json",
                "maxOutputTokens": 1800,
            },
        },
        timeout=60,
    )
    if not response.ok:
        try:
            detail = response.json().get("error", {}).get("message", "Gemini request failed")
        except (ValueError, AttributeError):
            detail = "Gemini request failed"
        raise RuntimeError(f"Gemini API error ({response.status_code}): {str(detail)[:400]}")
    payload = response.json()
    parts = payload.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    raw = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    if not raw.strip():
        raise RuntimeError("Gemini returned an empty trend selection.")
    parsed = json.loads(raw)
    allowed = {"جهان", "فناوری", "ورزش", "علم", "فرهنگ", "سرگرمی", "اقتصاد"}
    result = []
    for item in parsed.get("items", [])[:MAX_POSTS]:
        if not isinstance(item, dict):
            continue
        category = item.get("category", "جهان")
        if category not in allowed:
            category = "جهان"
        normalized = {
            "topic": clean(item.get("topic", ""))[:180],
            "category": category,
            "headline_fa": clean(item.get("headline_fa", ""))[:180],
            "explanation_fa": clean(item.get("explanation_fa", ""))[:700],
            "signal_fa": clean(item.get("signal_fa", ""))[:400],
            "caution_fa": clean(item.get("caution_fa", ""))[:300],
        }
        if normalized["topic"] and normalized["headline_fa"] and normalized["explanation_fa"]:
            result.append(normalized)
    return result


def build_post(item, source, coverage):
    examples = coverage.get("examples", [])
    links = []
    for example in examples[:2]:
        links.append(
            '• <a href="' + html.escape(example["url"], quote=True) + '">' +
            html.escape(example["title"]) + '</a>'
        )
    trend_link = source.get("trend_links", [])
    if trend_link:
        links.append('• <a href="' + html.escape(trend_link[0], quote=True) + '">نمای سیگنال در Google Trends</a>')
    coverage_line = (
        f"📰 بازتاب خبری در GDELT: {coverage.get('count', 0)} دامنه در ۲۴ ساعت اخیر"
        if coverage.get("count", 0) else
        "📰 بازتاب خبری مستقل در بررسی GDELT پیدا نشد؛ این مورد صرفاً یک سیگنال جست‌وجو است."
    )
    caution = ("\n\n⚠️ " + html.escape(item["caution_fa"])) if item.get("caution_fa") else ""
    source_links = "\n".join(links) if links else "پیوند خبر مکمل در دسترس نبود."
    return (
        "🌐 <b>رادار ترند جهانی | اتاق وضعیت</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>{html.escape(item['headline_fa'])}</b>\n"
        f"🏷 دسته: {html.escape(item['category'])}\n\n"
        f"{html.escape(item['explanation_fa'])}\n\n"
        f"📈 {html.escape(item['signal_fa'])}\n"
        f"{coverage_line}{caution}\n\n"
        "🔎 <b>منابع و پیوندها</b>\n"
        f"{source_links}\n\n"
        "🧾 <i>روش‌شناسی: Google Trends برای تشخیص افزایش جست‌وجو؛ GDELT برای سنجش بازتاب خبری. "
        "ترند بودن به‌تنهایی به معنی صحت ادعا یا محبوبیت عمومی نیست.</i>\n"
        "— <b>اطلس</b> | رادار جهانی اتاق وضعیت"
        + channel_footer()
    )


def publish(message):
    response = post_with_retry(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHANNEL, "text": message, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=35,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"description": response.text[:500]}
    if not response.ok or not payload.get("ok"):
        raise RuntimeError(f"Telegram API error ({response.status_code}): {payload.get('description', payload)}")
    return payload.get("result", {}).get("message_id")


def main():
    if not BOT_TOKEN or not CHANNEL or not GEMINI_KEY:
        raise SystemExit("Missing required Telegram or Gemini secrets.")
    state = load_state()
    trends = rank_candidates(fetch_google_trends())
    if not trends:
        print("No Google Trends items available; nothing published.")
        return
    # Enrich a small top set to keep free public endpoints within reasonable use.
    candidates = []
    for item in trends[:MAX_CANDIDATES]:
        item["gdelt"] = gdelt_coverage(item["topic"])
        candidates.append(item)
    today = datetime.now(TEHRAN).date().isoformat()
    already = set(state.get("posted", []))
    already_fingerprints = {str(value).split(":")[-1] for value in already}
    fresh = [x for x in candidates if x["fingerprint"] not in already_fingerprints]
    if not fresh:
        print("All current trend candidates were already processed today.")
        return
    selected = select_and_write(fresh[:12])
    if not selected:
        print("Editorial filter selected no safe, meaningful trend topics.")
        return
    by_topic = {x["topic"].lower(): x for x in fresh}
    published = 0
    for item in selected[:MAX_POSTS]:
        source = by_topic.get(item["topic"].lower())
        if source is None:
            # Match the normalized Google Trends topic without trusting generated claims.
            source = next((x for x in fresh if item["topic"].lower() in x["topic"].lower()
                           or x["topic"].lower() in item["topic"].lower()), None)
        if source is None:
            print("Skipped AI-selected topic not found in source signals: " + item["topic"])
            continue
        message_id = publish(build_post(item, source, source["gdelt"]))
        already.add(f"{today}:{source['fingerprint']}")
        state["posted"] = list(already)[-1200:]
        state["last_run_tehran"] = datetime.now(TEHRAN).isoformat()
        save_state(state)
        published += 1
        print(f"Published trend [{item['category']}] {source['topic']} message_id={message_id}")
    state["posted"] = list(already)[-1200:]
    state["last_run_tehran"] = datetime.now(TEHRAN).isoformat()
    save_state(state)
    print(f"Published {published} trend post(s).")


if __name__ == "__main__":
    main()
