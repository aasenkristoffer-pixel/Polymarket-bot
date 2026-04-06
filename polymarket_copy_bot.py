import os
import json
import time
import logging
import asyncio
import hmac
import hashlib
import base64
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format=”%(asctime)s [%(levelname)s] %(message)s”)
log = logging.getLogger(“copytrade”)

CLOB_HOST = “https://clob.polymarket.com”
GAMMA_HOST = “https://gamma-api.polymarket.com”

PRIVATE_KEY = os.getenv(“WALLET_PRIVATE_KEY”, “”)
API_KEY = os.getenv(“POLY_API_KEY”, “”)
API_SECRET = os.getenv(“POLY_API_SECRET”, “”)
API_PASSPHRASE = os.getenv(“POLY_API_PASSPHRASE”, “”)
SERVER_SECRET = os.getenv(“SERVER_SECRET”, “changeme”)

MIN_WIN_RATE = float(os.getenv(“MIN_WIN_RATE”, “0.88”))
MAX_ORDER_SIZE = float(os.getenv(“MAX_ORDER_SIZE”, “50.0”))
DEFAULT_SLIPPAGE = float(os.getenv(“SLIPPAGE_PCT”, “0.02”))
DRY_RUN = os.getenv(“DRY_RUN”, “true”).lower() == “true”

app = FastAPI(title=“Polymarket CopyTrade Server”, version=“1.0.0”)

app.add_middleware(
CORSMiddleware,
allow_origins=[”*”],
allow_credentials=True,
allow_methods=[”*”],
allow_headers=[”*”],
)

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
raise HTTPException(status_code=401, detail=“Unauthorized”)

class PolyClient:
def **init**(self):
self.host = CLOB_HOST
self.api_key = API_KEY
self.api_secret = API_SECRET
self.passphrase = API_PASSPHRASE

```
def _sign(self, method: str, path: str, body: str = "") -> dict:
    ts = str(int(time.time() * 1000))
    msg = ts + method.upper() + path + body
    sig = hmac.new(
        self.api_secret.encode(), msg.encode(), hashlib.sha256
    ).digest()
    sig_b64 = base64.b64encode(sig).decode()
    return {
        "POLY-API-KEY": self.api_key,
        "POLY-SIGNATURE": sig_b64,
        "POLY-TIMESTAMP": ts,
        "POLY-PASSPHRASE": self.passphrase,
        "Content-Type": "application/json",
    }

async def get_balance(self) -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        headers = self._sign("GET", "/balance")
        r = await c.get(f"{self.host}/balance", headers=headers)
        r.raise_for_status()
        return r.json()

async def get_open_orders(self) -> list:
    async with httpx.AsyncClient(timeout=15) as c:
        headers = self._sign("GET", "/orders")
        r = await c.get(f"{self.host}/orders", headers=headers)
        r.raise_for_status()
        return r.json().get("data", [])

async def place_order(self, token_id: str, side: str, price: float, size: float) -> dict:
    body = json.dumps({
        "token_id": token_id,
        "side": side,
        "price": round(price, 4),
        "size": round(size, 2),
        "type": "GTC",
    })
    headers = self._sign("POST", "/order", body)
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{self.host}/order", headers=headers, content=body)
        r.raise_for_status()
        return r.json()

async def cancel_order(self, order_id: str) -> dict:
    body = json.dumps({"order_id": order_id})
    headers = self._sign("DELETE", "/order", body)
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.delete(f"{self.host}/order", headers=headers, content=body)
        r.raise_for_status()
        return r.json()

async def get_trades(self, address: str, limit: int = 200) -> list:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{self.host}/trades", params={"maker_address": address, "limit": limit})
        return r.json().get("data", []) if r.status_code == 200 else []
```

client = PolyClient()

async def fetch_active_markets(limit: int = 50) -> list:
async with httpx.AsyncClient(timeout=15) as c:
r = await c.get(
f”{GAMMA_HOST}/markets”,
params={“active”: “true”, “limit”: limit, “order”: “volume”, “ascending”: “false”}
)
d = r.json()
return d if isinstance(d, list) else d.get(“markets”, [])

