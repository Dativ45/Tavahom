import argparse
import hashlib
import html
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHANNEL = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
STATE_FILE = Path(os.getenv("TREND_STATE_FILE", "trend_state.json"))
TEHRAN = ZoneInfo("Asia/Tehran")

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
OPENVERSE_URL = "https://api.openverse.org/v1/images/"
GDELT_QUERIES = [
    ("موضوعات جهان", '("artificial intelligence" OR "machine learning" OR semiconductor OR cybersecurity OR "climate change" OR "space exploration" OR NASA OR biodiversity OR football OR basketball OR cricket OR tennis OR Olympics OR cinema OR film OR music OR festival OR literature OR inflation OR trade OR energy OR "central bank" OR employment)'),
]
ALLOWED_LICENSES = {"cc0", "by", "by-sa"}
ALLOWED_FILETYPES = {"jpg", "jpeg", "png"}
STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "after", "before",
    "over", "under", "into", "about", "says", "said", "live", "latest",
    "update", "updates", "news", "report", "reports", "today", "november",
    "nov", "2026", "world", "global", "what", "when", "where", "will",
    "has", "have", "had", "are", "was", "were", "its", "their", "his",
    "her", "you", "your", "our", "they", "them", "how", "why", "who",
}
UNSAFE_TERMS = {
    "porn", "pornography", "explicit", "nude", "nudity", "casino", "gambling",
    "cocaine", "heroin", "methamphetamine", "overdose", "suicide", "self-harm",
    "terrorism", "terrorist", "massacre", "beheading", "rape", "murder",
    "shooting", "bombing", "missile", "hostage", "graphic", "gore",
}
MAX_DAILY_IMAGES = 3
MAX_ARTICLES_PER_QUERY = 120
MAX_CANDIDATES = 12
MAX_IMAGE_BYTES = 8 * 1024 * 1024
GDELT_MIN_INTERVAL = 30.0
GDELT_RETRY_DELAYS = (30.0, 60.0)
REQUEST_TIMEOUT = 18
USER_AGENT = "StatusRoomTrendRadar/2.0 (+https://github.com/Dativ45/Tavahom)"
_LAST_GDELT_REQUEST = 0.0

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("status-room-trends")


def clean(value):
    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    return html.unescape(re.sub(r"\s+", " ", value)).strip()


def load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("posted", [])
            data.setdefault("images_posted", [])
            # Migrate legacy date:fingerprint entries so an older trend publisher
            # cannot bypass today's three-post cap or repeat a topic.
            existing = {
                entry.get("topic_key")
                for entry in data["images_posted"]
                if isinstance(entry, dict)
            }
            for legacy in data["posted"]:
                if not isinstance(legacy, str):
                    continue
                match = re.match(r"^(\d{4}-\d{2}-\d{2}):([0-9a-f]{32,64})$", legacy)
                if match and legacy not in existing:
                    data["images_posted"].append({
                        "date": match.group(1),
                        "topic_key": legacy,
                        "legacy": True,
                    })
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"posted": [], "images_posted": []}


