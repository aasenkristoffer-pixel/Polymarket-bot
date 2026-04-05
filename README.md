# Polymarket Copy Trading Bot

Automatically copies trades from the top 10 Polymarket wallets with a **≥90% win rate**.

## Features
- Fetches top traders live from Polymarket leaderboard
- Pinned wallet: `0xade6c822315a1c945aa168a5b90b22b200b788b8`
- Max $20 USDC per trade, max $50 USDC exposure per market
- Skips markets near resolution (price > 0.95)
- Auto-restarts on failure when deployed to Railway

## Files
| File | Purpose |
|------|---------|
| `polymarket_copy_bot.py` | Main bot code |
| `requirements.txt` | Python dependencies |
| `railway.json` | Railway deployment config |
| `.env.example` | Credentials template |
| `.gitignore` | Prevents accidental key uploads |

## Setup

### Local
```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your real credentials
python polymarket_copy_bot.py
```

### Railway (cloud, runs 24/7)
1. Push this repo to GitHub
2. Go to railway.app → New Project → Deploy from GitHub
3. Add environment variables in Railway dashboard:
   - `PRIVATE_KEY`
   - `FUNDER_ADDRESS`
4. Deploy

## Configuration
Edit these values at the top of `polymarket_copy_bot.py`:

```python
MAX_TRADE_USDC = 20.0   # max USDC per trade
MAX_POS_USDC   = 50.0   # max exposure per market
MIN_WIN_RATE   = 0.90   # minimum win rate filter (90%)
TOP_N          = 10     # number of wallets to track
SIG_TYPE       = 1      # 1 = Magic/email wallet, 0 = MetaMask
```

## Requirements
- USDC on Polygon (chain ID 137)
- Small amount of POL for gas (~$0.10)

## Warning
This bot trades with real money. Start with a small `MAX_TRADE_USDC` (e.g. $1–2) to test before increasing. Never share your private key.
