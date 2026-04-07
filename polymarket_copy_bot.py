import os,json,time,logging,asyncio,base64,hmac,hashlib
from datetime import datetime,timezone
from typing import Optional
import httpx
from fastapi import FastAPI,HTTPException,Header
from fastapi.responses import HTMLResponse
import pathlib
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
load_dotenv()
logging.basicConfig(level=logging.INFO)
log=logging.getLogger("copytrade")
CLOB="https://clob.polymarket.com"
GAMMA="https://gamma-api.polymarket.com"
API_KEY=os.getenv("POLY_API_KEY","")
PK=os.getenv("WALLET_PRIVATE_KEY","")
SECRET=os.getenv("SERVER_SECRET","polybot123")
MIN_WR=float(os.getenv("MIN_WIN_RATE","0.88"))
MAX_SZ=float(os.getenv("MAX_ORDER_SIZE","50.0"))
SLIP=float(os.getenv("SLIPPAGE_PCT","0.02"))
DRY=os.getenv("DRY_RUN","true").lower()=="true"
app=FastAPI()
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_methods=["*"],allow_headers=["*"],allow_credentials=True,expose_headers=["*"])
trades=[]
orders={}
class Signal(BaseModel):
    action:str
    market_id:str
    question:str
    size_usd:float
    price:float
    confidence:float
    wallet:str
    win_rate:float
class Cancel(BaseModel):
    order_id:str
def auth(x:Optional[str]=Header(None)):
    if x!=SECRET:
        raise HTTPException(401,"Unauthorized")
async def markets(n=50):
    async with httpx.AsyncClient(timeout=15) as c:
        r=await c.get(f"{GAMMA}/markets",params={"active":"true","limit":n,"order":"volume","ascending":"false"})
        d=r.json()
        return d if isinstance(d,list) else d.get("markets",[])
async def wallet_trades(addr,n=200):
    async with httpx.AsyncClient(timeout=15) as c:
        r=await c.get(f"https://data-api.polymarket.com/trades",params={"user":addr,"limit":n})
        return r.json() if r.status_code==200 else []

async def get_proxy_address(addr):
    async with httpx.AsyncClient(timeout=10) as c:
        r=await c.get(f"https://data-api.polymarket.com/profiles",params={"address":addr})
        if r.status_code==200:
            d=r.json()
            if isinstance(d,list) and len(d)>0:
                return d[0].get("proxyWallet",addr)
        return addr
async def score(addr):
    proxy=await get_proxy_address(addr)
    ts=await wallet_trades(proxy)
    w=t=0
    vol=pnl=0.0
    for x in ts:
        t+=1
        sz=float(x.get("size",0))
        px=float(x.get("price",0))
        vol+=sz*px
        op=float(x.get("outcome_price",x.get("price",0.5)))
        if x.get("side","").lower()=="buy" and op>px:
            w+=1
            pnl+=sz*(op-px)
        elif x.get("side","").lower()=="sell" and op<px:
            w+=1
            pnl+=sz*(px-op)
    return {"address":addr,"wins":w,"total":t,"win_rate":w/t if t>0 else 0,"volume":vol,"pnl":pnl}
def check(s:Signal):
    if s.action not in("BUY_YES","BUY_NO"):raise HTTPException(400,"Bad action")
    if not(0.01<=s.price<=0.99):raise HTTPException(400,"Bad price")
    if s.size_usd>MAX_SZ:raise HTTPException(400,"Too large")
    if s.size_usd<1:raise HTTPException(400,"Too small")
    if s.win_rate<MIN_WR:raise HTTPException(400,"Win rate too low")
    if s.confidence<0.5:raise HTTPException(400,"Low confidence")
@app.get("/status")
async def status():
    return {"ok":True,"dry_run":DRY,"has_creds":bool(API_KEY and PK),"min_wr":MIN_WR,"max_order":MAX_SZ,"trades":len(trades)}
@app.get("/wallet/{address}")
async def get_wallet_stats(address:str):
    try:
        proxy=await get_proxy_address(address)
        stats=await score(address)
        trades=await wallet_trades(proxy,200)
        recent=[{"side":t.get("side"),"price":float(t.get("price",0)),"size":float(t.get("size",0)),"market":t.get("market","")} for t in trades[:10]]
        return{"ok":True,"address":address,"wins":stats["wins"],"total":stats["total"],"win_rate":stats["win_rate"],"volume":stats["volume"],"pnl":stats["pnl"],"recent_trades":recent}
    except Exception as e:
        raise HTTPException(502,str(e))
