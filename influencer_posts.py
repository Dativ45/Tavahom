import os
import json
import html
import re
import time
import calendar
import hashlib
from pathlib import Path
from urllib.parse import quote

import feedparser
import requests

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
STATE_FILE = Path("influencer_state.json")
MAX_AGE_SECONDS = 36 * 60 * 60
MAX_SEEN = 3000
MAX_POST_CHARS = 1800
MIN_EDITORIAL_SCORE = 2

IMPORTANT_TERMS = {
    "war", "attack", "strike", "missile", "drone", "military", "troops",
    "ceasefire", "peace", "nuclear", "sanction", "election", "vote",
    "president", "prime minister", "parliament", "government", "minister",
    "resign", "appoint", "arrest", "court", "ruling", "law", "policy",
    "tariff", "trade deal", "inflation", "interest rate", "central bank",
    "crisis", "earthquake", "flood", "fire", "outbreak", "pandemic",
    "artificial intelligence", "ai regulation", "chip", "export control",
    "immigration", "visa", "refugee", "border", "diplomatic",
}

# Curated starter list. Each entry is an individual/public figure or official office.
# FxTwitter's public RSS bridge is unofficial and may become unavailable.
ACCOUNTS = [
    ("The White House", "whitehouse", "سیاست و دولت"),
    ("Narendra Modi", "narendramodi", "سیاست و دیپلماسی"),
    ("Ursula von der Leyen", "vonderleyen", "سیاست و دیپلماسی"),
    ("Emmanuel Macron", "EmmanuelMacron", "سیاست و دیپلماسی"),
    ("António Guterres", "antonioguterres", "دیپلماسی و سازمان ملل"),
    ("Elon Musk", "elonmusk", "فناوری و کسب‌وکار"),
    ("Bill Gates", "BillGates", "علم و سلامت"),
    ("Sam Altman", "sama", "هوش مصنوعی و فناوری"),
    ("Jensen Huang", "JensenHuang", "فناوری و تراشه"),
    ("Dario Amodei", "DarioAmodei", "هوش مصنوعی و سیاست فناوری"),
]

