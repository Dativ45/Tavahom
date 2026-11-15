import os
import json
import html
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BOT = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")
STATE = Path("sports_results_state.json")
ESPN = "https://site.api.espn.com/apis/site/v2/sports"
HEADERS = {"User-Agent": "StatusRoomSportsDesk/1.0"}
TIMEOUT = 18
MAX_EVENTS = 12

LEAGUES = [
    ("فوتبال | لیگ برتر انگلیس", "soccer/eng.1"),
    ("فوتبال | لالیگای اسپانیا", "soccer/esp.1"),
    ("فوتبال | بوندس‌لیگای آلمان", "soccer/ger.1"),
    ("فوتبال | سری‌آ ایتالیا", "soccer/ita.1"),
    ("فوتبال | لیگ یک فرانسه", "soccer/fra.1"),
    ("فوتبال | لیگ قهرمانان اروپا", "soccer/uefa.champions"),
    ("فوتبال | لیگ اروپا", "soccer/uefa.europa"),
    ("فوتبال | لیگ MLS آمریکا", "soccer/usa.1"),
    ("بسکتبال | NBA", "basketball/nba"),
    ("هاکی | NHL", "hockey/nhl"),
    ("فوتبال آمریکایی | NFL", "football/nfl"),
    ("بیسبال | MLB", "baseball/mlb"),
]


def load_state():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"seen": []}
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": []}


