import os
import re
import json
import html
import requests
from common import post_with_retry
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL = os.getenv("TELEGRAM_CHAT_ID")
STATE_FILE = Path("music_state.json")
TEHRAN = ZoneInfo("Asia/Tehran")
SEARCH_API = "https://archive.org/advancedsearch.php"
META_API = "https://archive.org/metadata/"
TIMEOUT = 30
ALLOWED_LICENSE_MARKERS = (
    "creativecommons.org/publicdomain/zero/1.0",
    "creativecommons.org/licenses/by/3.0",
    "creativecommons.org/licenses/by/4.0",
    "creativecommons.org/licenses/by-sa/3.0",
    "creativecommons.org/licenses/by-sa/4.0",
)

def load_state():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            state.setdefault("posted", [])
            return state
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"posted": []}

def save_state(state):
    state["posted"] = state.get("posted", [])[-500:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def channel_footer():
    configured = os.getenv("STATUS_ROOM_CHANNEL_URL", "").strip()
    if configured.startswith("https://t.me/"):
        link = configured
    else:
        response = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getChat",
                                params={"chat_id": CHANNEL}, timeout=TIMEOUT)
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

def allowed_license(url):
    value = str(url or "").lower().replace("http://", "https://")
    return any(marker in value for marker in ALLOWED_LICENSE_MARKERS)

def search_items(slot):
    # Keyless Internet Archive search; rights are checked again against each item's metadata.
    query = 'mediatype:audio AND licenseurl:("https://creativecommons.org/publicdomain/zero/1.0/" OR "https://creativecommons.org/licenses/by/3.0/" OR "https://creativecommons.org/licenses/by/4.0/" OR "https://creativecommons.org/licenses/by-sa/3.0/" OR "https://creativecommons.org/licenses/by-sa/4.0/")'
    params = {
        "q": query,
        "fl[]": ["identifier", "title", "creator", "licenseurl", "downloads"],
        "rows": 100,
        "page": 1,
        "output": "json",
        "sort[]": "downloads desc",
    }
    response = requests.get(SEARCH_API, params=params, headers={"User-Agent": "StatusRoomMusic/1.0"}, timeout=TIMEOUT)
    response.raise_for_status()
    docs = response.json().get("response", {}).get("docs", [])
    words = ("happy upbeat cheerful acoustic folk piano") if slot == "morning" else ("calm ambient peaceful relaxing piano instrumental")
    wanted = set(words.split())
    def score(item):
        text = (str(item.get("title", "")) + " " + str(item.get("creator", ""))).lower()
        return sum(1 for word in wanted if word in text)
    return sorted(docs, key=score, reverse=True)

def get_licensed_track(items, posted):
    for item in items:
        identifier = str(item.get("identifier", ""))
        if not identifier or identifier in posted:
            continue
        meta_response = requests.get(META_API + quote(identifier, safe=""), headers={"User-Agent": "StatusRoomMusic/1.0"}, timeout=TIMEOUT)
        if meta_response.status_code != 200:
            continue
        metadata = meta_response.json()
        info = metadata.get("metadata", {})
        license_url = info.get("licenseurl") or info.get("license_url") or ""
        if not allowed_license(license_url):
            continue
        files = metadata.get("files", [])
        candidates = []
        for f in files:
            name = str(f.get("name", ""))
            fmt = str(f.get("format", "")).lower()
            if name.lower().endswith(".mp3") and fmt in ("", "mp3", "mpeg audio"):
                try:
                    size = int(f.get("size", 0))
                except (TypeError, ValueError):
                    size = 0
                if 0 < size <= 45 * 1024 * 1024 and not name.startswith("__"):
                    candidates.append((size, name))
        if not candidates:
            continue
        # Prefer a reasonably sized MP3, avoiding oversized albums and tiny previews.
        candidates.sort(key=lambda pair: (pair[0] < 150_000, pair[0]))
        size, filename = candidates[0]
        title = info.get("title") or item.get("title") or identifier
        creator = info.get("creator") or item.get("creator") or ""
        if isinstance(creator, list):
            creator = ", ".join(str(x) for x in creator[:3])
        if not str(creator).strip():
            continue  # Skip items without a reliable creator credit.
        return {
            "id": identifier, "title": str(title), "creator": str(creator),
            "license_url": str(license_url),
            "item_url": "https://archive.org/details/" + quote(identifier, safe=""),
            "audio_url": "https://archive.org/download/" + quote(identifier, safe="") + "/" + quote(filename, safe="/"),
            "filename": filename, "size": size,
        }
    return None

def publish_audio(track, slot):
    title = html.escape(track["title"])
    creator = html.escape(track["creator"])
    license_url = html.escape(track["license_url"], quote=True)
    item_url = html.escape(track["item_url"], quote=True)
    caption = (
        "🎵 <b>موسیقی روز | اتاق وضعیت</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🎧 <b>{title}</b>\n"
        f"👤 صاحب اثر/هنرمند: {creator}\n"
        f"🌅 حال‌وهوا: {'شروع پرانرژی روز' if slot == 'morning' else 'پایان آرام شب'}\n\n"
        "📜 مجوز: اثر دارای مجوز آزاد قابل‌بازنشر؛ نام صاحب اثر و منبع درج شده است.\n"
        f"🔗 <a href=\"{item_url}\">صفحه و مشخصات اثر</a> | "
        f"<a href=\"{license_url}\">متن مجوز</a>\n"
        "— <b>ماه‌رخ</b> | موسیقی و فرهنگ"
        + channel_footer()
    )
    audio_response = requests.get(track["audio_url"], headers={"User-Agent": "StatusRoomMusic/1.0"}, timeout=90)
    audio_response.raise_for_status()
    audio_bytes = audio_response.content
    if not audio_bytes or len(audio_bytes) > 45 * 1024 * 1024:
        raise RuntimeError("Audio file is empty or exceeds the safe 45 MB Telegram upload limit.")
    files = {"audio": (track["filename"], audio_bytes, "audio/mpeg")}
    data = {"chat_id": CHANNEL, "caption": caption, "parse_mode": "HTML",
            "title": track["title"][:64], "performer": track["creator"][:64]}
    response = post_with_retry(f"https://api.telegram.org/bot{BOT_TOKEN}/sendAudio",
                             data=data, files=files, timeout=120)
    try:
        payload = response.json()
    except ValueError:
        payload = {"description": response.text[:400]}
    if not response.ok or not payload.get("ok"):
        raise RuntimeError(f"Telegram sendAudio failed ({response.status_code}): {payload.get('description', payload)}")
    return payload.get("result", {}).get("message_id")

def main():
    if not BOT_TOKEN or not CHANNEL:
        raise SystemExit("Missing Telegram secrets.")
    now = datetime.now(TEHRAN)
    slot = "morning" if 4 <= now.hour < 12 else "night"
    state = load_state()
    posted = set(str(x) for x in state.get("posted", []))
    items = search_items(slot)
    track = get_licensed_track(items, posted)
    if not track:
        print("No unused Internet Archive MP3 passed the explicit redistribution-license and file-size filters.")
        return
    message_id = publish_audio(track, slot)
    posted.add(track["id"])
    state["posted"] = sorted(posted)
    state["last_track_id"] = track["id"]
    state["last_posted_at_tehran"] = now.isoformat()
    state["last_source"] = "Internet Archive"
    save_state(state)
    print(f"Published licensed Internet Archive audio: {track['title']} / {track['creator']} message_id={message_id}")

if __name__ == "__main__":
    main()