def save_state(state):
    state["posted"] = state.get("posted", [])[-1200:]
    state["images_posted"] = state.get("images_posted", [])[-1200:]
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_gdelt_time(value):
    value = str(value or "").strip()
    if not value:
        return None
    for pattern in ("%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def infer_category(title):
    text = clean(title).lower()
    categories = [
        ("فناوری", ("artificial intelligence", "machine learning", "cybersecurity", "semiconductor", "chip", "robot", "quantum")),
        ("علم و محیط زیست", ("climate", "space", "nasa", "biodiversity", "wildlife", "conservation")),
        ("ورزش", ("football", "soccer", "basketball", "cricket", "tennis", "olympic", "nba", "fifa")),
        ("فرهنگ", ("cinema", "film", "music", "festival", "literature", "museum")),
        ("اقتصاد", ("inflation", "trade", "energy", "central bank", "employment", "economy", "market")),
    ]
    for category, terms in categories:
        if any(term in text for term in terms):
            return category
    return "بین‌الملل"


def wait_for_gdelt_slot():
    global _LAST_GDELT_REQUEST
    elapsed = time.monotonic() - _LAST_GDELT_REQUEST
    if _LAST_GDELT_REQUEST and elapsed < GDELT_MIN_INTERVAL:
        time.sleep(GDELT_MIN_INTERVAL - elapsed)
    _LAST_GDELT_REQUEST = time.monotonic()


def fetch_gdelt_query(label, query):
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": MAX_ARTICLES_PER_QUERY,
        "timespan": "24h",
        "sort": "DateDesc",
    }
    for attempt in range(3):
        wait_for_gdelt_slot()
        try:
            response = requests.get(
                GDELT_URL,
                params=params,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 429:
                if attempt == 2:
                    log.warning("GDELT rate limit persisted for %s; skipping this run.", label)
                    return []
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = max(float(retry_after), GDELT_RETRY_DELAYS[attempt])
                except (TypeError, ValueError):
                    delay = GDELT_RETRY_DELAYS[attempt]
                delay = min(delay, 90.0)
                log.warning("GDELT rate limit for %s; retrying after %.0f seconds.", label, delay)
                time.sleep(delay)
                continue
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            if "json" not in content_type:
                log.warning("GDELT returned non-JSON content for %s; skipping.", label)
                return []
            payload = response.json()
            if not isinstance(payload, dict):
                return []
            articles = payload.get("articles", [])
            if not isinstance(articles, list):
                return []
            now = datetime.now(timezone.utc)
            cleaned = []
            for article in articles:
                if not isinstance(article, dict):
                    continue
                title = clean(article.get("title"))
                link = str(article.get("url") or "").strip()
                domain = clean(article.get("domain")).lower().removeprefix("www.")
                seen_at = parse_gdelt_time(article.get("seendate"))
                if not title or not link.startswith("https://") or not domain or not seen_at:
                    continue
                if seen_at > now + timedelta(minutes=10) or now - seen_at > timedelta(hours=30):
                    continue
                cleaned.append({
                    "title": title[:240],
                    "url": link,
                    "domain": domain,
                    "seen_at": seen_at.isoformat(),
                    "query_category": infer_category(title) if label == "موضوعات جهان" else label,
                })
            return cleaned
        except (requests.RequestException, ValueError, TypeError) as exc:
            log.warning("GDELT query failed for %s: %s", label, exc)
            return []
    return []


def title_tokens(title):
    tokens = set(re.findall(r"[a-z][a-z0-9]{2,}", clean(title).lower()))
    return tokens - STOPWORDS


def article_similarity(left, right):
    a = title_tokens(left)
    b = title_tokens(right)
    if not a or not b:
        return 0.0
    jaccard = len(a & b) / len(a | b)
    sequence = SequenceMatcher(None, clean(left).lower(), clean(right).lower()).ratio()
    return max(jaccard, sequence * 0.65)


def cluster_gdelt_articles(articles):
    groups = []
    for article in articles:
        best_group = None
        best_score = 0.0
        for group in groups:
            score = max(article_similarity(article["title"], item["title"]) for item in group["articles"])
            if score > best_score:
                best_score = score
                best_group = group
        if best_group is not None and best_score >= 0.45:
            best_group["articles"].append(article)
        else:
            groups.append({"articles": [article]})

    candidates = []
    for group in groups:
        unique_domains = sorted({item["domain"] for item in group["articles"]})
        if len(unique_domains) < 2:
            continue
        newest = max(group["articles"], key=lambda item: item["seen_at"])
        fingerprint = hashlib.sha256(
            re.sub(r"\W+", " ", newest["title"].lower()).encode("utf-8")
        ).hexdigest()
        candidates.append({
            "topic": newest["title"],
            "category": newest["query_category"],
            "domains": unique_domains,
            "articles": sorted(group["articles"], key=lambda item: item["seen_at"], reverse=True)[:5],
            "fingerprint": fingerprint,
            "score": len(unique_domains) * 10 + min(len(group["articles"]), 8) * 2,
        })
    return sorted(candidates, key=lambda item: (item["score"], len(item["domains"])), reverse=True)


def candidate_is_safe(candidate):
    text = " ".join(
        [candidate.get("topic", "")] +
        [article.get("title", "") for article in candidate.get("articles", [])]
    ).lower()
    return not any(
        re.search(r"\b" + re.escape(term) + r"\b", text)
        for term in UNSAFE_TERMS
    )


def discover_gdelt_trends():
    all_articles = []
    for label, query in GDELT_QUERIES:
        all_articles.extend(fetch_gdelt_query(label, query))
    # De-duplicate syndicated copies from the same URL before clustering.
    unique = {}
    for article in all_articles:
        unique.setdefault(article["url"], article)
    clustered = cluster_gdelt_articles(list(unique.values()))
    candidates = [item for item in clustered if candidate_is_safe(item)]
    log.info(
        "GDELT discovery: %d recent articles, %d multi-domain clusters, %d family-safe candidates.",
        len(unique), len(clustered), len(candidates),
    )
    return candidates[:MAX_CANDIDATES]


def summarize_candidates(candidates):
    if not candidates:
        return []
    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_API_KEY is required to prepare Persian editorial captions.")
    compact = []
    for index, candidate in enumerate(candidates):
        compact.append({
            "candidate_id": index,
            "source_headline": candidate["topic"],
            "category": candidate["category"],
            "independent_domains": candidate["domains"],
            "other_headlines": [item["title"] for item in candidate["articles"][1:4]],
        })
    prompt = (
        "You are the cautious Persian visual-trend editor for Status Room. "
        "Use only the supplied candidate headlines. A cluster means multiple domains covered similar wording; "
        "it is not proof that the underlying claim is true. Do not invent facts, numbers, causes, quotes, "
        "locations, identities, virality or public opinion. Exclude match listings, isolated names, generic "
        "phrases, rumors, political/military allegations, graphic events, gambling, drugs, sexual material, "
        "hate, harassment and dangerous challenges. Prefer a clear public-interest subject suitable for a "
        "family-friendly image card. Return at most 3 items and only candidate_id values present in the input. "
        "Return JSON only: {items:[{candidate_id,headline_fa,explanation_fa,caution_fa}]}. "
        "headline_fa must be a concise Persian rendering of the supplied source headline, not a new claim. "
        "explanation_fa must say this is a topic receiving coverage across multiple media outlets, and avoid "
        "asserting unverified details. caution_fa should be empty unless the source headlines themselves show "
        "uncertainty. If no topic is safe and meaningful, return {items:[]}.\n\n"
        + json.dumps(compact, ensure_ascii=False)
    )
    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + GEMINI_MODEL + ":generateContent"
    )
    response = requests.post(
        endpoint,
        headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
                "maxOutputTokens": 1500,
            },
        },
        timeout=45,
    )
    if not response.ok:
        raise RuntimeError("Gemini editorial request failed with HTTP " + str(response.status_code))
    payload = response.json()
    parts = payload.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    raw = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
    if not raw.strip():
        raise RuntimeError("Gemini returned an empty editorial selection.")
    parsed = json.loads(raw)
    selected = []
    for item in parsed.get("items", [])[:MAX_DAILY_IMAGES]:
        if not isinstance(item, dict):
            continue
        try:
            candidate_id = int(item.get("candidate_id"))
        except (TypeError, ValueError):
            continue
        if candidate_id < 0 or candidate_id >= len(candidates):
            continue
        headline = clean(item.get("headline_fa"))[:150]
        explanation = clean(item.get("explanation_fa"))[:500]
        caution = clean(item.get("caution_fa"))[:220]
        if not headline or not explanation:
            continue
        candidate = dict(candidates[candidate_id])
        candidate.update({
            "headline_fa": headline,
            "explanation_fa": explanation,
            "caution_fa": caution,
        })
        selected.append(candidate)
    return selected


