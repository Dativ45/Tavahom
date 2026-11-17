import os,html,requests
from datetime import datetime
from zoneinfo import ZoneInfo
BOT=os.getenv("TELEGRAM_BOT_TOKEN"); CHAT=os.getenv("TELEGRAM_CHAT_ID")
if not BOT or not CHAT: raise SystemExit("Missing Telegram secrets.")
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
now=datetime.now(ZoneInfo("Asia/Tehran")); evening=now.hour>=15
def get(url,params=None):
 r=requests.get(url,params=params,headers={"User-Agent":"StatusRoom/1.0"},timeout=25); r.raise_for_status(); return r.json()
def toman(n): return f"{int(round(n)):,}".translate(str.maketrans("0123456789","۰۱۲۳۴۵۶۷۸۹"))
def main():
 sections=[]; usd=None
 try:
  p=get("https://raw.githubusercontent.com/hidevs/tabloo/main/data/popular.json"); rows={x.get("symbol"):x for x in p.get("data",[])}
  for sym in ["USD_IRR_FREE","EUR_IRR_FREE","GOLD_18K_IRR","COIN_EMAMI_IRR"]:
   x=rows.get(sym)
   if not x or x.get("price") is None or x.get("stale"): continue
   stamp=x.get("timestamp_tehran","")
   try:
    dt=datetime.fromisoformat(stamp)
    if (now-dt).total_seconds()>64800: continue
   except Exception: continue
   sections.append(f"• {html.escape(x.get('name_fa',sym))}: <b>{toman(x['price'])} تومان</b> ({x.get('change_pct',0):+.2f}٪) | زمان داده: {dt.strftime('%H:%M')} تهران")
   if sym=="USD_IRR_FREE": usd=x["price"]
  if sections: sections.insert(0,"💱 <b>ارز و طلا | بازار آزاد</b>")
 except Exception as e: print("Iran market feed unavailable:",str(e)[:180])
 if usd:
  try:
   coins=get("https://api.coingecko.com/api/v3/coins/markets",{"vs_currency":"usd","ids":"bitcoin,ethereum,solana","price_change_percentage":"24h","precision":"2"})
   lines=[]
   for c in coins:
    val=c.get("current_price")
    if val is None: continue
    pct=c.get("price_change_percentage_24h"); delta=(" | ۲۴ساعت: %+.2f٪"%pct) if isinstance(pct,(int,float)) else ""
    lines.append(f"• {c['name']}: <b>USD {val:,.2f}</b> | حدود <b>{toman(val*usd)} تومان</b>{delta}")
   if lines: sections.append("₿ <b>رمزارزهای منتخب</b>\n"+"\n".join(lines)+"\n<i>قیمت دلاری CoinGecko؛ تبدیل تقریبی با دلار آزاد همین تابلو.</i>")
  except Exception as e: print("CoinGecko unavailable:",str(e)[:180])
 if evening:
  try:
   data=get("https://api.nemuda.com/api/market/vehicles/car"); found=[]
   def walk(o):
    if isinstance(o,dict):
     price=o.get("price"); pts=o.get("dataPoints"); name=o.get("name") or o.get("name_fa") or o.get("title"); link=o.get("url") or o.get("pageUrl")
     if isinstance(price,(int,float)) and price>0 and isinstance(pts,int) and pts>=10 and name: found.append((str(name),price,pts,link))
     for v in o.values(): walk(v)
    elif isinstance(o,list):
     for v in o: walk(v)
   walk(data); wanted=["پژو ۲۰۷","پژو ۲۰۶","دنا","تارا","سورن","شاهین","اطلس","کوییک","ساینا","ری‌را"]; chosen=[]
   for key in wanted:
    hit=next((z for z in found if key in z[0] and z not in chosen),None)
    if hit: chosen.append(hit)
   if not chosen: chosen=found[:8]
   if chosen:
    lines=[]
    for name,price,pts,link in chosen[:8]:
     label=html.escape(name)
     if isinstance(link,str) and link.startswith("http"): label='<a href="'+html.escape(link,quote=True)+'">'+label+'</a>'
     lines.append(f"• {label}: <b>{toman(price)} تومان</b> | {pts} داده")
    sections.append("🚘 <b>قیمت خودروهای منتخب</b>\n"+"\n".join(lines)+"\n<i>قیمت میانهٔ خودرو کارکرده با بدنه سالم؛ تعداد داده کنار هر مدل آمده است.</i>\nمنبع: <a href=\"https://nemuda.com/data\">نمودا</a>")
  except Exception as e: print("Vehicle data unavailable:",str(e)[:180])
 if not sections:
  print("No sufficiently fresh price data; nothing posted.")
  return
 msg="🛰️ <b>اتاق وضعیت | تابلوی بازار</b>\n━━━━━━━━━━━━━━━━━━\n"+"\n\n".join(sections)+"\n\n⚠️ <i>اطلاع‌رسانی، نه توصیه خریدوفروش. داده قدیمی یا نامعتبر منتشر نمی‌شود.</i>\n— <b>تراز</b> | تحریریه اقتصاد"+channel_footer()
 r=requests.post("https://api.telegram.org/bot"+BOT+"/sendMessage",json={"chat_id":CHAT,"text":msg,"parse_mode":"HTML","disable_web_page_preview":False},timeout=35); p=r.json()
 if not r.ok or not p.get("ok"): raise SystemExit(p.get("description","Telegram send failed"))
 print("Published market bulletin",p["result"]["message_id"])
if __name__=="__main__": main()
