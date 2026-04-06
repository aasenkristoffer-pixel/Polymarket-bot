“””
╔══════════════════════════════════════════════════════════╗
║     POLYMARKET COPYTRADING BOT — EXECUTION SERVER        ║
║     Connects your UI signals to real Polymarket orders   ║
╚══════════════════════════════════════════════════════════╝

Endpoints:
POST /execute       — place a trade from UI signal
POST /cancel        — cancel an open order
GET  /orders        — list open orders
GET  /balance       — USDC balance on Polygon
GET  /status        — server health + wallet info
GET  /wallets/top   — fetch top wallets from Polymarket
GET  /markets       — fetch active markets
“””

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
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format=”%(asctime)s [%(levelname)s] %(message)s”)
log = logging.getLogger(“copytrade”)

# ── CONFIG ────────────────────────────────────────────────

CLOB_HOST        = “https://clob.polymarket.com”
GAMMA_HOST       = “https://gamma-api.polymarket.com”
CHAIN_ID         = 137  # Polygon mainnet

PRIVATE_KEY      = os.getenv(“WALLET_PRIVATE_KEY”, “”)
API_KEY          = os.getenv(“POLY_API_KEY”, “”)
API_SECRET       = os.getenv(“POLY_API_SECRET”, “”)
API_PASSPHRASE   = os.getenv(“POLY_API_PASSPHRASE”, “”)
SERVER_SECRET    = os.getenv(“SERVER_SECRET”, “changeme-use-a-strong-secret”)

MIN_WIN_RATE     = float(os.getenv(“MIN_WIN_RATE”, “0.88”))
MAX_ORDER_SIZE   = float(os.getenv(“MAX_ORDER_SIZE”, “50.0”))   # USD cap per trade
DEFAULT_SLIPPAGE = float(os.getenv(“SLIPPAGE_PCT”, “0.02”))     # 2%
DRY_RUN          = os.getenv(“DRY_RUN”, “true”).lower() == “true”

# ── FASTAPI APP ───────────────────────────────────────────

app = FastAPI(title=“Polymarket CopyTrade Server”, version=“1.0.0”)

app.add_middleware(
CORSMiddleware,
allow_origins=[”*”],  # lock this down to your UI domain in production
allow_credentials=True,
allow_methods=[”*”],
allow_headers=[”*”],
)

# ── IN-MEMORY TRADE LOG ───────────────────────────────────

trade_log: list[dict] = []
order_cache: dict[str, dict] = {}

# ── PYDANTIC MODELS ───────────────────────────────────────

class TradeSignal(BaseModel):
action: str            # “BUY_YES” | “BUY_NO”
market_id: str         # condition_id or token_id
question: str
size_usd: float        # dollar size to trade
price: float           # limit price (0.0 – 1.0)
confidence: float
wallet_source: str     # which wallet we’re copying
wallet_win_rate: float

class CancelRequest(BaseModel):
order_id: str

# ── AUTH HELPER ───────────────────────────────────────────

def verify_secret(x_secret: Optional[str] = Header(None)):
“”“Simple shared-secret auth so only your UI can hit this server.”””
if x_secret != SERVER_SECRET:
raise HTTPException(status_code=401, detail=“Unauthorized”)

# ── POLYMARKET CLOB CLIENT ────────────────────────────────

class PolyClient:
def **init**(self):
self.host       = CLOB_HOST
self.api_key    = API_KEY
self.api_secret = API_SECRET
self.passphrase = API_PASSPHRASE
self.private_key = PRIVATE_KEY