def save_state(state):
    state["seen"] = state.get("seen", [])[-5000:]
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def scoreboard(path, date_value):
    url = f"{ESPN}/{path}/scoreboard"
    response = requests.get(url, params={"dates": date_value, "limit": 100}, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    return payload.get("events", [])


def extract_event(event, league):
    event_id = str(event.get("id", "")).strip()
    if not event_id:
        return None
    status = event.get("status", {}).get("type", {})
    completed = bool(status.get("completed"))
    state = str(status.get("state", "")).lower()
    # Publish completed scores only; do not mistake scheduled fixtures for results.
    if not completed and state != "post":
        return None
    competitions = event.get("competitions") or []
    if not competitions:
        return None
    competitors = competitions[0].get("competitors") or []
    if len(competitors) < 2:
        return None

    def team(comp):
        item = comp.get("team") or {}
        return {
            "name": item.get("displayName") or item.get("name") or item.get("abbreviation") or "تیم نامشخص",
            "abbr": item.get("abbreviation") or "",
            "score": str(comp.get("score", "—")),
            "winner": bool(comp.get("winner")),
            "home": comp.get("homeAway") == "home",
        }

    teams = [team(x) for x in competitors[:2]]
    teams.sort(key=lambda x: not x["home"])
    date_text = event.get("date", "")
    try:
        kickoff = datetime.fromisoformat(date_text.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%d %b %H:%M UTC")
    except (ValueError, TypeError):
        kickoff = "زمان نامشخص"
    detail = status.get("shortDetail") or status.get("detail") or "پایان‌یافته"
    links = event.get("links") or []
    source_url = next((x.get("href") for x in links if x.get("rel") and "summary" in x.get("rel")), "")
    return {
        "id": event_id,
        "league": league,
        "home": teams[0],
        "away": teams[1],
        "status": detail,
        "date": kickoff,
        "url": source_url,
    }


def collect():
    now = datetime.now(timezone.utc)
    dates = [(now - timedelta(days=1)).strftime("%Y%m%d"), now.strftime("%Y%m%d")]
    events = {}
    for league, path in LEAGUES:
        for date_value in dates:
            try:
                for event in scoreboard(path, date_value):
                    parsed = extract_event(event, league)
                    if parsed:
                        events[parsed["id"]] = parsed
            except requests.RequestException as exc:
                print(f"ESPN feed unavailable [{league} {date_value}]: {str(exc)[:180]}")
            except (ValueError, TypeError, KeyError) as exc:
                print(f"ESPN parse warning [{league}]: {str(exc)[:180]}")
    return sorted(events.values(), key=lambda x: x["date"], reverse=True)


def result_line(event):
    home, away = event["home"], event["away"]
    home_score, away_score = home["score"], away["score"]
    if home_score.isdigit() and away_score.isdigit():
        h, a = int(home_score), int(away_score)
        if h > a:
            outcome = "برد میزبان"
        elif a > h:
            outcome = "برد میهمان"
        else:
            outcome = "مساوی"
    else:
        outcome = "نتیجه ثبت‌شده"
    home_name = html.escape(home["name"][:50])
    away_name = html.escape(away["name"][:50])
    score = f"<b>{html.escape(home_score)}</b>  —  <b>{html.escape(away_score)}</b>"
    link = event.get("url")
    if link and link.startswith("https://"):
        return (
            f"🏟 <b>{home_name}</b>  {score}  <b>{away_name}</b>\n"
            f"   <i>{html.escape(outcome)} | {html.escape(event['date'])}</i> "
            f'· <a href="{html.escape(link, quote=True)}">جزئیات</a>'
        )
    return f"🏟 <b>{home_name}</b>  {score}  <b>{away_name}</b>\n   <i>{html.escape(outcome)} | {html.escape(event['date'])}</i>"


def footer():
    configured = os.getenv("STATUS_ROOM_CHANNEL_URL", "").strip()
    if configured.startswith("https://t.me/"):
        link = configured
    elif str(CHAT).startswith("@"):
        link = "https://t.me/" + str(CHAT)[1:]
    else:
        return ""
    return '\n\n📣 <a href="' + html.escape(link, quote=True) + '">عضویت در اتاق وضعیت</a>'


def publish(events):
    lines = [
        "🏆 <b>اتاق وضعیت | نتایج رسمی مسابقات</b>",
        "━━━━━━━━━━━━━━━━━━",
        "📊 <i>نتایج بازی‌های پایان‌یافته؛ تفکیک‌شده بر اساس لیگ و رشته</i>",
        "",
    ]
    current_league = None
    for event in events:
        if event["league"] != current_league:
            current_league = event["league"]
            lines.append(f"\n<b>🔹 {html.escape(current_league)}</b>")
        lines.append(result_line(event))
    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━",
        "منبع داده: ESPN Scoreboard",
        "زمان‌ها به UTC درج شده‌اند؛ برای نتیجه قطعی، جزئیات مسابقه را باز کنید.",
    ])
    text = "\n".join(lines) + footer()
    # Telegram message hard limit is 4096 chars; keep a safe margin.
    if len(text) > 3900:
        text = text[:3850] + "\n\n<i>برای فهرست کامل، اجرای بعدی را دنبال کنید.</i>" + footer()
    response = requests.post(
        f"https://api.telegram.org/bot{BOT}/sendMessage",
        json={"chat_id": CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=35,
    )
    payload = response.json()
    if not response.ok or not payload.get("ok"):
        raise RuntimeError("Telegram sports publish failed: " + str(payload.get("description", payload))[:300])
    return payload.get("result", {}).get("message_id")


def main():
    if not BOT or not CHAT:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.")
    state = load_state()
    seen = set(state.get("seen", []))
    all_events = collect()
    fresh = [event for event in all_events if event["id"] not in seen]
    if not fresh:
        print("No new completed match results in the monitored ESPN leagues.")
        return
    selected = fresh[:MAX_EVENTS]
    message_id = publish(selected)
    seen.update(event["id"] for event in selected)
    state["seen"] = list(seen)
    save_state(state)
    print(f"Published {len(selected)} completed match result(s), Telegram message_id={message_id}.")
    for event in selected:
        print(f"{event['league']}: {event['home']['name']} {event['home']['score']}-{event['away']['score']} {event['away']['name']}")


if __name__ == "__main__":
    main()
