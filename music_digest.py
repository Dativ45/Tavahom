import os
import re
import json
import html
import requests
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL = os.getenv("TELEGRAM_CHAT_ID")
JAMENDO_CLIENT_ID = os.getenv("JAMENDO_CLIENT_ID")
STATE_FILE = Path("music_state.json")
TEHRAN = ZoneInfo("Asia/Tehran")
API = "https://api.jamendo.com/v3.0/tracks/"
TIMEOUT = 25
# Only licenses that permit redistribution and commercial use are considered.
# NC (non-commercial), ND (no derivatives), and unknown licenses are excluded.
ALLOWED_LICENSES = (
    "creativecommons.org/licenses/by/3.0/",
    "creativecommons.org/licenses/by/4.0/",
    "creativecommons.org/licenses/by-sa/3.0/",
    "creativecommons.org/licenses/by-sa/4.0/",
    "creativecommons.org/publicdomain/zero/1.0/",
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


def allowed_license(track):
    license_url = str(track.get("license_ccurl", "")).lower().replace("http://", "https://")
    return any(item in license_url for item in ALLOWED_LICENSES)


def fetch_tracks(slot):
    # Different moods for the start and end of the day; the API returns metadata and license.
    tags = "happy+acoustic+instrumental" if slot == "morning" else "calm+ambient+instrumental"
    params = {
        "client_id": JAMENDO_CLIENT_ID,
        "format": "json",
        "limit": 100,
        "order": "popularity_week",
        "fuzzytags": tags,
        "include": "musicinfo",
        "audioformat": "mp32",
        "audiodlformat": "mp32",
    }
    response = requests.get(API, params=params, headers={"User-Agent": "StatusRoomMusic/1.0"}, timeout=TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if payload.get("headers", {}).get("status") != "success":
        raise RuntimeError("Jamendo API did not return success: " + str(payload.get("headers", {})))
    return payload.get("results", [])


def choose_track(tracks, posted):
    for track in tracks:
        track_id = str(track.get("id", ""))
        if not track_id or track_id in posted:
            continue
        if track.get("audiodownload_allowed") is not True:
            continue
        if not track.get("audiodownload", "").startswith("https://"):
            continue
        if not allowed_license(track):
            continue
        if not track.get("name") or not track.get("artist_name") or not track.get("shareurl"):
            continue
        return track
    return None


def duration_text(seconds):
    try:
        seconds = int(seconds)
        return f"{seconds // 60}:{seconds % 60:02d}"
    except (TypeError, ValueError):
        return "نامشخص"


def publish_audio(track, slot):
    artist = html.escape(str(track["artist_name"]))
    title = html.escape(str(track["name"]))
    license_url = str(track["license_ccurl"]).replace("http://", "https://")
    share_url = str(track["shareurl"])
    caption = (
        "🎵 <b>موسیقی روز | اتاق وضعیت</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🎧 <b>{title}</b>\n"
        f"👤 هنرمند: {artist}\n"
        f"🕒 مدت: {duration_text(track.get('duration'))}\n"
        f"🌅 حال‌وهوا: {'شروع پرانرژی روز' if slot == 'morning' else 'پایان آرام شب'}\n\n"
        "📜 مجوز: Creative Commons با اجازه بازنشر تجاری؛ نام هنرمند و اثر طبق شرط انتساب درج شده است.\n"
        f"🔗 <a href=\"{html.escape(share_url, quote=True)}\">صفحه رسمی اثر در Jamendo</a> | "
        f"<a href=\"{html.escape(license_url, quote=True)}\">متن مجوز</a>\n"
        "— <b>ماه‌رخ</b> | موسیقی و فرهنگ"
        + channel_footer()
    )
    audio_url = track["audiodownload"]
    audio_response = requests.get(audio_url, headers={"User-Agent": "StatusRoomMusic/1.0"}, timeout=60, stream=True)
    audio_response.raise_for_status()
    content_type = audio_response.headers.get("Content-Type", "").lower()
    if "audio" not in content_type and "octet-stream" not in content_type:
        raise RuntimeError("Jamendo download did not return an audio content type.")
    files = {"audio": (f"{track['id']}.mp3", audio_response.raw, "audio/mpeg")}
    data = {"chat_id": CHANNEL, "caption": caption, "parse_mode": "HTML", "title": str(track["name"])[:64],
            "performer": str(track["artist_name"])[:64]}
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendAudio",
        data=data, files=files, timeout=120,
    )
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
    if not JAMENDO_CLIENT_ID:
        print("JAMENDO_CLIENT_ID is not configured; music post skipped safely. Add it as a GitHub Actions secret.")
        return
    now = datetime.now(TEHRAN)
    slot = "morning" if 4 <= now.hour < 12 else "night"
    state = load_state()
    posted = set(str(x) for x in state.get("posted", []))
    tracks = fetch_tracks(slot)
    track = choose_track(tracks, posted)
    if not track:
        print("No unused Jamendo track passed both download permission and redistribution-license filters.")
        return
    message_id = publish_audio(track, slot)
    posted.add(str(track["id"]))
    state["posted"] = sorted(posted)
    state["last_track_id"] = str(track["id"])
    state["last_posted_at_tehran"] = now.isoformat()
    save_state(state)
    print(f"Published licensed Jamendo audio: {track['name']} / {track['artist_name']} message_id={message_id}")


if __name__ == "__main__":
    main()

# Scoped syntax validation trigger.
