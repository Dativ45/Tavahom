import os,json,re,time,hashlib,html
from pathlib import Path
import feedparser,requests
BOT=os.getenv("TELEGRAM_BOT_TOKEN"); CHAT=os.getenv("TELEGRAM_CHAT_ID"); KEY=os.getenv("GEMINI_API_KEY"); MODEL=os.getenv("GEMINI_MODEL","gemini-3.1-flash-lite")
if not all([BOT,CHAT,KEY]): raise SystemExit("Missing Telegram/Gemini secrets.")
def channel_footer():
 configured=os.getenv("STATUS_ROOM_CHANNEL_URL","").strip()
 if configured.startswith("https://t.me/"): link=configured
 else:
  r=requests.get("https://api.telegram.org/bot"+BOT+"/getChat",params={"chat_id":CHAT},timeout=20); p=r.json()
  if not r.ok or not p.get("ok"): raise RuntimeError("Telegram getChat failed: "+str(p.get("description",p)))
  c=p.get("result",{}); link=("https://t.me/"+c["username"]) if c.get("username") else c.get("invite_link","")
  if not link and str(CHAT).startswith("@"): link="https://t.me/"+str(CHAT)[1:]
 if not link: print("WARNING: Set STATUS_ROOM_CHANNEL_URL to a public channel or invite link."); return ""
 return '\n\n📣 <b>دنبال‌کردن اتاق وضعیت:</b> <a href="'+html.escape(link,quote=True)+'">عضویت در کانال</a>'
FEEDS={"iran":[("ایسنا","https://www.isna.ir/rss"),("مهر","https://www.mehrnews.com/rss"),("باشگاه خبرنگاران","https://www.yjc.ir/fa/rss/allnews"),("تابناک","https://www.tabnak.ir/fa/rss/allnews")],
"sports":[("سرپوش فوتبال ایران","https://www.sarpoosh.com/rss/iran-pro-league.xml"),("سرپوش فوتبال جهان","https://www.sarpoosh.com/rss/football-world.xml"),("سرپوش ورزش","https://www.sarpoosh.com/rss/sports-news.xml")]}
STATE=Path("regional_state.json")
def clean(s): return html.unescape(re.sub(r"\s+"," ",re.sub(r"<[^>]+>","",s or ""))).strip()
def send(msg):
 r=requests.post("https://api.telegram.org/bot"+BOT+"/sendMessage",json={"chat_id":CHAT,"text":msg,"parse_mode":"HTML","disable_web_page_preview":False},timeout=35); p=r.json()
 if not r.ok or not p.get("ok"): raise RuntimeError(p.get("description","Telegram error"))
def translate(title,excerpt,desk):
 prompt="ترجمه و خلاصه دقیق فارسی ایران، بی‌طرف، بدون افزودن واقعیت. JSON با کلیدهای title_fa و summary_fa برگردان.\n"+json.dumps({"title":title,"excerpt":excerpt,"desk":desk},ensure_ascii=False)
 u="https://generativelanguage.googleapis.com/v1beta/models/"+MODEL+":generateContent"
 r=requests.post(u,headers={"x-goog-api-key":KEY,"Content-Type":"application/json"},json={"contents":[{"parts":[{"text":prompt}]}],"generationConfig":{"temperature":0.1,"responseMimeType":"application/json","maxOutputTokens":450}},timeout=45); r.raise_for_status()
 raw="".join(p.get("text","") for p in r.json().get("candidates",[{}])[0].get("content",{}).get("parts",[])); d=json.loads(raw)
 return clean(d.get("title_fa") or title),clean(d.get("summary_fa") or excerpt or "خوراک منبع فقط تیتر را ارائه کرده است.")
def collect(kind):
 out=[]
 for source,url in FEEDS[kind]:
  try:
   for e in feedparser.parse(url).entries[:25]:
    title=clean(e.get("title")); link=e.get("link","")
    if not title or not link.startswith("http"): continue
    p=e.get("published_parsed") or e.get("updated_parsed"); ts=int(time.mktime(p)) if p else 0
    if ts and time.time()-ts>36*3600: continue
    key=hashlib.sha256((title+link).encode()).hexdigest()
    out.append({"source":source,"title":title,"summary":clean(e.get("summary",e.get("description","")))[:1400],"link":link,"ts":ts,"key":key})
  except Exception as ex: print("Feed warning",source,str(ex)[:160])
 return out
def main():
 try: state=json.loads(STATE.read_text(encoding="utf-8"))
 except Exception: state={"seen":[]}
 seen=set(state.get("seen",[])); count=0
 for kind,desk,alias,heading in [("iran","ایران","آینه","اخبار ایران"),("sports","ورزش","ضربان","نبض ورزش")]:
  items=[x for x in collect(kind) if x["key"] not in seen]; items.sort(key=lambda x:x["ts"],reverse=True)
  if not items: print("No new",desk); continue
  x=items[0]; title,summary=translate(x["title"],x["summary"],desk)
  msg=("🛰️ <b>اتاق وضعیت | "+heading+"</b>\n━━━━━━━━━━━━━━━━━━\n⚠️ <b>گزارش یک منبع | نیازمند بررسی مستقل</b>\n"
   "این خبر از یک خوراک خبری دریافت شده و تأیید مستقل آن در این سامانه انجام نشده است.\n\n🇮🇷 <b>"+html.escape(title)+"</b>\n"+html.escape(summary[:1100])+
   "\n\n🗞 منبع: "+html.escape(x["source"])+"\n🔗 <a href=\""+html.escape(x["link"],quote=True)+"\">متن کامل منبع</a>\n\n— <b>"+alias+"</b> | تحریریه "+desk+
   "\n<i>نام‌ها پنهان می‌مانند؛ حقیقت نه.</i>"+channel_footer())
  send(msg); seen.add(x["key"]); count+=1; print("Published",desk,x["title"])
 STATE.write_text(json.dumps({"seen":list(seen)[-4000:]},ensure_ascii=False,indent=2),encoding="utf-8")
 print("Published",count,"regional posts.")
if __name__=="__main__": main()