```
def _sign(self, method: str, path: str, body: str = "") -> dict:
    """Build HMAC-SHA256 auth headers for CLOB API."""
    ts = str(int(time.time() * 1000))
    msg = ts + method.upper() + path + body
    sig = hmac.new(
        self.api_secret.encode(), msg.encode(), hashlib.sha256
    ).digest()
    sig_b64 = base64.b64encode(sig).decode()
    return {
        "POLY-API-KEY":        self.api_key,
        "POLY-SIGNATURE":      sig_b64,
        "POLY-TIMESTAMP":      ts,
        "POLY-PASSPHRASE":     self.passphrase,
        "Content-Type":        "application/json",
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
    """
    Place a limit order on Polymarket CLOB.

    In production this requires EIP-712 signing via the py-clob-client SDK.
    See: https://github.com/Polymarket/py-clob-client

    Full SDK usage:
        from py_clob_client.client import ClobClient, ApiCreds
        from py_clob_client.clob_types import OrderArgs, BUY, SELL

        client = ClobClient(
            host=CLOB_HOST,
            key=PRIVATE_KEY,
            chain_id=137,
            creds=ApiCreds(API_KEY, API_SECRET, API_PASSPHRASE)
        )
        order = client.create_and_post_order(OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=BUY if side == "BUY" else SELL,
        ))
    """
    body = json.dumps({
        "token_id": token_id,
        "side":     side,
        "price":    round(price, 4),
        "size":     round(size, 2),
        "type":     "GTC",  # Good Till Cancelled
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

async def get_orderbook(self, token_id: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{self.host}/book", params={"token_id": token_id})
        return r.json() if r.status_code == 200 else {}

async def get_trades(self, address: str, limit: int = 200) -> list:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{self.host}/trades", params={"maker_address": address, "limit": limit})
        return r.json().get("data", []) if r.status_code == 200 else []
```

client = PolyClient()

# ── GAMMA API HELPERS ─────────────────────────────────────

async def fetch_active_markets(limit: int = 50) -> list:
async with httpx.AsyncClient(timeout=15) as c:
r = await c.get(
f”{GAMMA_HOST}/markets”,
params={“active”: “true”, “limit”: limit, “order”: “volume”, “ascending”: “false”}
)
d = r.json()
return d if isinstance(d, list) else d.get(“markets”, [])

async def fetch_wallet_trades(address: str) -> list:
return await client.get_trades(address)

# ── WIN RATE ENGINE ───────────────────────────────────────

async def score_wallet(address: str) -> dict:
“”“Compute win rate for a wallet from its resolved trades.”””
trades = await fetch_wallet_trades(address)
wins = total = 0
volume = pnl = 0.0
for t in trades:
total += 1
sz = float(t.get(“size”, 0))
px = float(t.get(“price”, 0))
volume += sz * px
# A “win” heuristic: filled BUY at price < 0.5 that later resolved YES,
# or SELL > 0.5 that resolved NO. Real impl needs outcome lookup.
outcome_price = float(t.get(“outcome_price”, t.get(“price”, 0.5)))
if t.get(“side”,””).lower() == “buy” and outcome_price > px:
wins += 1
pnl += sz * (outcome_price - px)
elif t.get(“side”,””).lower() == “sell” and outcome_price < px:
wins += 1
pnl += sz * (px - outcome_price)

```
return {
    "address":  address,
    "wins":     wins,
    "total":    total,
    "win_rate": wins / total if total > 0 else 0,
    "volume":   volume,
    "pnl":      pnl,
}
```

# ── RISK CHECKS ───────────────────────────────────────────

def validate_signal(sig: TradeSignal) -> None:
if sig.action not in (“BUY_YES”, “BUY_NO”):
raise HTTPException(400, f”Invalid action: {sig.action}”)
if not (0.01 <= sig.price <= 0.99):
raise HTTPException(400, f”Price {sig.price} out of range (0.01–0.99)”)
if sig.size_usd > MAX_ORDER_SIZE:
raise HTTPException(400, f”Order size ${sig.size_usd} exceeds cap ${MAX_ORDER_SIZE}”)
if sig.size_usd < 1.0:
raise HTTPException(400, “Minimum order size is $1.00”)
if sig.wallet_win_rate < MIN_WIN_RATE:
raise HTTPException(400, f”Wallet win rate {sig.wallet_win_rate:.1%} below minimum {MIN_WIN_RATE:.1%}”)
if sig.confidence < 0.5:
raise HTTPException(400, “AI confidence too low to execute”)

# ── ROUTES ───────────────────────────────────────────────

