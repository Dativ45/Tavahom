import os, re, json, html, hashlib
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests, feedparser
from bs4 import BeautifulSoup

BOT=os.getenv("TELEGRAM_BOT_TOKEN"); CHAT=os.getenv("TELEGRAM_CHAT_ID")
KEY=os.getenv("GEMINI_API_KEY"); MODEL=os.getenv("GEMINI_MODEL","gemini-3.1-flash-lite")
if not BOT or not CHAT or not KEY: raise SystemExit("Missing required secrets.")
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
NOW=datetime.now(ZoneInfo("Asia/Tehran"))
STATE=Path("livelihood_state.json")
HEADERS={"User-Agent":"Mozilla/5.0 (compatible; StatusRoom/1.0)"}
LABOR_TERMS=("حقوق کارگران","حقوق کارمندان","افزایش حقوق","افزایش دستمزد","شورای عالی کار","حداقل مزد","حداقل حقوق","پایه حقوق","حق مسکن کارگران","بن کارگری","حق اولاد کارگران","عیدی کارگران","حقوق بازنشستگان","مستمری بازنشستگان","متناسب سازی حقوق بازنشستگان","سبد معیشت کارگران","تعیین دستمزد کارگران")
SUBSIDY_TERMS=("یارانه نقدی","واریز یارانه","افزایش یارانه","حذف یارانه","قطع یارانه","کالابرگ الکترونیکی","کالابرگ","دهک بندی یارانه","دهک‌بندی یارانه")
PRICE_TERMS=("قیمت نان","قیمت روغن خوراکی","قیمت مرغ","قیمت تخم مرغ","قیمت تخم‌مرغ","قیمت برنج","قیمت لبنیات","قیمت گوشت","قیمت شیر","قیمت کالاهای اساسی","تورم مواد غذایی","سبد کالاهای اساسی")
BLOCK_TERMS=("پنتاگون","نیروهای نظامی","نیروهای آمریکایی","ارتش آمریکا","ناتو","رزمایش","موشک","پهپاد","حمله نظامی","جنگ","نبرد","تحریم نظامی","اروپا","کاخ سفید","وزارت دفاع","سربازان","استقرار نیرو","خروج نیرو","کاهش نیروهای")
FEEDS=[
 ("ایسنا","https://www.isna.ir/rss"),
 ("مهر","https://www.mehrnews.com/rss"),
 ("تسنیم","https://www.tasnimnews.com/fa/rss/feed/0/7/0/"),
 ("ایلنا","https://www.ilna.ir/rss"),
 ("تابناک","https://www.tabnak.ir/fa/rss/allnews"),
 ("باشگاه خبرنگاران","https://www.yjc.ir/fa/rss/allnews"),
]
def clean(s): return html.unescape(re.sub(r"\s+"," ",BeautifulSoup(s or "","html.parser").get_text(" ",strip=True))).strip()
def send(text):
 r=requests.post("https://api.telegram.org/bot"+BOT+"/sendMessage",json={"chat_id":CHAT,"text":text,"parse_mode":"HTML","disable_web_page_preview":False},timeout=35)
 p=r.json()
 if not r.ok or not p.get("ok"): raise RuntimeError(p.get("description","Telegram send failed"))
 return p["result"]["message_id"]
def get_state():
 try: return json.loads(STATE.read_text(encoding="utf-8"))
 except Exception: return {"seen_news":[],"seen_deals":[]}
def translate(title,summary):
 prompt="این خبر اقتصادی/معیشتی را دقیق و بی‌طرف به فارسی روان ایران خلاصه کن. هیچ رقم، تاریخ، مصوبه یا قطعیتی را اضافه نکن. اگر خبر صرفاً نقل قول یا پیشنهاد است، همان را روشن بنویس. حداکثر ۳ جمله. JSON با کلید summary برگردان.\
"+json.dumps({"title":title,"summary":summary},ensure_ascii=False)
 u=f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
 r=requests.post(u,headers={"x-goog-api-key":KEY,"Content-Type":"application/json"},json={"contents":[{"parts":[{"text":prompt}]}],"generationConfig":{"temperature":0.1,"responseMimeType":"application/json","maxOutputTokens":300}},timeout=45)
 r.raise_for_status()
 raw="".join(x.get("text","") for x in r.json().get("candidates",[{}])[0].get("content",{}).get("parts",[]))
 try: return clean(json.loads(raw).get("summary",""))
 except Exception: return clean(summary)[:600]
def is_livelihood_title(title):
 t=title.replace("ي","ی").replace("ك","ک").lower()
 if any(term in t for term in BLOCK_TERMS): return False
 return any(term in t for term in LABOR_TERMS+SUBSIDY_TERMS+PRICE_TERMS)
