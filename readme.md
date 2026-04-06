# Polymarket CopyTrade Bot — Setup Guide

## What you’re running

A FastAPI server that:

1. Receives trade signals from your UI
1. Validates them (win rate check, size cap, risk rules)
1. Signs and submits real orders to Polymarket’s CLOB
1. Optionally watches tracked wallets and auto-copies their trades

-----

## Quick Start (Local / VPS)

### 1. Install Python 3.11+

```bash
python --version   # need 3.11+
# macOS:  brew install python@3.11
# Ubuntu: sudo apt install python3.11 python3.11-venv
```

### 2. Clone / copy these files

```
copytradebot/
├── server.py
├── requirements.txt
├── .env.example
└── .env           ← you create this
```

### 3. Create virtual environment

```bash
cd copytradebot
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
pip install py-clob-client        # Polymarket official SDK
```

### 4. Configure credentials

```bash
cp .env.example .env
nano .env   # fill in your values (see below)
```

**Where to get each value:**

|Variable             |Where to get it                                |
|---------------------|-----------------------------------------------|
|`WALLET_PRIVATE_KEY` |MetaMask → Account Details → Export Private Key|
|`POLY_API_KEY`       |polymarket.com → Profile → API Keys → Create   |
|`POLY_API_SECRET`    |Same page, shown once on creation              |
|`POLY_API_PASSPHRASE`|Same page                                      |
|`SERVER_SECRET`      |Run: `openssl rand -hex 32`                    |

### 5. Start in DRY RUN first (no real money)

```bash
# .env: DRY_RUN=true
python server.py
# Server starts at http://localhost:8000
```

### 6. Test it works

```bash
curl http://localhost:8000/status
# Should return: {"ok":true,"dry_run":true,...}
```

### 7. Send a test trade from your UI

Your UI needs to add this header + call the server:

```
POST http://localhost:8000/execute
Headers: x-secret: <your SERVER_SECRET>
Body: {
  "action": "BUY_YES",
  "market_id": "TOKEN_ID_FROM_POLYMARKET",
  "question": "Will BTC exceed $90k?",
  "size_usd": 5.0,
  "price": 0.42,
  "confidence": 0.85,
  "wallet_source": "0xABC...",
  "wallet_win_rate": 0.91
}
```

### 8. Go live

```bash
# .env: DRY_RUN=false
# Make sure you have USDC on Polygon in your wallet
python server.py
```

-----

## Connect the UI to this server

In the React bot UI, find the `executeTrade` function and replace it with:

```javascript
async function executeTrade(signal, market, wallet) {
  const res = await fetch("http://YOUR_SERVER_IP:8000/execute", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-secret": "YOUR_SERVER_SECRET",   // from .env
    },
    body: JSON.stringify({
      action:           signal.action,
      market_id:        market.tokens?.[0]?.token_id || market.id,
      question:         market.question,
      size_usd:         signal.size_pct,   // or map to dollar amount
      price:            signal.action === "BUY_YES"
                          ? parseFloat(market.outcomePrices?.[0] || 0.5)
                          : parseFloat(market.outcomePrices?.[1] || 0.5),
      confidence:       signal.confidence,
      wallet_source:    wallet.addr,
      wallet_win_rate:  wallet.winRate,
    }),
  });
  return res.json();
}
```

-----

## Deploy to a VPS (DigitalOcean / Hetzner / AWS)

### 1. Spin up a $6/mo Ubuntu 24.04 droplet

### 2. SSH in and install

```bash
ssh root@YOUR_SERVER_IP
apt update && apt install -y python3.11 python3.11-venv git

mkdir copytrade && cd copytrade
# upload your files via scp or git clone
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt py-clob-client
cp .env.example .env && nano .env
```

### 3. Run as a background service (systemd)

```bash
nano /etc/systemd/system/copytrade.service
```

```ini
[Unit]
Description=Polymarket CopyTrade Bot
After=network.target

[Service]
User=root
WorkingDirectory=/root/copytrade
ExecStart=/root/copytrade/venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
Restart=always
EnvironmentFile=/root/copytrade/.env

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable copytrade
systemctl start copytrade
systemctl status copytrade
```

### 4. Secure it with nginx + HTTPS (recommended)

```bash
apt install -y nginx certbot python3-certbot-nginx
# Point your domain to the server IP, then:
certbot --nginx -d yourdomain.com
```

nginx config (`/etc/nginx/sites-available/copytrade`):

```nginx
server {
    server_name yourdomain.com;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

-----

## Enable Auto-Watcher (fully automated)

To auto-copy wallets without clicking anything, edit `server.py` startup:

```python
@app.on_event("startup")
async def startup():
    tracked = [
        "0xWALLET_ADDRESS_1",
        "0xWALLET_ADDRESS_2",
        # ... up to 25 addresses
    ]
    asyncio.create_task(wallet_watcher(tracked, poll_seconds=15))
```

The watcher polls every 15 seconds, detects new trades, scores the wallet,
and auto-executes if win rate ≥ MIN_WIN_RATE.

**Where to find top wallet addresses:**

- Polymarket leaderboard: https://polymarket.com/leaderboard
- CLOB API: `GET https://clob.polymarket.com/trades?limit=1000` — extract top maker_address values

-----

## API Reference

|Endpoint        |Method|Description              |
|----------------|------|-------------------------|
|`/status`       |GET   |Server health, config    |
|`/balance`      |GET   |Your USDC balance        |
|`/orders`       |GET   |Open orders              |
|`/trades`       |GET   |Trade history + PnL      |
|`/markets`      |GET   |Active Polymarket markets|
|`/execute`      |POST  |Execute a copied trade   |
|`/cancel`       |POST  |Cancel an open order     |
|`/wallets/score`|POST  |Score a list of wallets  |

All routes except `/status` and `/markets` require `x-secret` header.

-----

## Safety Checklist

- [ ] Start with `DRY_RUN=true` for at least 24 hours
- [ ] Set `MAX_ORDER_SIZE=10` when first going live
- [ ] Never store private key in git — use `.env` only
- [ ] Use a dedicated wallet with only trading funds
- [ ] Monitor `/trades` endpoint for unexpected activity
- [ ] Keep USDC balance low — only what you’re willing to trade

-----

⚠️ **Disclaimer:** This software is for educational purposes. Prediction market trading carries risk of total loss. Not financial advice.