import os
import json
import re
import time
import hashlib
import html
import calendar
from pathlib import Path
from urllib.parse import quote

import feedparser
import requests

BOT=os.getenv("TELEGRAM_BOT_TOKEN")
CHAT=os.getenv("TELEGRAM_CHAT_ID")
KEY=os.getenv("GEMINI_API_KEY")
MODEL=os.getenv("GEMINI_MODEL","gemini-3.1-flash-lite")
if not all([BOT,CHAT,KEY]):
    raise SystemExit("Missing Telegram/Gemini secrets.")

STATE=Path("regional_state.json")
FEEDS={
 "sports":[
  ("سرپوش فوتبال ایران","https://www.sarpoosh.com/rss/iran-pro-league.xml"),
  ("سرپوش فوتبال جهان","https://www.sarpoosh.com/rss/football-world.xml"),
  ("سرپوش ورزش","https://www.sarpoosh.com/rss/sports-news.xml"),
  ("BBC Sport","https://feeds.bbci.co.uk/sport/rss.xml"),
  ("BBC Sport Football","https://feeds.bbci.co.uk/sport/football/rss.xml"),
  ("Sky Sports","https://www.skysports.com/rss/12040"),
  ("The Guardian Sport","https://www.theguardian.com/uk/sport/rss"),
 ]}
SPORT_TERMS=re.compile(
 r"فوتبال|تیم ملی|لیگ برتر|پرسپولیس|استقلال|تراکتور|سپاهان|کشتی|والیبال|"
 r"بسکتبال|هندبال|وزنه.?برداری|تنیس|فوتسال|ورزش زنان|Iran|Iranian|football|"
 r"wrestling|volleyball|basketball",re.I
)
IRAN_SPORT_RELEVANCE=re.compile(
 r"ایران|ایرانی|پرسپولیس|استقلال|تراکتور|سپاهان|فولاد|ملوان|ذوب.?آهن|"
 r"فجر سپاسی|چادرملو|لیگ برتر ایران|جام حذفی ایران|تیم ملی ایران|"
 r"iran|iranian|persian gulf pro league|iran pro league|iran national team|"
 r"iranian wrestler|iranian volleyball|iranian basketball",re.I
)

def channel_footer():
    configured=os.getenv("STATUS_ROOM_CHANNEL_URL","").strip()
    if configured.startswith("https://t.me/"):
        link=configured
    else:
        response=requests.get("https://api.telegram.org/bot"+BOT+"/getChat",params={"chat_id":CHAT},timeout=20)
        payload=response.json()
        if not response.ok or not payload.get("ok"):
            raise RuntimeError("Telegram getChat failed: "+str(payload.get("description",payload)))
        chat=payload.get("result",{})
        link=("https://t.me/"+chat["username"]) if chat.get("username") else chat.get("invite_link","")
        if not link and str(CHAT).startswith("@"):
            link="https://t.me/"+str(CHAT)[1:]
    if not link:
        print("WARNING: Set STATUS_ROOM_CHANNEL_URL to a public channel or invite link.")
        return ""
    return '\n\n📣 <b>دنبال‌کردن اتاق وضعیت:</b> <a href="'+html.escape(link,quote=True)+'">عضویت در کانال</a>'

def clean(value):
    return html.unescape(re.sub(r"\s+"," ",re.sub(r"<[^>]+>"," ",value or ""))).strip()

def send(message):
    response=requests.post(
        "https://api.telegram.org/bot"+BOT+"/sendMessage",
        json={"chat_id":CHAT,"text":message,"parse_mode":"HTML","disable_web_page_preview":False},
        timeout=35,
    )
    payload=response.json()
    if not response.ok or not payload.get("ok"):
        raise RuntimeError(payload.get("description","Telegram error"))
    return payload.get("result",{}).get("message_id")

def translate(title,excerpt,desk):
    prompt=(
      "You are the sports editor of a Persian news channel. Return valid JSON with title_fa and summary_fa. "
      "Use natural Iranian Persian sports terminology; preserve team/player names, scores, competition names "
      "and transfer uncertainty. Never turn speculation into a confirmed transfer, invent a score, or state "
      "a prediction as a result. Summarize only the supplied excerpt in at most two sentences. "
      "If the feed gives only a headline, say that details are limited.\n"+
      json.dumps({"title":title,"excerpt":excerpt,"desk":desk},ensure_ascii=False)
    )
    endpoint="https://generativelanguage.googleapis.com/v1beta/models/"+MODEL+":generateContent"
    response=requests.post(
      endpoint,
      headers={"x-goog-api-key":KEY,"Content-Type":"application/json"},
      json={"contents":[{"parts":[{"text":prompt}]}],
            "generationConfig":{"temperature":0.1,"responseMimeType":"application/json","maxOutputTokens":500}},
      timeout=45,
    )
    response.raise_for_status()
    raw="".join(part.get("text","") for part in response.json().get("candidates",[{}])[0].get("content",{}).get("parts",[]))
    data=json.loads(raw)
    return clean(data.get("title_fa") or title),clean(data.get("summary_fa") or "خوراک منبع جزئیات بیشتری ارائه نکرده است.")

