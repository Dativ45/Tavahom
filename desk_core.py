"""Shared, conservative publishing primitives for Tavahom desks."""
from __future__ import annotations
import hashlib, html, json, os, random, re, tempfile, time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
import feedparser, requests

DEFAULT_TIMEOUT = (5, 20)
MAX_TELEGRAM_TEXT = 4096
RETRY_STATUSES = {429, 500, 502, 503, 504}

class DeskError(RuntimeError): pass
class DeliveryUncertain(DeskError): pass

def safe_error(value: Any, *secrets: str) -> str:
    text = str(value)
    for secret in secrets:
        if secret: text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"https://api\.telegram\.org/bot[^/\s]+", "https://api.telegram.org/bot[REDACTED]", text)
    text = re.sub(r"([?&](?:key|api_key|token)=)[^&\s]+", r"\1[REDACTED]", text, flags=re.I)
    return text[:1200]

def clean_html(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", value or ""))).strip()

def canonical_url(value: str) -> str:
    p = urlsplit((value or "").strip())
    if p.scheme.lower() not in {"http", "https"} or not p.netloc: return ""
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", p.query, ""))

def item_key(title: str, link: str) -> str:
    title = re.sub(r"\s+", " ", (title or "").strip().casefold())
    return hashlib.sha256((title + "|" + canonical_url(link)).encode()).hexdigest()

def fetch_feed(url: str, *, timeout=DEFAULT_TIMEOUT, session=None):
    response = (session or requests).get(url, timeout=timeout, headers={"User-Agent":"TavahomDesk/2.0"})
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        raise DeskError("Feed parse failed: " + safe_error(getattr(parsed, "bozo_exception", "invalid feed")))
    out = []
    for entry in parsed.entries:
        title, link = clean_html(entry.get("title", "")), canonical_url(entry.get("link", ""))
        if title and link:
            image = None
            for candidate in (entry.get("media_content", []) or []) + (entry.get("media_thumbnail", []) or []) + (entry.get("enclosures", []) or []):
                if isinstance(candidate, dict):
                    raw = candidate.get("url") or candidate.get("href")
                    if raw and urlsplit(raw).scheme in {"http", "https"} and urlsplit(raw).netloc:
                        image = raw
                        break
            out.append({"title":title[:500], "link":link,
                        "summary":clean_html(entry.get("summary", entry.get("description", "")))[:4000],
                        "image":image})
    return out

def _delay(attempt, retry_after=None):
    try:
        if retry_after: return min(max(float(retry_after), 0), 60)
    except (TypeError, ValueError): pass
    return min(2 ** attempt + random.uniform(0, .5), 30)

def gemini_json(prompt: str, *, api_key=None, model=None, timeout=DEFAULT_TIMEOUT, max_retries=3, session=None):
    key = api_key or os.getenv("GEMINI_API_KEY")
    model = model or os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
    if not key: raise DeskError("Missing GEMINI_API_KEY")
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {"contents":[{"parts":[{"text":prompt}]}],
               "generationConfig":{"temperature":0.1,"responseMimeType":"application/json"}}
    client, response = session or requests, None
    for attempt in range(max_retries):
        try:
            response = client.post(endpoint, headers={"x-goog-api-key":key,"Content-Type":"application/json"},
                                   json=payload, timeout=timeout)
        except requests.Timeout as exc:
            if attempt + 1 == max_retries: raise DeskError("Gemini request timed out") from exc
            time.sleep(_delay(attempt)); continue
        except requests.RequestException as exc:
            raise DeskError("Gemini network error: " + safe_error(exc, key)) from exc
        if response.status_code in RETRY_STATUSES and attempt + 1 < max_retries:
            time.sleep(_delay(attempt, response.headers.get("Retry-After"))); continue
        if not response.ok:
            try: detail = response.json().get("error",{}).get("message","Gemini API error")
            except (ValueError, AttributeError): detail = "Gemini API error"
            raise DeskError(f"Gemini HTTP {response.status_code}: {safe_error(detail,key)}")
        break
    try:
        parts = response.json()["candidates"][0]["content"]["parts"]
        result = json.loads("".join(p.get("text","") for p in parts if isinstance(p,dict)))
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise DeskError("Gemini returned invalid or incomplete JSON") from exc
    if not isinstance(result, dict): raise DeskError("Gemini JSON must be an object")
    return result

def telegram_send_photo(photo_url: str, caption: str, *, bot_token=None, chat_id=None, timeout=DEFAULT_TIMEOUT, session=None):
    token, target = bot_token or os.getenv("TELEGRAM_BOT_TOKEN"), chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not target: raise DeskError("Missing Telegram credentials")
    if not photo_url.startswith(("https://", "http://")): raise DeskError("Invalid photo URL")
    if len(caption) > 1024: raise DeskError("Telegram photo caption exceeds 1024 characters")
    endpoint = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        response = (session or requests).post(endpoint, json={"chat_id":target,"photo":photo_url,
                    "caption":caption,"parse_mode":"HTML"}, timeout=timeout)
    except requests.RequestException as exc:
        raise DeliveryUncertain("Telegram photo send outcome unknown; reconcile before retrying") from exc
    try: payload = response.json()
    except ValueError as exc: raise DeliveryUncertain("Telegram photo response was non-JSON") from exc
    if response.status_code >= 500: raise DeliveryUncertain("Telegram photo server error; delivery may be unknown")
    if not response.ok or not payload.get("ok"):
        raise DeskError("Telegram photo rejected: " + safe_error(payload.get("description","API error")))
    result=payload.get("result")
    if not isinstance(result,dict) or "message_id" not in result:
        raise DeliveryUncertain("Telegram photo response lacked message_id")
    return result

def telegram_send(text: str, *, bot_token=None, chat_id=None, timeout=DEFAULT_TIMEOUT, session=None):
    token, target = bot_token or os.getenv("TELEGRAM_BOT_TOKEN"), chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not target: raise DeskError("Missing Telegram credentials")
    if not isinstance(text,str) or not text.strip(): raise DeskError("Telegram message is empty")
    if len(text) > MAX_TELEGRAM_TEXT: raise DeskError("Telegram message exceeds 4096 characters")
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = (session or requests).post(endpoint,json={"chat_id":target,"text":text,"parse_mode":"HTML",
                                    "disable_web_page_preview":False},timeout=timeout)
    except requests.RequestException as exc:
        raise DeliveryUncertain("Telegram send outcome unknown; reconcile before retrying") from exc
    try: payload = response.json()
    except ValueError as exc: raise DeliveryUncertain("Telegram returned non-JSON; reconcile before retrying") from exc
    if response.status_code == 429: raise DeskError("Telegram rate limited the send")
    if not response.ok or not payload.get("ok"):
        if response.status_code >= 500: raise DeliveryUncertain("Telegram server error; delivery outcome may be unknown")
        raise DeskError("Telegram rejected send: " + safe_error(payload.get("description","API error")))
    result = payload.get("result")
    if not isinstance(result,dict) or "message_id" not in result:
        raise DeliveryUncertain("Telegram response lacked message_id")
    return result

class DeskState:
    """Atomic per-desk state. Pending/unknown items are never blindly resent."""
    def __init__(self, path, *, max_seen=5000):
        self.path, self.max_seen = Path(path), max_seen
        self.data = {"items":{}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(loaded,dict) or not isinstance(loaded.get("items",{}),dict): raise ValueError()
                self.data = {"items":loaded.get("items",{})}
            except (OSError,json.JSONDecodeError,ValueError) as exc:
                raise DeskError(f"Invalid state preserved for inspection: {self.path}") from exc
    def status(self,key): return self.data["items"].get(key,{}).get("status","new")
    def import_legacy_seen(self, keys):
        """Import old SHA256(title+link) keys as sent markers during desk migration."""
        for key in keys:
            if isinstance(key, str) and key:
                self.data["items"].setdefault("legacy:" + key, {"status":"sent","title":"legacy migration","updated_at":int(time.time())})
        self.save()
    def begin(self,key,title=""):
        if self.status(key) not in {"new", "failed"}: raise DeskError("Item is not new or retryable")
        self.data["items"][key]={"status":"pending","title":title[:200],"updated_at":int(time.time())}
        self.save()
    def mark_sent(self,key,message_id):
        if self.status(key)!="pending": raise DeskError("Only pending items can be marked sent")
        self.data["items"][key].update(status="sent",message_id=int(message_id),updated_at=int(time.time()))
        self.save()
    def mark_failed(self,key,reason=""):
        if self.status(key)!="pending": raise DeskError("Only pending items can be marked failed")
        self.data["items"][key].update(status="failed",reason=safe_error(reason),updated_at=int(time.time()))
        self.save()
    def mark_unknown(self,key,reason=""):
        if self.status(key)!="pending": raise DeskError("Only pending items can be marked unknown")
        self.data["items"][key].update(status="unknown",reason=safe_error(reason),updated_at=int(time.time()))
        self.save()
    def save(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        items=self.data["items"]
        if len(items)>self.max_seen:
            unresolved={k:v for k,v in items.items() if v.get("status") in {"pending","unknown"}}
            resolved=[(k,v) for k,v in items.items() if v.get("status")=="sent"]
            resolved=resolved[-max(0,self.max_seen-len(unresolved)):]
            self.data["items"]={**dict(resolved),**unresolved}
        fd,tmp=tempfile.mkstemp(prefix=self.path.name+".",dir=str(self.path.parent))
        try:
            with os.fdopen(fd,"w",encoding="utf-8") as f:
                json.dump(self.data,f,ensure_ascii=False,indent=2); f.flush(); os.fsync(f.fileno())
            os.replace(tmp,self.path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)

def format_rss_notice(summary: str) -> str:
    if not clean_html(summary):
        return "جزئیات بیشتر در خوراک RSS منبع موجود نبود؛ این پست بر پایه تیتر منبع تنظیم شده است."
    return "خلاصه بر پایه متن در دسترس خوراک RSS منبع است؛ لزوماً متن کامل گزارش نیست."