async def score_wallet(address: str) -> dict:
trades = await client.get_trades(address)
wins = total = 0
volume = pnl = 0.0
for t in trades:
total += 1
sz = float(t.get(“size”, 0))
px = float(t.get(“price”, 0))
volume += sz * px
outcome_price = float(t.get(“outcome_price”, t.get(“price”, 0.5)))
if t.get(“side”, “”).lower() == “buy” and outcome_price > px:
wins += 1
pnl += sz * (outcome_price - px)
elif t.get(“side”, “”).lower() == “sell” and outcome_price < px:
wins += 1
pnl += sz * (px - outcome_price)
return {
“address”: address,
“wins”: wins,
“total”: total,
“win_rate”: wins / total if total > 0 else 0,
“volume”: volume,
“pnl”: pnl,
}

def validate_signal(sig: TradeSignal) -> None:
if sig.action not in (“BUY_YES”, “BUY_NO”):
raise HTTPException(400, f”Invalid action: {sig.action}”)
if not (0.01 <= sig.price <= 0.99):
raise HTTPException(400, f”Price out of range”)
if sig.size_usd > MAX_ORDER_SIZE:
raise HTTPException(400, f”Order too large”)
if sig.size_usd < 1.0:
raise HTTPException(400, “Minimum order is $1.00”)
if sig.wallet_win_rate < MIN_WIN_RATE:
raise HTTPException(400, “Wallet win rate too low”)
if sig.confidence < 0.5:
raise HTTPException(400, “Confidence too low”)

@app.get(”/status”)
async def status():
return {
“ok”: True,
“dry_run”: DRY_RUN,
“has_creds”: bool(API_KEY and API_SECRET),
“min_win_rate”: MIN_WIN_RATE,
“max_order”: MAX_ORDER_SIZE,
“total_trades”: len(trade_log),
“timestamp”: datetime.now(timezone.utc).isoformat(),
}

@app.get(”/balance”)
async def get_balance(x_secret: Optional[str] = Header(None)):
verify_secret(x_secret)
if DRY_RUN:
return {“usdc_balance”: 1000.00, “dry_run”: True}
try:
return await client.get_balance()
except Exception as e:
raise HTTPException(502, f”CLOB error: {e}”)

@app.get(”/orders”)
async def get_orders(x_secret: Optional[str] = Header(None)):
verify_secret(x_secret)
if DRY_RUN:
return {“orders”: list(order_cache.values()), “dry_run”: True}
try:
return {“orders”: await client.get_open_orders()}
except Exception as e:
raise HTTPException(502, f”CLOB error: {e}”)

@app.get(”/markets”)
async def get_markets(limit: int = 30):
try:
markets = await fetch_active_markets(limit)
return {“markets”: markets, “count”: len(markets)}
except Exception as e:
raise HTTPException(502, f”Gamma API error: {e}”)

@app.post(”/wallets/score”)
async def score_wallets(body: dict, x_secret: Optional[str] = Header(None)):
verify_secret(x_secret)
addresses = body.get(“addresses”, [])
if not addresses:
raise HTTPException(400, “Provide a list of addresses”)
results = []
for addr in addresses[:50]:
try:
stats = await score_wallet(addr)
if stats[“win_rate”] >= body.get(“min_win_rate”, MIN_WIN_RATE):
results.append(stats)
except Exception as e:
log.warning(f”Failed to score {addr}: {e}”)
results.sort(key=lambda x: x[“win_rate”], reverse=True)
return {“wallets”: results[:25], “count”: len(results)}

@app.post(”/execute”)
async def execute_trade(signal: TradeSignal, x_secret: Optional[str] = Header(None)):
verify_secret(x_secret)
validate_signal(signal)