def image_is_relevant(image, topic):
    title_and_tags = clean(image.get("title", "")) + " " + " ".join(
        clean(tag.get("name", "") if isinstance(tag, dict) else tag)
        for tag in (image.get("tags") or [])
    )
    topic_words = title_tokens(topic)
    image_words = title_tokens(title_and_tags)
    if not topic_words or not image_words:
        return False
    needed = 1 if len(topic_words) <= 3 else 2
    return len(topic_words & image_words) >= needed


def usable_openverse_image(image, topic):
    if not isinstance(image, dict):
        return False
    license_code = str(image.get("license") or "").lower().strip()
    license_url = str(image.get("license_url") or "").strip()
    image_url = str(image.get("url") or "").strip()
    landing_url = str(image.get("foreign_landing_url") or "").strip()
    filetype = str(image.get("filetype") or "").lower().strip()
    if license_code not in ALLOWED_LICENSES:
        return False
    if not str(image.get("id") or "").strip():
        return False
    license_path = license_url.lower().rstrip("/")
    if license_code == "cc0":
        license_matches = bool(re.fullmatch(
            r"https://creativecommons\.org/publicdomain/zero/[0-9.]+", license_path
        ))
    elif license_code == "by":
        license_matches = bool(re.fullmatch(
            r"https://creativecommons\.org/licenses/by/[0-9.]+", license_path
        ))
    else:
        license_matches = bool(re.fullmatch(
            r"https://creativecommons\.org/licenses/by-sa/[0-9.]+", license_path
        ))
    if not license_matches:
        return False
    if not license_url.startswith("https://") or not landing_url.startswith("https://"):
        return False
    if not image_url.startswith("https://") or filetype not in ALLOWED_FILETYPES:
        return False
    if image.get("mature") is True:
        return False
    try:
        width = int(image.get("width") or 0)
        height = int(image.get("height") or 0)
    except (TypeError, ValueError):
        return False
    if max(width, height) < 600 or min(width, height) < 300:
        return False
    return image_is_relevant(image, topic)