def livelihood_news(state):
 items=[]
 for source,url in FEEDS:
  try:
   for e in feedparser.parse(url).entries[:30]:
    title=clean(e.get("title")); summary=clean(e.get("summary",e.get("description",""))); link=e.get("link","")
    if not title or not link.startswith("http") or not is_livelihood_title(title): continue
    published=e.get("published_parsed") or e.get("updated_parsed")
    if published:
     from time import mktime
     age=datetime.now(timezone.utc).timestamp()-mktime(published)
     if age>72*3600: continue
    key=hashlib.sha256((title+link).encode()).hexdigest()
    if key not in state["seen_news"]: items.append((source,title,summary,link,key))
  except Exception as ex: print("RSS warning",source,str(ex)[:120])
 if not items: print("No new livelihood news."); return
 source,title,summary,link,key=items[0]
 fa=translate(title,summary)
 msg=("🧾 <b>معیشت و حقوق کارگران</b>\n━━━━━━━━━━━━━━━━━━\n"
      "🟠 <b>"+html.escape(title)+"</b>\n"+html.escape(fa or "خلاصه قابل اتکا در خوراک منبع موجود نبود.")+
      "\n\n🗞 منبع: "+html.escape(source)+"\n🔗 <a href=\""+html.escape(link,quote=True)+"\">متن کامل خبر</a>"+
      "\n\n⚠️ <i>خبر و اظهارنظر از هم تفکیک می‌شود؛ تغییر حقوق یا یارانه فقط با سند اجرایی قطعی اعلام خواهد شد.</i>\n— <b>تراز</b> | میز معیشت"+channel_footer())
 send(msg); state["seen_news"].append(key); print("Published livelihood news:",title)
def telegram_deals(state):
 # Public previews of the brands' own Telegram channels; only fresh, dated posts are eligible.
 channels=[("اُکالا","okala_com"),("دیجی‌کالا","digikalaofficialaccount")]
 posts=[]
 for brand,channel in channels:
  try:
   url="https://t.me/s/"+channel
   r=requests.get(url,headers=HEADERS,timeout=25); r.raise_for_status()
   soup=BeautifulSoup(r.text,"html.parser")
   for node in soup.select("div.tgme_widget_message_wrap"):
    body=node.select_one(".tgme_widget_message_text")
    date=node.select_one("time[datetime]")
    anchor=node.select_one("a.tgme_widget_message_date")
    if not body or not date or not anchor: continue
    dt=datetime.fromisoformat(date.get("datetime").replace("Z","+00:00")).astimezone(ZoneInfo("Asia/Tehran"))
    if (NOW-dt).total_seconds()>48*3600: continue
    text=clean(str(body))
    if not re.search(r"تخفیف|٪|درصد|کد تخفیف|ارسال رایگان|حراج|شگفت.?انگیز|پیشنهاد ویژه",text): continue
    href=anchor.get("href","")
    key=hashlib.sha256((channel+href+text).encode()).hexdigest()
    if key in state["seen_deals"]: continue
    posts.append((dt,brand,text,href,key))
  except Exception as ex: print("Deal source unavailable",brand,str(ex)[:120])
 if not posts: print("No new official discount posts."); return
 posts.sort(reverse=True,key=lambda x:x[0])
 # One offer per run; source text is quoted/summarized without inventing prices or validity.
 dt,brand,text,href,key=posts[0]
 msg=("🛍️ <b>شکار تخفیف | "+html.escape(brand)+"</b>\n━━━━━━━━━━━━━━━━━━\n"
      +html.escape(text[:900])+"\n\n🕒 زمان انتشار منبع: "+dt.strftime("%Y-%m-%d %H:%M")+" تهران"
      +"\n🔗 <a href=\""+html.escape(href,quote=True)+"\">مشاهده پست اصلی فروشگاه</a>"
      +"\n\n⚠️ <i>قیمت، موجودی، شهرهای مشمول و مهلت را پیش از پرداخت در خود فروشگاه بررسی کنید؛ تخفیف منقضی بازنشر نمی‌شود.</i>\n— <b>تراز</b> | رصد خرید روزانه"+channel_footer())
 send(msg); state["seen_deals"].append(key); print("Published official deal:",brand)
def main():
 state=get_state()
 livelihood_news(state)
 telegram_deals(state)
 state["seen_news"]=state["seen_news"][-2500:]
 state["seen_deals"]=state["seen_deals"][-1500:]
 STATE.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding="utf-8")
if __name__=="__main__": main()