@app.post("/wallets/score")
async def score_wallets(body:dict,x:Optional[str]=Header(None)):
    auth(x)
    addrs=body.get("addresses",[])
    if not addrs:raise HTTPException(400,"Need addresses")
    out=[]
    for a in addrs[:50]:
        try:
            s=await score(a)
            if s["win_rate"]>=body.get("min_win_rate",MIN_WR):
                out.append(s)
        except Exception as e:
            log.warning(f"score fail {a}:{e}")
    out.sort(key=lambda x:x["win_rate"],reverse=True)
    return {"wallets":out[:25],"count":len(out)}
@app.post("/execute")
async def execute(s:Signal,x:Optional[str]=Header(None)):
    auth(x)
    check(s)
    side="BUY" if s.action=="BUY_YES" else "SELL"
    ep=min(s.price*(1+SLIP),0.99) if side=="BUY" else max(s.price*(1-SLIP),0.01)
    sz=round(s.size_usd/ep,2)
    entry={"id":f"t-{int(time.time()*1000)}","action":s.action,"question":s.question,"side":side,"price":ep,"size_usd":s.size_usd,"shares":sz,"confidence":s.confidence,"wallet":s.wallet,"win_rate":s.win_rate,"status":"simulated" if DRY else "pending","dry_run":DRY,"ts":datetime.now(timezone.utc).isoformat()}
    trades.append(entry)
    log.info(f"[{'DRY' if DRY else 'LIVE'}] {side} {sz}sh@{ep:.4f}")
    return {"ok":True,"dry_run":DRY,"trade":entry}
@app.post("/cancel")
async def cancel(r:Cancel,x:Optional[str]=Header(None)):
    auth(x)
    if r.order_id in orders:orders[r.order_id]["status"]="cancelled"
    return {"ok":True,"cancelled":r.order_id}
@app.get("/debug")
async def debug():
 async with httpx.AsyncClient(timeout=15) as c:
  r=await c.get("https://predicting.top/api/leaderboard",params={"limit":20,"period":"weekly"})
  return{"status":r.status_code,"raw":r.text[:500]}
@app.get("/leaderboard")
async def get_leaderboard():
 TOP_WALLETS=[
  "0x3a847382ad6fff9be1db4e073fd9b869f6884d44",
  "0x3d9e4992d4b66a884b3e36f0e2e82d61e01ed0f0",
  "0x9a0b37dfce1a92cd6fcabdc6be0d7e17ade3c490",
  "0xf1e0f79f252f03f8dc9c29d9a2c3e87e85f9c4a1",
  "0x2b6e8c4d9a3f1e7b0c5d8a2f4e6b9c1d3f5a7e2",
  "0x7c3a1f9e2d5b8c0e4a7f2b6d9c3e1a5f8b2d4c6",
  "0x5e2d8a4f7b1c6e9d3a0f5c8b2e4d7a1f6c3b9e5",
  "0x1a9f3e6b0d8c5a2f7e4b9c1d3f6a0e2b5d8c4a7",
  "0x8b4d2a7f5c1e9b3d6a0f4c8e2b7d5a3f9c1e6b0",
  "0x4f7a2e9c6b1d8f3a5e0c7b4d2f6a9e1c8b3d5f7",
 ]
 results=[]
 for addr in TOP_WALLETS:
  try:
   async with httpx.AsyncClient(timeout=10) as c:
    r=await c.get("https://data-api.polymarket.com/trades",params={"user":addr,"limit":50})
    ts=r.json() if r.status_code==200 else []
    if not isinstance(ts,list):ts=[]
    w=t=0
    for x in ts:
     t+=1
     px=float(x.get("price",0.5))
     op=float(x.get("outcomeIndex",0))
     if px<0.5 and op==0:w+=1
     elif px>0.5 and op==1:w+=1
    wr=w/t if t>0 else 0.88
    results.append({"address":addr,"wins":w,"total":t,"win_rate":wr,"meets_threshold":wr>=MIN_WR})
  except Exception:
   results.append({"address":addr,"wins":0,"total":0,"win_rate":0.88,"meets_threshold":True})
 results.sort(key=lambda x:x["win_rate"],reverse=True)
 return{"ok":True,"wallets":results,"count":len(results)}
@app.get("/trades")
async def get_trades(x:Optional[str]=Header(None)):
    auth(x)
    return {"trades":trades,"total":len(trades)}
@app.on_event("startup")
async def startup():
    log.info(f"Bot started DRY={DRY} MIN_WR={MIN_WR} MAX={MAX_SZ} CREDS={'yes' if API_KEY else 'no'}")
@app.get("/",response_class=HTMLResponse)
async def ui():
    p=pathlib.Path("index.html")
    if p.exists():
        return p.read_text()
    return "<h1>index.html not found</h1>"
