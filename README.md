# DOGE Grid Trading Bot

A self-sustaining grid trading bot for DOGE/USD on Kraken, designed to cover its own hosting costs ($5/month on Railway) with profits accumulating as DOGE.

## How Grid Trading Works

```
Price ──────────────────────────────────
       SELL $0.0936  ─── Grid Level +4
       SELL $0.0927  ─── Grid Level +3
       SELL $0.0918  ─── Grid Level +2
       SELL $0.0909  ─── Grid Level +1
     ► $0.0900       ─── CENTER PRICE
       BUY  $0.0891  ─── Grid Level -1
       BUY  $0.0882  ─── Grid Level -2
       BUY  $0.0873  ─── Grid Level -3
       BUY  $0.0864  ─── Grid Level -4
```

- When a **buy fills** → place a **sell one level up**
- When a **sell fills** → place a **buy one level down**
- Each completed cycle captures **grid spacing minus fees** as profit
- At 1.0% spacing with 0.50% round-trip fees = **0.50% net per cycle**

## Architecture

```
doge-grid-bot/
├── config.py          # All tunable parameters (env-var driven)
├── kraken_client.py   # Kraken REST API wrapper (HMAC-SHA512 auth)
├── grid_strategy.py   # Core grid logic: levels, fills, pair cycling
├── ai_advisor.py      # Hourly market analysis via Groq (Llama 3.1)
├── notifier.py        # Telegram alerts (trades, P&L, risk warnings)
├── bot.py             # Main loop, graceful shutdown, health check
└── README.md          # You are here
```

**Zero external dependencies.** Pure Python standard library (`urllib`, `hmac`, `hashlib`, `json`, `csv`, `time`, `logging`).

## Quick Start

### 1. Clone and configure

```bash
cd doge-grid-bot
cp .env.example .env
# Edit .env with your API keys
```

### 2. Run in dry-run mode (safe — uses real prices, simulates trades)

```bash
python bot.py
```

The bot will:
- Fetch real DOGE prices from Kraken
- Simulate grid orders and fills
- Log everything including simulated P&L
- Send Telegram notifications tagged `[DRY RUN]`

### 3. Monitor via Telegram

You'll receive notifications for:
- Bot startup/shutdown
- Each completed round trip (with profit)
- Daily P&L summary at midnight UTC
- Risk events (stop floor, daily loss limit)
- AI advisor hourly recommendations
- Errors requiring attention

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `KRAKEN_API_KEY` | | Kraken API key |
| `KRAKEN_API_SECRET` | | Kraken API secret |
| `GROQ_API_KEY` | | Groq API key for AI advisor |
| `TELEGRAM_BOT_TOKEN` | | Telegram bot token |
| `TELEGRAM_CHAT_ID` | | Your Telegram chat ID |
| `DRY_RUN` | `true` | Simulate trades (set `false` for live) |
| `STARTING_CAPITAL` | `120` | USD allocated to grid trading |
| `ORDER_SIZE_USD` | `5` | USD per grid order |
| `GRID_LEVELS` | `4` | Levels above + below center |
| `GRID_SPACING_PCT` | `1.0` | % gap between grid levels |
| `STOP_FLOOR` | `100` | Min portfolio value before emergency stop |
| `DAILY_LOSS_LIMIT` | `3` | Max daily loss before pause |
| `GRID_DRIFT_RESET_PCT` | `5.0` | Price drift % to trigger grid rebuild |
| `POLL_INTERVAL_SECONDS` | `30` | Main loop interval |
| `AI_ADVISOR_INTERVAL` | `3600` | Seconds between AI checks |
| `LOG_LEVEL` | `INFO` | Python log level |

## Backtesting (v1)

Use the v1 historical replay runner:

```bash
python3 backtest_v1.py --pair XDGUSD --interval 15 --start 2025-01-01 --end 2026-01-01
```

Or replay a local candle CSV:

```bash
python3 backtest_v1.py --csv data/XDGUSD_15m.csv --pair XDGUSD --interval 15
```

Full guide: `docs/backtest_v1.md`.

## Deploy to Railway

### Option A: Railway CLI

```bash
railway login
railway init
railway up
```

### Option B: Git push

1. Create a new project on [Railway](https://railway.app)
2. Connect your GitHub repo
3. Set environment variables in the Railway dashboard
4. Deploy — Railway auto-detects the `railway.toml`

### Important Railway settings

- The bot runs as a **worker process** (not a web server)
- It exposes a health check HTTP endpoint on `$PORT` (Railway sets this)
- Set `DRY_RUN=true` initially, watch logs for a few days, then switch to `false`

## Risk Management

| Protection | Trigger | Action |
|------------|---------|--------|
| **Stop Floor** | Portfolio < $100 | Cancel all, shut down |
| **Daily Loss Limit** | Day losses > $3 | Pause until midnight UTC |
| **Grid Drift Reset** | Price moves > 5% from center | Cancel and rebuild grid |
| **API Error Limit** | 10 consecutive failures | Shut down |
| **Graceful Shutdown** | SIGTERM/SIGINT | Cancel all open orders |

## Fee Math

```
Kraken maker fee:     0.25% per side
Round trip fee:       0.50% (buy + sell)
Grid spacing:         1.00%
Net profit per cycle: 0.50%

Per $5 order:         $0.025 profit per round trip
Target:               ~6 round trips/day = $0.15/day
Monthly:              $0.15 × 30 = $4.50 (plus extras from volatile days)
```

## AI Advisor

Every hour, the bot queries Groq's free API (Llama 3.1 8B) with market context. The AI classifies the market and recommends grid adjustments.

**v1 is advisory only** — recommendations are logged and sent to Telegram but never auto-acted on. Review AI suggestions in `logs/ai_recommendations.csv` before giving it control.

## DOGE Accumulation

When profits exceed the $5/month hosting reserve:
1. Calculate excess USD
2. Convert to DOGE via market buy
3. Track separately from grid trading
4. Building toward the 1,000,000 DOGE goal

## Systemd Fallback (VPS deployment)

If not using Railway, create `/etc/systemd/system/doge-grid-bot.service`:

```ini
[Unit]
Description=DOGE Grid Trading Bot
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/doge-grid-bot
EnvironmentFile=/path/to/doge-grid-bot/.env
ExecStart=/usr/bin/python3 bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable doge-grid-bot
sudo systemctl start doge-grid-bot
sudo journalctl -u doge-grid-bot -f  # Watch logs
```
