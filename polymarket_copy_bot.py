#!/usr/bin/env python3
"""
Polymarket Copy Trading Bot
Finds top wallets with >90% win rate and mirrors their trades.
Pinned wallet: 0xade6c822315a1c945aa168a5b90b22b200b788b8

Requirements:
  pip install py-clob-client requests websockets python-dotenv

Setup:
  1. Create a .env file in the same folder as this script with:
       PRIVATE_KEY=0xyourprivatekeyhere
       FUNDER_ADDRESS=0xyourfunderaddresshere
  2. Make sure your wallet has USDC on Polygon (chain ID 137)
  3. Keep a small amount of POL for gas (~$0.10 is enough)
  4. Run: python polymarket_copy_bot.py
"""

import os, time, logging
from dotenv import load_dotenv
import requests

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("copytrade")

# ─── Config ────────────────────────────────────────────────────────────────────
CLOB_HOST      = "https://clob.polymarket.com"
DATA_API       = "https://data-api.polymarket.com"
GAMMA_API      = "https://gamma-api.polymarket.com"
CHAIN_ID       = 137
PRIVATE_KEY    = os.getenv("PRIVATE_KEY")
FUNDER         = os.getenv("FUNDER_ADDRESS")
SIG_TYPE       = 1             # 1 = email/Magic wallet proxy, 0 = MetaMask/EOA
MAX_TRADE_USDC = 20.0          # max USDC per copied trade
MAX_POS_USDC   = 50.0          # max USDC exposure per market
MIN_WIN_RATE   = 0.90          # 90% win rate filter
MIN_TRADES     = 50            # minimum resolved trades for credibility
TOP_N          = 10            # number of wallets to track from leaderboard
PRICE_BUFFER   = 0.02          # 2% slippage buffer on taker orders
ORDER_TYPE     = OrderType.FOK # FOK = fill or kill (taker), GTC = maker

# Always copy-trade these wallets regardless of leaderboard ranking
PINNED_WALLETS = [
    "0xade6c822315a1c945aa168a5b90b22b200b788b8",
]
# ───────────────────────────────────────────────────────────────────────────────


class LeaderboardFetcher:
    """Fetches and filters top traders from Polymarket Data API."""

    def fetch_top_wallets(self) -> list[dict]:
        log.info("Fetching trader leaderboard (min win rate: 90%)...")
        try:
            r = requests.get(
                f"{DATA_API}/profiles",
                params={"limit": 200, "offset": 0, "sortBy": "profitAndLoss"},
                timeout=10
            )
            r.raise_for_status()
            traders = r.json()
        except Exception as e:
            log.warning(f"Data API failed ({e}), trying Gamma API fallback...")
            r = requests.get(f"{GAMMA_API}/leaderboard", timeout=10)
            traders = r.json()

        qualified = []
        for t in traders:
            wins   = t.get("positivePnl", 0) or t.get("wins", 0)
            losses = t.get("negativePnl", 0) or t.get("losses", 0)
            total  = wins + losses
            if total < MIN_TRADES:
                continue
            win_rate = wins / total if total > 0 else 0
            if win_rate < MIN_WIN_RATE:
                continue
            qualified.append({
                "address":  t.get("proxyWallet") or t.get("address"),
                "win_rate": win_rate,
                "trades":   total,
                "pnl":      t.get("pnl") or t.get("profitAndLoss", 0),
            })

        qualified.sort(key=lambda x: x["win_rate"], reverse=True)
        top = qualified[:TOP_N]

        # Force-add pinned wallets at the top
        existing = {w["address"] for w in top}
        for addr in reversed(PINNED_WALLETS):
            if addr not in existing:
                top.insert(0, {"address": addr, "win_rate": None, "trades": 0, "pnl": 0})
                log.info(f"  [pinned] {addr}")

        for i, w in enumerate(top):
            wr = f"{w['win_rate']*100:.1f}%" if w["win_rate"] else "unknown"
            log.info(f"  [{i+1}] {w['address']} — win rate: {wr}")
        return top