def clean(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    return html.unescape(re.sub(r"\s+", " ", value)).strip()

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

def fetch_recent_posts(state):
    seen = set(state.get("seen", []))
    posts = []
    now = int(time.time())
    headers = {"User-Agent": "StatusRoom/1.0 (+https://github.com/Dativ45/Tavahom)"}
    for display_name, handle, desk in ACCOUNTS:
        url = f"https://fxtwitter.com/{quote(handle)}/feed.xml"
        try:
            response = requests.get(url, headers=headers, timeout=(5, 12))
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            if getattr(feed, "bozo", False) and not feed.entries:
                print(f"Feed warning [{handle}]: {getattr(feed, 'bozo_exception', 'invalid RSS')}")
                continue
            for entry in feed.entries[:12]:
                link = entry.get("link", "")
                text = clean(entry.get("title") or entry.get("summary") or entry.get("description", ""))
                if not text or not link.startswith("http") or text.lower().startswith("rt @"):
                    continue
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                published_ts = calendar.timegm(published) if published else 0
                if not published_ts or now - published_ts > MAX_AGE_SECONDS or published_ts > now + 300:
                    continue
                key = hashlib.sha256((handle + "|" + (entry.get("id") or link)).encode("utf-8")).hexdigest()
                if key in seen:
                    continue
                posts.append({
                    "name": display_name,
                    "handle": handle,
                    "desk": desk,
                    "text": text[:MAX_POST_CHARS],
                    "link": link,
                    "published_ts": published_ts,
                    "key": key,
                })
        except requests.RequestException as exc:
            print(f"Feed unavailable [{handle}]: {exc}")
        except Exception as exc:
            print(f"Feed parse error [{handle}]: {exc}")
    for post in posts:
        post["editorial_score"] = editorial_score(post)
    posts = [post for post in posts if post["editorial_score"] >= MIN_EDITORIAL_SCORE]
    posts.sort(key=lambda item: (item["editorial_score"], item["published_ts"]), reverse=True)
    print(f"Editorial filter retained {len(posts)} high-signal post(s).")
    return posts

def editorial_score(post):
    text = post["text"].lower()
    score = sum(2 if len(term.split()) > 1 else 1 for term in IMPORTANT_TERMS if term in text)
    if len(text) < 100:
        score -= 2
    if any(term in text for term in ("like and subscribe", "giveaway", "promo code", "happy birthday")):
        score -= 3
    return score


def translate_post(post):
    prompt = (
        "Translate this public social-media post faithfully into natural Iranian Persian. "
        "Return valid JSON only with keys title_fa and translation_fa. "
        "Do not add facts, context, claims, quotes, or certainty. Keep names, numbers, and nuance accurate. "
        "title_fa is a short neutral Persian headline describing that this person posted/commented; "
        "translation_fa is a faithful translation, not an endorsement.\n\n"
        + json.dumps({"author": post["name"], "handle": post["handle"], "post": post["text"]}, ensure_ascii=False)
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    last_error = None
    for attempt in range(2):
        try:
            response = requests.post(
                url,
                headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json", "maxOutputTokens": 900},
                },
                timeout=45,
            )
            if not response.ok:
                raise RuntimeError(f"Gemini HTTP {response.status_code}")
            payload = response.json()
            parts = payload.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            raw = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
            data = json.loads(raw)
            title = clean(str(data.get("title_fa", "")))
            translation = clean(str(data.get("translation_fa", "")))
            if not title or not translation:
                raise RuntimeError("Gemini returned incomplete translation")
            return title[:250], translation[:2200]
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(2)
    raise RuntimeError(f"Translation failed: {last_error}")

def publish(message):
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHANNEL, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=30,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"description": response.text[:500]}
    if not response.ok or not payload.get("ok"):
        raise RuntimeError(f"Telegram API error ({response.status_code}): {payload.get('description', 'unknown')}")
    return payload.get("result", {}).get("message_id")

def main():
    if not BOT_TOKEN or not CHANNEL or not GEMINI_KEY:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, or GEMINI_API_KEY")
    state = load_state()
    posts = fetch_recent_posts(state)
    if not posts:
        print("No new influencer posts found in the monitored feeds.")
        return
    post = posts[0]
    title_fa, translation_fa = translate_post(post)
    published = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(post["published_ts"]))
    message = (
        "🌐 <b>صدای اثرگذاران جهان</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <b>اظهارنظر مستقیم | نه خبر تأییدشده</b>\n"
        "این متن بازتاب دیدگاه منتشرشده از حساب معرفی‌شده است؛ صحت ادعاهای مطرح‌شده مستقلاً تأیید نشده است.\n\n"
        f"👤 <b>{html.escape(post['name'])}</b> · <a href=\"https://x.com/{html.escape(post['handle'], quote=True)}\">@{html.escape(post['handle'])}</a>\n"
        f"🗂 حوزه: {html.escape(post['desk'])}\n\n"
        f"🇮🇷 <b>{html.escape(title_fa)}</b>\n{html.escape(translation_fa)}\n\n"
        "🌍 <b>Original post</b>\n"
        f"{html.escape(post['text'][:MAX_POST_CHARS])}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🕒 زمان انتشار: {html.escape(published)}\n"
        f"🔗 <a href=\"{html.escape(post['link'], quote=True)}\">مشاهده پست اصلی در X</a>\n"
        "— <b>تحریریه صدای اثرگذاران</b> | اتاق وضعیت"
    )
    message_id = publish(message)
    state.setdefault("seen", []).append(post["key"])
    save_state(state)
    print(f"Published influencer post from @{post['handle']} message_id={message_id}")

if __name__ == "__main__":
    main()