```
if signal.action == "BUY_YES":
    side = "BUY"
    exec_price = min(signal.price * (1 + DEFAULT_SLIPPAGE), 0.99)
else:
    side = "SELL"
    exec_price = max(signal.price * (1 - DEFAULT_SLIPPAGE), 0.01)

size_shares = round(signal.size_usd / exec_price, 2)

log_entry = {
    "id": f"trade-{int(time.time()*1000)}",
    "action": signal.action,
    "market_id": signal.market_id,
    "question": signal.question,
    "side": side,
    "price": exec_price,
    "size_usd": signal.size_usd,
    "size_shares": size_shares,
    "confidence": signal.confidence,
    "wallet_source": signal.wallet_source,
    "wallet_wr": signal.wallet_win_rate,
    "status": "pending",
    "dry_run": DRY_RUN,
    "date": datetime.now(timezone.utc).date().isoformat(),
    "timestamp": datetime.now(timezone.utc).isoformat(),
}

if DRY_RUN:
    log_entry["status"] = "simulated"
    log_entry["order_id"] = f"dry-{log_entry['id']}"
    trade_log.append(log_entry)
    log.info(f"[DRY RUN] {side} {size_shares} shares @ {exec_price:.4f}")
    return {"ok": True, "dry_run": True, "trade": log_entry}

if not (API_KEY and API_SECRET and API_PASSPHRASE and PRIVATE_KEY):
    raise HTTPException(503, "API credentials not configured")

try:
    result = await client.place_order(
        token_id=signal.market_id,
        side=side,
        price=exec_price,
        size=size_shares,
    )
    order_id = result.get("order_id") or result.get("id", "unknown")
    log_entry["status"] = "filled" if result.get("status") == "MATCHED" else "open"
    log_entry["order_id"] = order_id
    order_cache[order_id] = log_entry
    trade_log.append(log_entry)
    return {"ok": True, "dry_run": False, "order_id": order_id, "trade": log_entry}
except httpx.HTTPStatusError as e:
    raise HTTPException(502, f"Order rejected: {e.response.text}")
except Exception as e:
    raise HTTPException(500, str(e))
```

@app.post(”/cancel”)
async def cancel_trade(req: CancelRequest, x_secret: Optional[str] = Header(None)):
verify_secret(x_secret)
if DRY_RUN:
if req.order_id in order_cache:
order_cache[req.order_id][“status”] = “cancelled”
return {“ok”: True, “dry_run”: True, “cancelled”: req.order_id}
try:
result = await client.cancel_order(req.order_id)
if req.order_id in order_cache:
order_cache[req.order_id][“status”] = “cancelled”
return {“ok”: True, “result”: result}
except Exception as e:
raise HTTPException(502, f”Cancel failed: {e}”)

@app.get(”/trades”)
async def get_trade_history(x_secret: Optional[str] = Header(None)):
verify_secret(x_secret)
return {“trades”: trade_log, “total”: len(trade_log)}

async def wallet_watcher(addresses: list, poll_seconds: int = 15):
seen_trades = set()
log.info(f”Watcher started for {len(addresses)} wallets”)
markets = await fetch_active_markets(20)
market_map = {m.get(“condition_id”, “”): m for m in markets}
while True:
for addr in addresses:
try:
trades = await client.get_trades(addr, limit=10)
for t in trades:
tid = t.get(“id”) or t.get(“transaction_hash”, “”)
if tid in seen_trades:
continue
seen_trades.add(tid)
stats = await score_wallet(addr)
if stats[“win_rate”] < MIN_WIN_RATE:
continue
market = market_map.get(t.get(“market”, “”), {})
side = t.get(“side”, “”).upper()
price = float(t.get(“price”, 0.5))
size = min(float(t.get(“size”, 0)) * price, MAX_ORDER_SIZE)
if size < 1.0 or not market:
continue
signal = TradeSignal(
action=“BUY_YES” if side == “BUY” else “BUY_NO”,
market_id=t.get(“market”, “”),
question=market.get(“question”, “Unknown market”),
size_usd=size,
price=price,
confidence=stats[“win_rate”],
wallet_source=addr,
wallet_win_rate=stats[“win_rate”],
)
log.info(f”[WATCHER] Auto-copying {addr[:10]} on {signal.question[:40]}”)
await execute_trade(signal, x_secret=SERVER_SECRET)
except Exception as e:
log.warning(f”Watcher error for {addr}: {e}”)
await asyncio.sleep(poll_seconds)

@app.on_event(“startup”)
async def startup():
log.info(f”CopyTrade Server started - DRY_RUN={DRY_RUN}”)
log.info(f”Min win rate: {MIN_WIN_RATE}”)
log.info(f”Max order: ${MAX_ORDER_SIZE}”)
log.info(f”Credentials: {‘loaded’ if API_KEY else ‘missing’}”)

if **name** == “**main**”:
import uvicorn
uvicorn.run(“polymarket_copy_bot:app”, host=“0.0.0.0”, port=8000, reload=True)