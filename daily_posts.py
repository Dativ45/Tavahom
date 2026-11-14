import os
import html
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

def channel_footer():
    configured = os.getenv("STATUS_ROOM_CHANNEL_URL", "").strip()
    if configured.startswith("https://t.me/"):
        link = configured
    else:
        r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getChat", params={"chat_id": CHANNEL}, timeout=20)
        p = r.json()
        if not r.ok or not p.get("ok"):
            raise RuntimeError("Telegram getChat failed: " + str(p.get("description", p)))
        chat = p.get("result", {})
        link = ("https://t.me/" + chat["username"]) if chat.get("username") else chat.get("invite_link", "")
        if not link and str(CHANNEL).startswith("@"):
            link = "https://t.me/" + str(CHANNEL)[1:]
    if not link:
        print("WARNING: Set STATUS_ROOM_CHANNEL_URL to a public channel or invite link.")
        return ""
    return '\\n\\n📣 <b>دنبال‌کردن اتاق وضعیت:</b> <a href="' + html.escape(link, quote=True) + '">عضویت در کانال</a>'

if not BOT_TOKEN or not CHANNEL or not GEMINI_KEY:
    raise SystemExit("Missing required Telegram or Gemini secrets.")

now = datetime.now(ZoneInfo("Asia/Tehran"))
slot = "صبح" if 4 <= now.hour < 12 else ("ظهر" if 12 <= now.hour < 17 else "شب")
if slot == "صبح":
    brief = (
        "یک پیام صبحگاهی فارسی برای کانال خبری Status Room بنویس؛ گرم، کوتاه، "
        "هوشمند و کمی بانمک. حداکثر ۳ جمله و مناسب خانواده. "
        "از نصیحت کلیشه‌ای، ادعای مناسبت خاص و نقل‌قول ساختگی پرهیز کن."
    )
elif slot == "ظهر":
    brief = (
        "یک پیام کوتاه ظهرگاهی فارسی برای کانال خبری Status Room بنویس؛ تازه‌نفس، مثبت و کمی بانمک، " 
        "حداکثر ۳ جمله، مناسب خانواده و بدون نصیحت کلیشه‌ای یا نقل‌قول ساختگی."
    )
else:
    brief = (
        "یک پیام شبانه فارسی برای کانال خبری Status Room بنویس؛ آرام، امیدبخش، "
        "کوتاه و کمی شاعرانه، بدون اغراق و مناسب خانواده. حداکثر ۳ جمله. "
        "از ادعای مناسبت خاص و نقل‌قول ساختگی پرهیز کن."
    )

url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
response = requests.post(
    url,
    headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
    json={
        "contents": [{"parts": [{"text": brief}]}],
        "generationConfig": {"temperature": 0.75, "maxOutputTokens": 220},
    },
    timeout=45,
)
if not response.ok:
    raise SystemExit(f"Gemini error: HTTP {response.status_code}")
data = response.json()
parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
message = " ".join(p.get("text", "").strip() for p in parts if p.get("text")).strip()
if not message:
    raise SystemExit("Gemini returned an empty greeting.")

prefix = {"صبح":"🌅 <b>صبح به‌خیر</b>","ظهر":"☀️ <b>ظهر به‌خیر</b>","شب":"🌙 <b>شب‌به‌خیر</b>"}[slot]
post = (
    "🛰️ <b>اتاق وضعیت | STATUS ROOM</b>\n"
    "━━━━━━━━━━━━━━━━━━\n"
    f"{prefix}\n\n"
    f"{html.escape(message)}\n\n"
    "— <b>سایه‌نویس</b> | تحریریه اتاق وضعیت\n"
    "<i>نام‌ها پنهان می‌مانند؛ حقیقت نه.</i>"
    + channel_footer()
)
tg = requests.post(
    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
    json={"chat_id": CHANNEL, "text": post, "parse_mode": "HTML", "disable_web_page_preview": True},
    timeout=35,
)
result = tg.json()
if not tg.ok or not result.get("ok"):
    raise SystemExit(f"Telegram error: {result.get('description', 'send failed')}")
print(f"Published {slot} greeting to Telegram. message_id={result['result']['message_id']}")
