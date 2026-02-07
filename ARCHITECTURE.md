# DOGE Grid Bot -- Architecture & Statistical Analysis Plan

## System Overview

A self-hosted DOGE/USD grid trading bot for Kraken, written in pure Python 3.12 (zero external dependencies). Runs on Railway ($5/month) and pays for itself through micro-profits from price oscillation.

The core idea: place a ladder of limit orders above and below the current price. When price dips and fills a buy, immediately place a sell one level up. When that sell fills, the spread minus fees is profit. Repeat thousands of times.

```
         SELL +4  $0.0936  ───┐
         SELL +3  $0.0927     │
         SELL +2  $0.0918     │  Grid captures profit
         SELL +1  $0.0909     │  from ANY oscillation
  ────── CENTER   $0.0900  ───┤  within this range
         BUY  -1  $0.0891     │
         BUY  -2  $0.0882     │
         BUY  -3  $0.0873     │
         BUY  -4  $0.0864  ───┘
```

---

## Module Map (8 files)

### `config.py` -- Settings Layer
All parameters read from environment variables via `_env(name, default, cast)`. Key values:

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `DRY_RUN` | `True` | Simulates everything; real prices, fake orders |
| `STARTING_CAPITAL` | $120 | Total USD allocated to the grid |
| `ORDER_SIZE_USD` | $3.50 | Per-order dollar value (adaptive, overridden each build) |
| `GRID_LEVELS` | 10 | Levels per side (adaptive, recalculated each build) |
| `GRID_SPACING_PCT` | 1.0% | Gap between adjacent levels |
| `MAKER_FEE_PCT` | 0.25% | Kraken maker fee; round-trip = 0.50% |
| `STOP_FLOOR` | $100 | Hard circuit breaker -- cancel everything below this |
| `DAILY_LOSS_LIMIT` | $3 | Pause trading for the day if cumulative losses exceed |
| `GRID_DRIFT_RESET_PCT` | 5% | Rebuild grid when price wanders this far from center |
| `POLL_INTERVAL_SECONDS` | 30s | Main loop tick rate |
| `AI_ADVISOR_INTERVAL` | 3600s | How often to query the AI model |
| `HEALTH_PORT` | 8080 | HTTP server port (dashboard + Railway health check) |

### `kraken_client.py` -- Exchange API
Pure `urllib` wrapper for Kraken REST v0. Handles:
- **Public** (no auth): `get_price()`, `get_spread()`, `get_ticker()`, `get_ohlc()`
- **Private** (HMAC-SHA512 signed): `place_order()`, `cancel_order()`, `cancel_all_orders()`, `query_orders()`, `get_balance()`, `get_trades_history()`
- **Rate limiting**: Tracks a 15-unit call counter that decays at 1/sec. Sleeps if budget is exhausted.
- **Dry run**: Returns fake txids (`DRY-B-1738...`) and skips all private endpoints.

Signing: `HMAC-SHA512(url_path + SHA256(nonce + post_body), base64_decode(secret))`

### `grid_strategy.py` -- Trading Logic (the brain)
Two core data structures:

**`GridOrder`**: level, side, price, volume, txid, status, placed_at

**`GridState`**: the full bot state -- center price, all grid orders, profit tracking (today/total/fees), risk state (paused, consecutive errors), price history (24h ring buffer), recent fills, trend ratio, AI recommendation.