def collect(kind):
    items=[]
    for source,url in FEEDS[kind]:
        try:
            response=requests.get(url,headers={"User-Agent":"StatusRoomSportsDesk/1.0"},timeout=18)
            response.raise_for_status()
            feed=feedparser.parse(response.content)
            if getattr(feed,"bozo",False):
                print("Feed warning",source,str(getattr(feed,"bozo_exception","invalid feed"))[:120])
            for entry in feed.entries[:30]:
                title=clean(entry.get("title"))
                link=entry.get("link","")
                excerpt=clean(entry.get("summary",entry.get("description","")))[:1400]
                if not title or not link.startswith(("http://","https://")):
                    continue
                if kind=="iran" and not IRAN_TERMS.search(title+" "+excerpt):
                    continue
                if kind=="sports" and (not SPORT_TERMS.search(title+" "+excerpt) or not IRAN_SPORT_RELEVANCE.search(title+" "+excerpt)):
                    continue
                parsed=entry.get("published_parsed") or entry.get("updated_parsed")
                timestamp=calendar.timegm(parsed) if parsed else 0
                if timestamp and (time.time()-timestamp>36*3600 or timestamp>time.time()+3600):
                    continue
                key=hashlib.sha256((title+link).encode("utf-8")).hexdigest()
                items.append({"source":source,"title":title,"summary":excerpt,"link":link,"ts":timestamp,"key":key})
        except Exception as exc:
            print("Feed warning",source,str(exc)[:160])

    if kind=="sports":
        queries=[
            "Iran Iranian football national team OR Persian Gulf Pro League OR Iran football",
            "Iran wrestling OR Iranian wrestlers OR Iran volleyball OR Iranian volleyball",
            "Iran women's sports OR Iranian athletes OR Iran basketball",
        ]
        for query in queries:
            try:
                url="https://news.google.com/rss/search?q="+quote(query+" when:36h")+"&hl=en-US&gl=US&ceid=US:en"
                response=requests.get(url,headers={"User-Agent":"StatusRoomSportsDesk/1.0"},timeout=18)
                response.raise_for_status()
                feed=feedparser.parse(response.content)
                for entry in feed.entries[:25]:
                    title=clean(entry.get("title"))
                    link=entry.get("link","")
                    excerpt=clean(entry.get("summary",entry.get("description","")))[:1400]
                    if not title or not link.startswith(("http://","https://")) or not SPORT_TERMS.search(title+" "+excerpt) or not IRAN_SPORT_RELEVANCE.search(title+" "+excerpt):
                        continue
                    parsed=entry.get("published_parsed") or entry.get("updated_parsed")
                    timestamp=calendar.timegm(parsed) if parsed else 0
                    if timestamp and (time.time()-timestamp>36*3600 or timestamp>time.time()+3600):
                        continue
                    source_meta=entry.get("source",{}) or {}
                    source=clean(source_meta.get("title","Google News")) if hasattr(source_meta,"get") else "Google News"
                    key=hashlib.sha256((title+link).encode("utf-8")).hexdigest()
                    items.append({"source":source,"title":title,"summary":excerpt,"link":link,"ts":timestamp,"key":key})
            except Exception as exc:
                print("Google News sports search warning",str(exc)[:160])

    unique={item["key"]:item for item in items}
    return sorted(unique.values(),key=lambda item:item["ts"],reverse=True)

def main():
    try:
        state=json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError,json.JSONDecodeError):
        state={"seen":[]}
    seen=set(state.get("seen",[]))
    count=0
    for kind,desk,alias,heading in [
        ("sports","ورزش","ضربان","نبض ورزش"),
    ]:
        items=[item for item in collect(kind) if item["key"] not in seen]
        if not items:
            print("No new",desk)
            continue
        item=items[0]
        title,summary=translate(item["title"],item["summary"],desk)
        original=""
        if re.search(r"[A-Za-z]",item["title"]):
            original="\n\n🌐 <b>Original (English)</b>\n"+html.escape(item["title"][:220])
            if item["summary"]:
                original+="\n"+html.escape(item["summary"][:450])
        message=(
            "🛰️ <b>اتاق وضعیت | "+heading+"</b>\n━━━━━━━━━━━━━━━━━━\n"
            "⚠️ <b>گزارش یک منبع؛ تأیید مستقل نشده</b>\n\n"
            "🇮🇷 <b>"+html.escape(title)+"</b>\n"+html.escape(summary[:850])
            +original
            +"\n\n🗞 منبع: "+html.escape(item["source"])
            +"\n🔗 <a href=\""+html.escape(item["link"],quote=True)+"\">مشاهده منبع</a>"
            +"\n\n— <b>"+alias+"</b> | دبیر "+desk+channel_footer()
        )
        message_id=send(message)
        seen.add(item["key"])
        count+=1
        print("Published",desk,item["source"],"message_id=",message_id)
    STATE.write_text(json.dumps({"seen":list(seen)[-5000:]},ensure_ascii=False,indent=2),encoding="utf-8")
    print("Published",count,"regional posts.")

if __name__=="__main__":
    main()