def search_openverse_image(topic):
    params = {
        "q": topic[:160],
        "license": "cc0,by,by-sa",
        "license_type": "commercial",
        "exclude_sensitive": "true",
        "page_size": 20,
    }
    try:
        response = requests.get(
            OPENVERSE_URL,
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results", []) if isinstance(payload, dict) else []
        for image in results:
            if usable_openverse_image(image, topic):
                return image
    except (requests.RequestException, ValueError, TypeError) as exc:
        log.warning("Openverse search failed for topic %s: %s", topic[:70], exc)
    return None


def download_openverse_image(image):
    url = image["url"]
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
        if content_type not in {"image/jpeg", "image/png"}:
            log.warning("Rejected image with unsupported MIME type: %s", content_type)
            return None
        content = response.content
        if not content or len(content) > MAX_IMAGE_BYTES:
            log.warning("Rejected image with invalid size.")
            return None
        if content_type == "image/jpeg" and not content.startswith(b"\xff\xd8\xff"):
            return None
        if content_type == "image/png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
            return None
        extension = "jpg" if content_type == "image/jpeg" else "png"
        return (extension, content, content_type)
    except requests.RequestException as exc:
        log.warning("Could not download Openverse image: %s", exc)
        return None


def license_label(image):
    code = str(image.get("license") or "").lower()
    version = clean(image.get("license_version") or "")
    labels = {"cc0": "CC0", "by": "CC BY", "by-sa": "CC BY-SA"}
    return labels.get(code, code.upper()) + ((" " + version) if version else "")


def build_caption(item, image):
    article_links = []
    for article in item.get("articles", [])[:2]:
        article_links.append(
            '<a href="' + html.escape(article["url"], quote=True) + '">' +
            html.escape(article["domain"][:70]) + "</a>"
        )
    creator = clean(image.get("creator") or "")[:60]
    creator_url = str(image.get("creator_url") or "").strip()
    if creator and creator_url.startswith("https://"):
        creator_text = '<a href="' + html.escape(creator_url, quote=True) + '">' + html.escape(creator) + "</a>"
    elif creator:
        creator_text = html.escape(creator)
    else:
        creator_text = "در اطلاعات Openverse درج نشده"
    license_url = html.escape(image["license_url"], quote=True)
    landing_url = html.escape(image["foreign_landing_url"], quote=True)
    headline = html.escape(clean(item["headline_fa"])[:110])
    explanation = html.escape(clean(item["explanation_fa"])[:260])
    image_title = html.escape(clean(image.get("title") or "مشاهده تصویر")[:90])
    caution_text = clean(item.get("caution_fa") or "")[:100]
    caution = ("\n\n⚠️ " + html.escape(caution_text)) if caution_text else ""
    caption = (
        "🌐 <b>ترند تصویری | اتاق وضعیت</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>{headline}</b>\n\n"
        f"{explanation}\n"
        f"📊 بازتاب: {len(item.get('domains', []))} دامنه خبری در GDELT طی ۲۴ ساعت اخیر"
        f"{caution}\n\n"
        "🖼 <b>تصویر آرشیوی/تزئینی</b> — این تصویر عکس واقعی رویداد روز نیست.\n"
        f"عنوان تصویر: <a href=\"{landing_url}\">{image_title}</a>\n"
        f"خالق: {creator_text}\n"
        f"مجوز: <a href=\"{license_url}\">{html.escape(license_label(image))}</a>\n"
        f"منابع پوشش خبری: {' | '.join(article_links[:2]) if article_links else 'پیوند مکمل در دسترس نیست'}\n\n"
        "🧾 <i>ترند بودن، تأیید صحت ادعا نیست؛ تصویر صرفاً برای توضیح موضوع انتخاب شده است.</i>\n"
        "— <b>اطلس</b> | رادار جهانی اتاق وضعیت"
    )
    if len(caption) > 1000:
        explanation = html.escape(clean(item["explanation_fa"])[:150])
        caption = (
            "🌐 <b>ترند تصویری | اتاق وضعیت</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>{headline[:100]}</b>\n\n"
            f"{explanation}\n"
            f"📊 بازتاب: {len(item.get('domains', []))} دامنه خبری در GDELT طی ۲۴ ساعت اخیر\n\n"
            "🖼 <b>تصویر آرشیوی/تزئینی</b> — عکس واقعی رویداد روز نیست.\n"
            f"عنوان تصویر: <a href=\"{landing_url}\">{image_title[:60]}</a>\n"
            f"خالق: {creator_text}\n"
            f"مجوز: <a href=\"{license_url}\">{html.escape(license_label(image))}</a>\n"
            f"منبع خبری: {article_links[0] if article_links else 'پیوند مکمل در دسترس نیست'}\n"
            "— <b>اطلس</b> | رادار جهانی اتاق وضعیت"
        )
    if len(caption) > 1024:
        raise ValueError("Telegram caption exceeds the 1024-character limit.")
    return caption

def already_posted(state, today, item, image):
    entries = state.get("images_posted", [])
    topic_key = today + ":" + item["fingerprint"]
    image_id = str(image.get("id") or "")
    if topic_key in {entry.get("topic_key") for entry in entries if isinstance(entry, dict)}:
        return True
    if image_id and image_id in {entry.get("image_id") for entry in entries if isinstance(entry, dict)}:
        return True
    return False


def daily_image_count(state, today):
    return sum(
        1 for entry in state.get("images_posted", [])
        if isinstance(entry, dict) and entry.get("date") == today
    )


def send_photo(image_payload, caption):
    if not BOT_TOKEN or not CHANNEL:
        raise RuntimeError("Telegram credentials are missing; refusing to publish.")
    extension, content, mime_type = image_payload
    response = requests.post(
        "https://api.telegram.org/bot" + BOT_TOKEN + "/sendPhoto",
        data={"chat_id": CHANNEL, "caption": caption, "parse_mode": "HTML"},
        files={"photo": ("status-room-trend." + extension, io.BytesIO(content), mime_type)},
        timeout=45,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"description": "Telegram returned a non-JSON response."}
    if not response.ok or not payload.get("ok"):
        raise RuntimeError("Telegram photo publishing failed: " + clean(payload.get("description", "unknown error"))[:250])
    return payload.get("result", {}).get("message_id")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Status Room GDELT/Openverse image trend radar")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="collect and validate content without Telegram publishing")
    mode.add_argument("--publish", action="store_true", help="allow Telegram publishing")
    args = parser.parse_args(argv)
    dry_run = args.dry_run or (not args.publish and os.getenv("TREND_DRY_RUN", "1").lower() not in {"0", "false", "no"})

    state = load_state()
    today = datetime.now(TEHRAN).date().isoformat()
    posted_today = daily_image_count(state, today)
    remaining = max(0, MAX_DAILY_IMAGES - posted_today)
    if remaining == 0:
        log.info("Daily image cap reached (%d); no more images will be published today.", MAX_DAILY_IMAGES)
        return

    candidates = discover_gdelt_trends()
    if not candidates:
        log.info("No multi-domain, recent GDELT trend cluster passed the discovery filter.")
        return

    selected = summarize_candidates(candidates)
    if not selected:
        log.info("Editorial filter found no safe, useful image topics.")
        return

    published = 0
    seen_images = {
        entry.get("image_id")
        for entry in state.get("images_posted", [])
        if isinstance(entry, dict) and entry.get("image_id")
    }
    for item in selected:
        if published >= remaining:
            break
        if already_posted(state, today, item, {"id": ""}):
            log.info("Skipped already-posted topic: %s", item["topic"])
            continue
        image = search_openverse_image(item["topic"])
        if not image:
            log.info("No clearly licensed and relevant Openverse image; skipped: %s", item["topic"])
            continue
        if image.get("id") in seen_images:
            log.info("Skipped previously used Openverse image: %s", image.get("id"))
            continue
        payload = download_openverse_image(image)
        if not payload:
            log.info("Image download/format validation failed; skipped: %s", item["topic"])
            continue
        try:
            caption = build_caption(item, image)
        except ValueError as exc:
            log.warning("Caption validation failed; image skipped: %s", exc)
            continue
        if dry_run:
            log.info(
                "DRY-RUN only: topic=%s | image_id=%s | license=%s | creator=%s | source=%s",
                item["topic"], image.get("id"), license_label(image),
                clean(image.get("creator") or "not supplied"),
                image.get("foreign_landing_url"),
            )
            published += 1
            continue

        message_id = send_photo(payload, caption)
        entry = {
            "date": today,
            "topic_key": today + ":" + item["fingerprint"],
            "topic": item["topic"][:180],
            "image_id": str(image.get("id") or ""),
            "message_id": message_id,
            "license": license_label(image),
            "source_url": image.get("foreign_landing_url"),
        }
        state.setdefault("images_posted", []).append(entry)
        seen_images.add(entry["image_id"])
        published += 1
        log.info("Published licensed archive image for topic %s; message_id=%s", item["topic"], message_id)

    if dry_run:
        log.info("Dry-run complete: %d candidate image(s) passed; no Telegram message was sent and state was not changed.", published)
    elif published:
        state["last_image_run_tehran"] = datetime.now(TEHRAN).isoformat()
        save_state(state)
        log.info("Published %d image post(s); daily cap is %d.", published, MAX_DAILY_IMAGES)
    else:
        log.info("No image passed all relevance, license, freshness and safety checks; nothing published.")


if __name__ == "__main__":
    main()