Key functions:
- `adapt_grid_params(state, price)` -- Recalculates order size (hugs Kraken's 13 DOGE minimum + 20% buffer) and grid levels (fills capital budget at 60% worst-case exposure). Profits grow effective capital, so the grid naturally expands as you earn.
- `calculate_grid_levels(center, buy_levels, sell_levels)` -- Generates asymmetric price ladder based on trend ratio.
- `build_grid(state, price)` -- Cancels old grid, adapts params, computes asymmetric buy/sell split from trend ratio, places all orders.
- `check_fills(state, price)` -- Dispatches to live (queries Kraken order status) or dry-run (price crossed order price = filled).
- `handle_fills(state, filled)` -- The money-making core. For each fill, places the opposite order one level away. When a sell fills, calculates and records round-trip profit.
- `update_trend_ratio(state)` -- Computes buy/sell fill asymmetry over a 12h window, confidence-scaled toward 0.5, clamped to [0.25, 0.75]. Drives asymmetric grid split.
- `check_grid_drift(state, price)` -- Triggers grid rebuild if price wanders >5% from center.
- `check_risk_limits(state, price)` -- Returns (should_stop, should_pause, reason). Checks stop floor, daily loss limit, consecutive errors.
- `check_accumulation(state)` / `execute_accumulation(state, usd, price)` -- Weekly sweep of excess profits into DOGE market buys.

### `ai_advisor.py` -- LLM Market Analysis
Calls any OpenAI-compatible endpoint (default: NVIDIA build.nvidia.com free tier, Llama 3.1 8B Instruct). Runs every `AI_ADVISOR_INTERVAL` seconds.

Flow: gather market context (price, 1h/4h/24h changes, spread, fill count) -> build compact prompt -> parse structured 3-line response (CONDITION / ACTION / REASON) -> log to CSV.

Actions: `continue`, `pause`, `widen_spacing`, `tighten_spacing`, `reset_grid`. Non-continue actions go to Telegram for human approval via inline buttons.

Never auto-acts. Never blocks trading on failure.

### `notifier.py` -- Telegram Alerts
Pure `urllib` calls to `api.telegram.org`. Supports:
- Plain messages (startup, shutdown, round trips, daily summary, risk events, errors, accumulation)
- Inline keyboard buttons (AI recommendation approve/skip)
- Callback polling (short-poll `getUpdates`, timeout=0)
- Message editing (update AI recommendation status after approval/skip/expiry)

### `bot.py` -- Main Loop & Orchestration
The entry point. Ties everything together:

1. **Init**: logging, signal handlers, banner, health/dashboard server
2. **Fetch price**: initial price from Kraken public API
3. **Build grid**: initial grid around current price
4. **Main loop** (every 30s):
   - a. Fetch price, record to history
   - b. Check daily reset (midnight UTC)
   - c. Check risk limits (stop floor, daily loss)
   - d. Check grid drift (>5% from center -> rebuild)
   - e. Check fills -> place replacement orders -> update trend ratio -> ratio-drift rebuild
   - f. AI advisor (hourly) -> Telegram with approve/skip buttons
   - f2. Poll Telegram callbacks + check approval expiry
   - f3. Apply web dashboard config changes
   - g. DOGE accumulation sweep
   - h. Periodic status log (every ~5 min)
   - i. Sleep remainder of poll interval
5. **Shutdown** (SIGTERM/SIGINT/SIGBREAK): cancel all orders, log final state, notify Telegram

Also handles: Telegram text commands (`/status`, `/interval`, `/check`, `/spacing`, `/ratio`, `/help`), AI approval workflow (pending approval with 10-min expiry), web config queue.

### `dashboard.py` -- Web Dashboard
Single-page dark-theme app served as a Python string constant. No external dependencies.

**`DASHBOARD_HTML`**: Full HTML/CSS/JS with auto-refresh every 5s:
- Header with mode badge (DRY RUN / LIVE / PAUSED), uptime
- 6 metric cards: price, center, today P&L, total P&L, round trips, DOGE accumulated
- Grid ladder: orders sorted by price desc, buy=green/sell=red, yellow current price marker
- Trend ratio bar: buy%/sell% visual split, source label, 12h fill counts
- Adaptive params table: order size, levels, spacing, capital, fees, AI interval, AI rec
- Controls: spacing, ratio (manual + auto), AI interval -- all via POST /api/config
- Recent fills table: last 20 fills with time/side/price/volume/profit

**`serialize_state(state, price)`**: Converts `GridState` + current price into a JSON-ready dict with everything the dashboard needs.

**`DashboardHandler`** (in bot.py): Routes GET `/` (dashboard), GET `/api/status` (JSON), POST `/api/config` (validate + queue), GET `/health` (legacy). Config changes are queued in `_web_config_pending` (thread-safe dict swap) and applied by the main loop via `_apply_web_config()`.

---

## Data Flow

```
Kraken API ──> kraken_client.py ──> grid_strategy.py ──> bot.py main loop
                                          │                    │
                                          │                    ├──> notifier.py ──> Telegram
                                          │                    ├──> ai_advisor.py ──> LLM API
                                          │                    └──> dashboard.py ──> Browser
                                          │
                                          └──> CSV logs (trades.csv, daily_summary.csv,
                                                         ai_recommendations.csv)
```

**State lives in one place**: `GridState` in `grid_strategy.py`. Everything else reads from it.

**Thread boundary**: The HTTP dashboard server runs in a daemon thread. It only reads `GridState` (via `serialize_state`) and writes to `_web_config_pending`. The main loop owns all mutations.

---

## What the Bot Already Tracks

The raw data available for statistical analysis:

| Data | Location | Granularity |
|------|----------|-------------|
| Price samples | `state.price_history` | Every 30s, 24h ring buffer (~2880 points) |
| Fill events | `state.recent_fills` | Every fill: time, side, price, volume, profit, fees |
| Round-trip profits | `trades.csv` | Each completed sell: price, amount, fee, profit, grid level |
| Daily summaries | `daily_summary.csv` | Date, trade count, gross/net profit, fees, DOGE accumulated |
| AI recommendations | `ai_recommendations.csv` | Timestamp, condition, action, price, approval decision |
| Grid orders | `state.grid_orders` | Level, side, price, volume, status, placed_at |
| Trend ratio | `state.trend_ratio` | Updated on each fill event, 12h sliding window |

---

## 5 Statistical Analyzers: How They Help

Each grid fill is a structured sample from the market's price process. The bot already collects the data -- these analyzers extract signal from it.

### Analyzer 1: Profitability Significance Test

**The question**: Is the bot actually making money, or is the positive P&L just noise?

**The method**: One-sample t-test on round-trip net profits. H0: mean profit per round trip = 0. Calculate the t-statistic, p-value, and 95% confidence interval for the true mean profit.

**Why it's counterintuitive**: A bot showing +$2.47 total profit after 30 round trips *feels* profitable. But if the standard deviation of per-trip profit is high relative to the mean, the CI includes zero and you can't reject H0. You might need 100+ round trips before statistical significance. Early positive results are meaningless -- they could be explained entirely by the random walk.

**How it helps the bot**: Display the CI and required sample size on the dashboard. When the CI excludes zero, the bot has *proven* its edge. Before that, the "total profit" number is decorative. This prevents premature decisions to go live or increase capital based on insufficient evidence.

**Data source**: `state.recent_fills` (profit field on sell fills), or `trades.csv`.

### Analyzer 2: Fill Asymmetry Regime Detector

**The question**: Is the market trending or ranging right now, and how confident should we be?

**The method**: Binomial test on buy vs. sell fill counts over a sliding window. H0: P(buy fill) = 0.5 (symmetric, range-bound market). Calculate exact binomial p-value and the 95% CI for the true buy proportion.

**Why it's counterintuitive**: The bot already tracks trend ratio (`update_trend_ratio`), but it treats 6 buys / 2 sells the same regardless of sample size. The binomial test reveals that 6/2 with n=8 has p=0.29 -- *not significant*. The market might be perfectly symmetric and you got unlucky. But 30/10 with n=40 has p=0.003 -- you've detected a real trend. The current heuristic (confidence scaling by `min(1.0, total/8)`) is an approximation of this, but the real test is more precise and gives a p-value you can threshold on.

The deeper counterintuition: a significant asymmetry means the grid bot is fighting a trend. Those 30 buy fills without matching sells mean you're accumulating DOGE in a falling market. The busier the bot *looks*, the worse it's actually doing -- because round trips only complete when fills are symmetric.

**How it helps the bot**: When p < 0.05, the bot should automatically widen spacing (reduce exposure to trend continuation) or trigger a grid rebuild around the new price. This replaces the current drift-based rebuild (which waits for 5% movement) with a statistically rigorous signal that fires earlier.

**Data source**: `state.recent_fills` (side field), sliding window configurable.

### Analyzer 3: Censored Sample Bias Correction

**The question**: How deep did price actually go between our observations, and are we underestimating tail risk?

**The method**: The bot samples price every 30 seconds and checks fills against grid levels. But between samples, price may have spiked or crashed far beyond any grid level. This is *interval censoring* -- we only observe that a fill happened, not the exact moment or the extreme price reached.

Use the fills that *did* occur to estimate the parameters of the underlying price process (e.g., fit a GBM or jump-diffusion model). Then calculate the probability that price reached levels *beyond* the outermost grid level during inter-sample intervals. Compare the model's predicted fill distribution across levels to the observed distribution.

**Why it's counterintuitive**: If the bot records a buy fill at level -3 ($0.0873), the naive interpretation is "price touched $0.0873." But the actual low between polls might have been $0.0850 -- the bot just doesn't know because it wasn't looking. The fills create a *survivorship bias*: you only see the levels that were there, not the crash that blew past them. Your grid's "edge" levels feel safe because they haven't been tested, but the unobserved distribution says they're more exposed than the fill data suggests.

**How it helps the bot**: Produces a "true risk" estimate that's higher than what the fill data alone suggests. If the censoring correction shows >10% probability of price exceeding the grid range between polls, the bot should either: (a) add buffer levels beyond the grid, (b) reduce position size on edge levels, or (c) increase poll frequency during high-volatility periods.

**Data source**: `state.price_history` (30s samples), `state.recent_fills` (observed fill prices), plus Kraken OHLC data (`get_ohlc()`) for intra-candle high/low to calibrate the model.

### Analyzer 4: Volatility Regime Detection via Fill Rate

**The question**: Has the market's volatility regime changed, and should the grid adapt?

**The method**: Model fill arrivals as a Poisson process with rate lambda (fills per hour). Estimate lambda from a baseline period (e.g., first 48 hours of operation). Build a 95% CI for the "normal" fill rate. On each new fill, compute the rolling fill rate and test whether it's significantly above or below the baseline CI.

**Why it's counterintuitive**: A burst of 8 fills in one hour when the baseline is 1.5/hour feels like the bot is printing money. But a Poisson test (or chi-squared goodness-of-fit on inter-arrival times) reveals this is a >3-sigma event -- the underlying process has changed. The grid was parameterized for a 1.5/hour world (1.0% spacing, 30s polls). In an 8/hour world, the correct spacing is much wider because:
1. Higher volatility means trends are more likely (Analyzer 2 becomes relevant)
2. The same spacing captures more fills but each fill is riskier (more likely to be one-directional)
3. Wider spacing = fewer fills but higher profit per fill and lower trend exposure

The counterintuition: *you should widen spacing when the bot is busiest*, which is the opposite of what feels right.

**How it helps the bot**: When the fill rate significantly exceeds the upper CI bound, automatically widen spacing by a calculated amount (proportional to the deviation). When the fill rate drops below the lower bound, tighten spacing to capture more of the reduced volatility. This replaces static spacing with a statistically adaptive parameter.

**Data source**: `state.recent_fills` (timestamps), rolling windows of varying length.

### Analyzer 5: Random Walk Goodness-of-Fit (Market Type Test)

**The question**: Is the market a random walk at our timescale? If not, what is it, and does the grid have an edge?

**The method**: Under a symmetric random walk, the probability of hitting grid level +-k before returning to center follows a known distribution (inversely proportional to distance). Collect the empirical distribution of which levels get hit (from fill data) and run a chi-squared goodness-of-fit test against the theoretical random walk distribution.

**Why it's counterintuitive**: If the test *fails to reject* (p > 0.05), the market is behaving like a random walk at your timescale. A grid bot on a random walk is a *zero-edge strategy* after fees -- every expected profit from a round trip is exactly offset by the expected loss from inventory accumulation. You're playing a fair game minus fees. The bot "works" only because of finite-sample luck.

If the test *rejects* the random walk:
- **More inner-level hits than expected** -> mean reversion. The grid has a genuine statistical edge. Price oscillates in a tighter range than a random walk predicts, so round trips complete faster than the null model expects.
- **More outer-level hits than expected** -> momentum/fat tails. The grid is *anti-optimal*. Price trends past the grid range more often than a random walk, meaning you accumulate one-sided inventory that doesn't reverse.

**How it helps the bot**: This is the fundamental question -- *does grid trading work on DOGE at this timescale?* Everything else is parameter tuning. If the test consistently shows mean reversion, the strategy is sound and the bot should run aggressively. If it shows momentum, the strategy itself is wrong and no amount of spacing/ratio tuning will fix it. The bot should report this prominently on the dashboard.

**Data source**: `state.grid_orders` (fill counts per level), `state.recent_fills` (which levels filled), accumulated over the bot's lifetime.

---

## Integration Summary

| Analyzer | Input Data | Output | Frequency | Dashboard Display |
|----------|-----------|--------|-----------|-------------------|
| 1. Profitability t-test | Fill profits | CI, p-value, min sample needed | Each fill | Card: "Edge: $0.008 +/- $0.012 (p=0.14, need ~80 more trips)" |
| 2. Binomial asymmetry | Fill sides | p-value, true buy proportion CI | Each fill | Trend bar annotation: "p=0.003 -- significant trend detected" |
| 3. Censored bias | Price history + fills | True risk estimate, tail probability | Every 5 min | Card: "Hidden risk: 12% chance price exceeded grid in last hour" |
| 4. Poisson fill rate | Fill timestamps | Lambda CI, regime flag | Each fill | Card: "Regime: HIGH VOL (4.2/hr vs baseline 1.5/hr, p<0.01)" |
| 5. Chi-squared RW test | Level hit counts | p-value, market type | Daily | Card: "Market type: MEAN REVERTING (p=0.02) -- edge confirmed" |

All five analyzers use only data the bot already collects. They require no new API calls, no external libraries (scipy-equivalent math can be implemented in pure Python for the distributions needed), and no changes to the trading logic -- they are read-only analysis of existing state.

The key architectural property: the grid itself is the sampling instrument. Every fill is a structured probe of market microstructure at a known price level and time. The analyzers turn this passive data collection into active statistical inference about whether the strategy has an edge, whether market conditions have changed, and whether the current parameters are appropriate.