class TradeExecutor:
    """Signs and submits copy orders via py-clob-client."""

    def __init__(self):
        self.client = ClobClient(
            CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
            signature_type=SIG_TYPE, funder=FUNDER
        )
        self.client.set_api_creds(self.client.create_or_derive_api_creds())
        log.info("CLOB client initialized")

    def get_balance(self) -> float:
        return float(self.client.get_balance()) / 1e6  # USDC has 6 decimals

    def current_exposure(self, token_id: str) -> float:
        for p in self.client.get_positions():
            if p.get("asset") == token_id:
                return float(p.get("size", 0)) * float(p.get("avgPrice", 0))
        return 0.0

    def execute_copy(self, token_id: str, price: float, size_usdc: float) -> bool:
        size_usdc = min(size_usdc, MAX_TRADE_USDC)

        if self.current_exposure(token_id) + size_usdc > MAX_POS_USDC:
            log.warning("Skipped: max exposure per market would be exceeded")
            return False

        if self.get_balance() < size_usdc:
            log.warning(f"Insufficient balance for trade of {size_usdc:.2f} USDC")
            return False

        if price > 0.95:
            log.info(f"Skipped: market near resolution (price={price:.3f})")
            return False

        try:
            order  = MarketOrderArgs(
                token_id=token_id,
                amount=size_usdc,
                side=BUY,
                order_type=ORDER_TYPE,
            )
            signed = self.client.create_market_order(order)
            resp   = self.client.post_order(signed, ORDER_TYPE)
            if resp.get("status") in ("matched", "delayed"):
                log.info(f"FILLED: {size_usdc:.2f} USDC @ {price:.3f} [{resp['status']}]")
                return True
            log.warning(f"Order not filled: {resp}")
            return False
        except Exception as e:
            log.error(f"Order error: {e}")
            return False


class WalletMonitor:
    """Polls trade history for a watched wallet and detects new trades."""

    def __init__(self, address: str, executor: TradeExecutor):
        self.address  = address
        self.executor = executor
        self.seen_txs: set[str] = set()

    def poll(self):
        try:
            r = requests.get(
                f"{DATA_API}/activity",
                params={"user": self.address, "limit": 20},
                timeout=8
            )
            r.raise_for_status()
            events = r.json()
        except Exception as e:
            log.warning(f"Poll failed for {self.address[:10]}...: {e}")
            return

        for evt in events:
            tx_id = evt.get("transactionHash") or evt.get("id")
            if tx_id in self.seen_txs:
                continue
            self.seen_txs.add(tx_id)

            if evt.get("type") not in ("trade", "BUY"):
                continue

            token_id  = evt.get("asset") or evt.get("tokenId")
            price     = float(evt.get("price", 0))
            size_usdc = float(evt.get("usdcSize") or evt.get("amount", 0))

            if not token_id or price <= 0 or size_usdc <= 0:
                continue

            log.info(f"NEW TRADE from {self.address[:10]}... | token={token_id[:12]}... | price={price:.3f} | size={size_usdc:.2f} USDC")
            self.executor.execute_copy(token_id, price, size_usdc)


class CopyBot:
    """Main bot: fetches leaderboard, spins up monitors, runs poll loop."""

    def __init__(self):
        self.executor = TradeExecutor()
        self.monitors: list[WalletMonitor] = []

    def setup(self):
        wallets = LeaderboardFetcher().fetch_top_wallets()
        if not wallets:
            raise RuntimeError("No wallets met the >90% win rate criteria.")
        self.monitors = [WalletMonitor(w["address"], self.executor) for w in wallets]
        log.info(f"Monitoring {len(self.monitors)} wallets")

    def run(self):
        self.setup()
        log.info("Bot running. Press Ctrl+C to stop.")
        try:
            while True:
                for m in self.monitors:
                    m.poll()
                    time.sleep(0.5)   # stagger requests (stay under 30 req/min)
                time.sleep(10)        # re-poll all wallets every ~15s
        except KeyboardInterrupt:
            log.info("Stopped by user.")


if __name__ == "__main__":
    CopyBot().run()