@app.get(”/status”)
async def status():
has_creds = bool(API_KEY and API_SECRET and API_PASSPHRASE and PRIVATE_KEY)
return {
“ok”:           True,
“dry_run”:      DRY_RUN,
“has_creds”:    has_creds,
“min_win_rate”: MIN_WIN_RATE,
“max_order”:    MAX_ORDER_SIZE,
“trades_today”: len([t for t in trade_log if t[“date”] == datetime.now(timezone.utc).date().isoformat()]),
“total_trades”: len(trade_log),
“timestamp”:    datetime.now(timezone.utc).isoformat(),
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
orders = await client.get_open_orders()
return {“orders”: orders}
except Exception as e:
raise HTTPException(502, f”CLOB error: {e}”)

@app.get(”/markets”)
async def get_markets(limit: int = 30):
try:
markets = await fetch_active_markets(limit)
return {“markets”: markets, “count”: len(markets)}
except Exception as e:
raise HTTPException(502, f”Gamma API error: {e}”)

@app.get(”/wallets/top”)
async def get_top_wallets(
min_win_rate: float = 0.88,
limit: int = 25,
x_secret: Optional[str] = Header(None)
):
verify_secret(x_secret)
# In production: pull known active addresses from CLOB /trades,
# then score each. Here we return a scaffold with the scoring logic.
return {
“message”: “In production, provide a list of addresses to score via POST /wallets/score”,
“min_win_rate”: min_win_rate,
“limit”: limit,
“note”: “Use the Polymarket leaderboard or CLOB trade data to seed wallet addresses.”,
}

@app.post(”/wallets/score”)
async def score_wallets(
body: dict,
x_secret: Optional[str] = Header(None)
):
“”“Score a list of wallet addresses and return those above MIN_WIN_RATE.”””
verify_secret(x_secret)
addresses = body.get(“addresses”, [])
if not addresses:
raise HTTPException(400, “Provide a list of addresses”)
results = []
for addr in addresses[:50]:  # cap at 50 to avoid rate limits
try:
stats = await score_wallet(addr)
if stats[“win_rate”] >= min(body.get(“min_win_rate”, MIN_WIN_RATE), 0.5):
results.append(stats)
except Exception as e:
log.warning(f”Failed to score {addr}: {e}”)
results.sort(key=lambda x: x[“win_rate”], reverse=True)
return {“wallets”: results[:25], “count”: len(results)}

@app.post(”/execute”)
async def execute_trade(
signal: TradeSignal,
x_secret: Optional[str] = Header(None)
):
“””
Main entry point — UI sends a trade signal, we validate + execute it.
“””
verify_secret(x_secret)
validate_signal(signal)

```
# Apply slippage to limit price
if signal.action == "BUY_YES":
    side       = "BUY"
    exec_price = min(signal.price * (1 + DEFAULT_SLIPPAGE), 0.99)
else:
    side       = "SELL"
    exec_price = max(signal.price * (1 - DEFAULT_SLIPPAGE), 0.01)

# Size in shares = USD / price
size_shares = round(signal.size_usd / exec_price, 2)

log_entry = {
    "id":           f"trade-{int(time.time()*1000)}",
    "action":       signal.action,
    "market_id":    signal.market_id,
    "question":     signal.question,
    "side":         side,
    "price":        exec_price,
    "size_usd":     signal.size_usd,
    "size_shares":  size_shares,
    "confidence":   signal.confidence,
    "wallet_source":signal.wallet_source,
    "wallet_wr":    signal.wallet_win_rate,
    "status":       "pending",
    "dry_run":      DRY_RUN,
    "date":         datetime.now(timezone.utc).date().isoformat(),
    "timestamp":    datetime.now(timezone.utc).isoformat(),
}

if DRY_RUN:
    log_entry["status"]   = "simulated"
    log_entry["order_id"] = f"dry-{log_entry['id']}"
    trade_log.append(log_entry)
    log.info(f"[DRY RUN] {side} {size_shares} shares @ {exec_price:.4f} on market {signal.market_id[:16]}…")
    return {"ok": True, "dry_run": True, "trade": log_entry}

# ── LIVE EXECUTION ────────────────────────────────────
if not (API_KEY and API_SECRET and API_PASSPHRASE and PRIVATE_KEY):
    raise HTTPException(503, "API credentials not configured. Set env vars and restart.")

try:
    result = await client.place_order(
        token_id = signal.market_id,
        side     = side,
        price    = exec_price,
        size     = size_shares,
    )
    order_id = result.get("order_id") or result.get("id", "unknown")
    log_entry["status"]   = "filled" if result.get("status") == "MATCHED" else "open"
    log_entry["order_id"] = order_id
    order_cache[order_id] = log_entry
    trade_log.append(log_entry)
    log.info(f"[LIVE] Order {order_id} placed: {side} {size_shares}sh @ {exec_price}")
    return {"ok": True, "dry_run": False, "order_id": order_id, "trade": log_entry}
except httpx.HTTPStatusError as e:
    log.error(f"CLOB order failed: {e.response.text}")
    raise HTTPException(502, f"Order rejected by Polymarket: {e.response.text}")
except Exception as e:
    log.error(f"Order error: {e}")
    raise HTTPException(500, str(e))
```

@app.post(”/cancel”)
async def cancel_trade(
req: CancelRequest,
x_secret: Optional[str] = Header(None)
):
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
return {
“trades”: trade_log,
“total”:  len(trade_log),
“pnl_estimate”: sum(
t[“size_usd”] * (t[“confidence”] - 0.5) * 2
for t in trade_log if t[“status”] in (“filled”,“simulated”)
)
}

# ── WATCHER: auto-copy when tracked wallets place trades ──

async def wallet_watcher(addresses: list[str], poll_seconds: int = 15):
“””
Background task: polls tracked wallets for new trades and
auto-executes copies if win rate threshold is met.
Pipe results into /execute internally.
“””
seen_trades: set[str] = set()
log.info(f”Watcher started for {len(addresses)} wallets, polling every {poll_seconds}s”)
markets = await fetch_active_markets(20)
market_map = {m.get(“condition_id”,””): m for m in markets}

```
while True:
    for addr in addresses:
        try:
            trades = await client.get_trades(addr, limit=10)
            for t in trades:
                tid = t.get("id") or t.get("transaction_hash","")
                if tid in seen_trades:
                    continue
                seen_trades.add(tid)
                stats = await score_wallet(addr)
                if stats["win_rate"] < MIN_WIN_RATE:
                    continue
                market = market_map.get(t.get("market",""), {})
                side   = t.get("side","").upper()
                price  = float(t.get("price", 0.5))
                size   = min(float(t.get("size",0)) * price, MAX_ORDER_SIZE)
                if size < 1.0 or not market:
                    continue
                signal = TradeSignal(
                    action         = "BUY_YES" if side == "BUY" else "BUY_NO",
                    market_id      = t.get("market",""),
                    question       = market.get("question","Unknown market"),
                    size_usd       = size,
                    price          = price,
                    confidence     = stats["win_rate"],
                    wallet_source  = addr,
                    wallet_win_rate= stats["win_rate"],
                )
                log.info(f"[WATCHER] Auto-copying {addr[:10]}… on {signal.question[:40]}")
                await execute_trade(signal, x_secret=SERVER_SECRET)
        except Exception as e:
            log.warning(f"Watcher error for {addr}: {e}")
    await asyncio.sleep(poll_seconds)
```

@app.on_event(“startup”)
async def startup():
log.info(f”╔══ CopyTrade Server started ({‘DRY RUN’ if DRY_RUN else ‘🔴 LIVE’}) ══╗”)
log.info(f”  Min win rate : {MIN_WIN_RATE:.0%}”)
log.info(f”  Max order    : ${MAX_ORDER_SIZE}”)
log.info(f”  Credentials  : {‘✓ loaded’ if API_KEY else ‘✗ missing — set .env’}”)
# To start the auto-watcher, uncomment and provide wallet addresses:
# tracked = [“0xABCD…”, “0x1234…”]
# asyncio.create_task(wallet_watcher(tracked))

if **name** == “**main**”:
import uvicorn
uvicorn.run(“server:app”, host=“0.0.0.0”, port=8000, reload=True)