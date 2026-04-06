import os, json, time, logging, asyncio, hmac, hashlib, base64
from datetime import datetime, timezone
from typing import Optional
import httpx
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("copytrade")
logging.basicConfig(level=logging.INFO)

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
API_KEY = os.getenv("POLY_API_KEY", "")
API_SECRET = os.getenv("POLY_API_SECRET", "")
API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE", "")
PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY", "")
SERVER_SECRET = os.getenv("SERVER_SECRET", "changeme")
MIN_WIN_RATE = float(os.getenv("MIN_WIN_RATE", "0.88"))
MAX_ORDER_SIZE = float(os.getenv("MAX_ORDER_SIZE", "50.0"))
SLIPPAGE = float(os.getenv("SLIPPAGE_PCT", "0.02"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
trade_log = []
order_cache = {}
class TradeSignal(BaseModel):
    action: str
    market_id: str
    question: str
    size_usd: float
    price: float
    confidence: float
    wallet_source: str
    wallet_win_rate: float

class CancelRequest(BaseModel):
    order_id: str

def verify_secret(x_secret: Optional[str] = Header(None)):
    if x_secret != SERVER_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

async def fetch_active_markets(limit: int = 50) -> list:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{GAMMA_HOST}/markets", params={"active": "true", "limit": limit, "order": "volume", "ascending": "false"})
        d = r.json()
        return d if isinstance(d, list) else d.get("markets", [])

async def get_trades(address: str, limit: int = 200) -> list:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{CLOB_HOST}/trades", params={"maker_address": address, "limit": limit})
        return r.json().get("data", []) if r.status_code == 200 else []

async def score_wallet(address: str) -> dict:
    trades = await get_trades(address)
    wins​​​​​​​​​​​​​​​​
@app.get("/status")
async def status():
    return {"ok": True, "dry_run": DRY_RUN, "has_creds": bool(API_KEY and API_SECRET), "min_win_rate": MIN_WIN_RATE, "max_order": MAX_ORDER_SIZE, "total_trades": len(trade_log)}

@app.get("/markets")
async def get_markets(limit: int = 30):
    try:
        markets = await fetch_active_markets(limit)
        return {"markets": markets, "count": len(markets)}
    except Exception as e:
        raise HTTPException(502, f"API error: {e}")

@app.post("/wallets/score")
async def score_wallets(body: dict, x_secret: Optional[str] = Header(None)):
    verify_secret(x_secret)
    addresses = body.get("addresses", [])
    if not addresses:
        raise HTTPException(400, "Provide a list of addresses")
    results = []
    for addr in addresses[:50]:
        try:
            stats = await score_wallet(addr)
            if stats["win_rate"] >= body.get("min_win_rate", MIN_WIN_RATE):
                results.append(stats)
        except Exception as e:
            log.warning(f"Failed to score {addr}: {e}")
    results.sort(key=lambda x: x["win_rate"], reverse=True)
    return {"wallets": results[:25], "count": len(results)}

@app.post("/execute")
async def execute_trade(signal: TradeSignal, x_secret: Optional[str] = Header(None)):
    verify_secret(x_secret)
    validate_signal(signal)
    if signal.action == "BUY_YES":
        side = "BUY"
        exec_price = min(signal.price * (1 + SLIPPAGE), 0.99)
    else:
        side = "SELL"
        exec_price = max(signal.price * (1 - SLIPPAGE), 0.01)
    size_shares = round(signal.size_usd / exec_price, 2)
    log_entry = {"id": f"trade-{int(time.time()*1000)}", "action": signal.action, "question": signal.question, "side": side, "price": exec_price, "size_usd": signal.size_usd, "size_shares": size_shares, "confidence": signal.confidence, "wallet_source": signal.wallet_source, "wallet_wr": signal.wallet_win_rate, "status": "simulated" if DRY_RUN else "pending", "dry_run": DRY_RUN, "timestamp": datetime.now(timezone.utc).isoformat()}
    trade_log.append(log_entry)
    log.info(f"[{'DRY RUN' if DRY_RUN else 'LIVE'}] {side} {size_shares} shares @ {exec_price:.4f}")
    return {"ok": True, "dry_run": DRY_RUN, "trade": log_entry}

@app.post("/cancel")
async def cancel_trade(req: CancelRequest, x_secret: Optional[str] = Header(None)):
    verify_secret(x_secret)
    if req.order_id in order_cache:
        order_cache[req.order_id]["status"] = "cancelled"
    return {"ok": True, "cancelled": req.order_id}

@app.get("/trades")
async def get_trade_history(x_secret: Optional[str] = Header(None)):
    verify_secret(x_secret)
    return {"trades": trade_log, "total": len(trade_log)}

@app.on_event("startup")
async def startup():
    log.info(f"CopyTrade Server started - DRY_RUN={DRY_RUN}")
    log.info(f"Min win rate: {MIN_WIN_RATE} | Max order: ${MAX_ORDER_SIZE}")
    log.info(f"Credentials: {'loaded' if API_KEY else 'missing'}")
