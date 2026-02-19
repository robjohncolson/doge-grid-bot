# Grid Bot v2 — The Definitive Manifesto

**Author**: Robert (with synthesis from Claude Opus 4.6, after review of assessments from Claude Code, Grok, DeepSeek, and ChatGPT)
**Date**: 2026-02-18
**Status**: LOCKED — This is the build spec. No more deliberation.

---

## 0. The One-Sentence Pitch

**A grid trading bot you can see — where every order is a machine on a factory floor, the foreman reads a thermometer and watches the floor, and every dollar is ledgered.**

---

## 1. Locked Decisions

These are resolved. They don't get revisited.

| Decision | Resolution | Source |
|----------|-----------|--------|
| State machine | Unchanged. Pure reducer. The IP. | All agree |
| Sticky/cycle | Per-slot boolean, one state machine | All agree |
| Rangers/Churners/Herd | Dead. Deleted. Gone. | All agree |
| Factory viz | Primary interface. Boot screen. | Claude Code + Robert |
| Dashboard | Settings panel, accessible from factory | Robert |
| Ledger | Double-entry, reconciliation, audit trail | V2 spec |
| Persistence | Local-first (SQLite + JSONL). Supabase optional. | Consensus |
| Multi-pair | Killed. One pair, one factory. Run multiple instances. | Claude Code |
| Telegram | Repurposed: AI diagnostic console via DeepSeek. Not in the control loop — consulting doctor, not surgeon. | Robert |
| DCA accumulation | Killed from core. Separate tool if desired. | All agree |
| Sticky repricing | No auto-repricing. Sticky means wait forever. Manual intervention only. | Robert |
| Home screen | Factory first | Robert |
| Purpose | Personal tool first, distributable later | Robert |
| Rewrite vs refactor | Clean rewrite. Fresh repo. State machine ports unchanged. | Consensus (4 of 5) |

---

## 2. The Intelligence Layer — Resolved

This is where the four LLMs disagreed most. Here is the resolution.

### 2.1 The Principle

**The intelligence layer is not a brain that predicts the market. It is a thermometer on the factory wall and a set of instruments on the foreman's desk.**

Its job is to **tune operational parameters dynamically** — not to decide "should I buy DOGE" but to answer questions like:

- "How long should this recovery order's lottery window be before it becomes headroom sludge?"
- "Should the foreman be converting cycle slots to sticky right now?"
- "Is the market character about to change in a way that will jam one side of the floor?"

This is a **control systems** framing, not a quant framing. The intelligence layer feeds the governor. The governor operates the factory.

### 2.2 What Ships in Core

**Two-layer architecture:**

**Layer 1 — Factory Telemetry (feedback loop, always on):**
- Exit age distribution (median, p90, p95 per slot type and side)
- Distance-to-market for all open exits
- Orphan rate per hour (per slot type, per side)
- Write-off cost per hour
- Headroom utilization
- Fill-time distributions (rolling window)

This is the foreman looking at the factory floor. It runs every loop iteration. No external dependencies.

**Layer 2 — Market Scanner (feed-forward hint, configurable):**
- One HMM on 1-hour candles (retained from v1, simplified — single timeframe only)
- Simple indicators computed from local candles:
  - ATR(14) — volatility magnitude
  - ADX(14) — trend strength
  - Directional efficiency — `abs(net_change) / sum(abs(bar_changes))`
  - Bollinger Band width — volatility compression/expansion

The HMM classifies market character as `ranging | mild_trend | strong_trend`. The simple indicators provide cross-validation. Together they produce a single `MarketCharacter` struct:

```python
@dataclass(frozen=True)
class MarketCharacter:
    regime: str              # "ranging" | "mild_trend" | "strong_trend"
    trend_direction: str     # "up" | "down" | "none"
    volatility: float        # ATR-based, normalized
    confidence: float        # 0.0 - 1.0
    updated_at: float        # timestamp
```

**Hysteresis and stability**: Regime transitions require dwell time + cooldown (carried from v1's tier stability logic). The scanner does not flip-flop.

### 2.3 What Dies Forever

| Component | Why It Dies |
|-----------|------------|
| Multi-timeframe HMM (1m, 15m) | One timeframe is enough for market character. Consensus across three was solving a non-problem. |
| Bayesian belief state + posterior | Directional prediction tool. Grid doesn't need direction. |
| BOCPD change-point detection | Elegant but wrong problem. The foreman detects change-points by watching exit ages spike. |
| Cox proportional hazards survival model | Fill-time distributions are sufficient. A rolling median/p90 replaces the survival model. |
| 5 hypothesis tests | Statistical rigor for a $2 order. The factory diagnosis engine replaces this. |
| Signal digest traffic light | The factory IS the signal digest. Belt jams, brownouts, lane starvation — those are visual traffic lights. |
| Manifold Trading Score | Meta-score of meta-scores. Replaced by factory telemetry. |
| AI regime advisor (DeepSeek/Groq) | Paying an LLM to classify regime when HMM + ADX already does it. API cost often exceeds cycle profit. |
| Throughput sizer (renewal reward theory) | Replaced by simple sizing with governor-tuned parameters. |
| DCA accumulation engine | Different product. If you want to DCA, use a separate tool. |
| Inventory rebalancer | Already disabled in v1. Conflicts with directional regime. Dead. |

### 2.4 What Becomes Optional Plugins (v2.1+)

The v1 intelligence stack was good engineering. It just doesn't belong in the core loop. These can be wrapped as `GovernorPlugin` implementations for users who want to experiment:

```
plugins/
├── intelligence/
│   ├── hmm_multi_timeframe.py    # 3-timeframe HMM (for research)
│   ├── bayesian_engine.py        # Belief state (for research)
│   ├── bocpd.py                  # Change-point detection (for research)
│   ├── survival_model.py         # Fill-time survival (for research)
│   └── ai_advisor.py             # LLM regime advisor (for research)
```

These are NOT shipped in the default install. They exist in a `plugins/` directory for users who want to explore them.

### 2.5 How Intelligence Feeds the Governor

The governor consumes both layers and makes operational decisions:

```
Factory Telemetry ─┐
                   ├─→ Governor ─→ Operational Decisions
Market Scanner ────┘

Operational decisions:
  - Suggest sticky/cycle ratio adjustment
  - Set recovery order TTL for cycle slots (dynamic, based on market character)
  - Suggest write-off for stale exits
  - Pause new slot creation when headroom is tight
  - Tighten/loosen orphan timeouts for cycle slots
```

**Critical**: The governor never suppresses a side. It never blocks entries. It adjusts *how the factory operates*, not *what the factory trades*. The v1 mistake was letting the intelligence layer asymmetrically block buy entries (tier2 suppression), which created the USD leak. That pattern is dead.

---

## 3. The Governor — The Foreman, Not a Quant

### 3.1 Core Responsibilities

The governor answers these questions every cycle:

1. **Ratio**: How many slots should be sticky vs cycle?
2. **Recovery TTL**: How long should cycle slot recovery orders live before write-off?
3. **Auto-close**: Which exits are pathological and should be flagged?
4. **Capacity**: Can we add more slots or do we need to pause?

### 3.2 Ratio Governor (Reactive + Predictive)

**Reactive (from factory telemetry):**
Uses orphan pressure (recovery backlog / max recoveries) as the primary signal. This is the v2 spec's existing design — it works.

| Orphan Pressure | Action |
|-----------------|--------|
| 0.0 - 0.3 | Healthy. May suggest converting some sticky → cycle for faster capital recycling. |
| 0.3 - 0.6 | Moderate. Hold current ratio. |
| 0.6 - 0.8 | Elevated. Suggest converting some cycle → sticky. |
| 0.8 - 1.0 | Critical. Force-convert most-orphaned cycle slots to sticky (in autonomous mode). |

**Predictive (from market scanner):**
When the scanner detects a regime transition (e.g., ranging → trending), the governor adjusts the ratio target *before* the orphan backlog proves it:

| Market Character | Ratio Bias |
|-----------------|------------|
| Ranging | Favor cycle (faster capital recycling, exits fill quickly) |
| Mild trend | Hold current ratio |
| Strong trend | Favor sticky (exits on the against-trend side will get stuck; let them wait) |

The predictive signal is a *bias*, not a command. If the scanner says "trending" but orphan pressure is low, the governor doesn't force-convert. The factory telemetry is the boss; the scanner is the hint.

### 3.3 Dynamic Recovery TTL (The Key Innovation)

This is Robert's insight: the HMM and statistical models should **tune operational parameters** rather than predict direction.

For cycle slots, recovery orders get a TTL before write-off. That TTL is not a static config — it's dynamically computed:

```python
def compute_recovery_ttl(market_character, fill_time_stats, headroom):
    """
    Dynamic TTL for cycle slot recovery orders.
    
    In ranging markets with fast fills: short TTL (orders likely to fill quickly
    or not at all — don't waste headroom).
    
    In trending markets with slow fills: longer TTL (recovery orders from the
    with-trend side have a real chance of filling on a pullback).
    
    Under capacity pressure: shorter TTL regardless (headroom is more valuable
    than lottery tickets).
    """
    base_ttl = fill_time_stats.p50  # Median fill time as baseline
    
    if market_character.regime == "ranging":
        ttl = base_ttl * 0.5        # Quick turnover in ranging markets
    elif market_character.regime == "mild_trend":
        ttl = base_ttl * 1.0        # Standard
    elif market_character.regime == "strong_trend":
        ttl = base_ttl * 2.0        # Give recovery orders more time
    
    # Capacity pressure overrides
    if headroom < CAPACITY_CAUTION_HEADROOM:
        ttl = min(ttl, 300)          # 5 min max when headroom is tight
    
    return clamp(ttl, MIN_RECOVERY_TTL, MAX_RECOVERY_TTL)
```

This is where the HMM earns its keep. Not by predicting DOGE price, but by telling the foreman: "We're in a strong trend — give those recovery orders more time to catch a pullback."

Similarly, the governor can dynamically tune:
- **Orphan timeout** (`s1_orphan_after_sec`) for cycle slots — shorter in ranging (exits should fill fast or not at all), longer in mild trends
- **Profit target modulation** — nudge `profit_pct` down when exits are dragging, up when they're filling fast (ChatGPT's Lever #3)
- **Entry spacing bias** — slightly widen entries on the against-trend side in strong trends (low-risk, inherited from v1 tier1 concept)

### 3.4 Stuckness Score (Per-Slot Health)

Adapted from ChatGPT's suggestion. Each slot gets a health score:

```python
def compute_stuck_score(slot, fill_time_stats, market_price):
    if slot.state.derive_phase() == "S0":
        return 0.0  # Not stuck, waiting for entry
    
    exit_order = get_exit_order(slot)
    if exit_order is None:
        return 0.0
    
    age = now() - exit_order.placed_at
    distance = abs(exit_order.price - market_price) / market_price
    
    age_score = min(age / fill_time_stats.p90, 3.0)
    distance_score = min(distance / cfg.profit_pct, 3.0)
    
    return 0.6 * age_score + 0.4 * distance_score
```

The stuck score is:
- Visible in the factory (color gradient on machines)
- Used by the governor to prioritize which cycle slots to write off first
- Surfaced in the dashboard slot detail view
- **Not** used to auto-reprice sticky slots (sticky means wait forever — Robert's decision)

### 3.5 Governor Visibility

Everything the governor does is visible on the factory floor:

| Signal | Factory Visual | Dashboard Widget |
|--------|---------------|-----------------|
| Orphan pressure | Gauge in status bar | Percentage + bar |
| Market character | Background hue shift (teal=ranging, warm=trending) | Badge with regime label |
| Recovery TTL | Timer countdown on recovery order sprites | TTL column in slot table |
| Stuck score | Machine color gradient (green → amber → red) | Score column in slot table |
| Ratio suggestion | Toast notification | Suggestion card with approve/dismiss |
| Capacity band | Headroom bar with color bands | Detailed breakdown |

The foreman doesn't hide in a log file. Everything he thinks is painted on the factory wall.

---

## 4. Architecture

### 4.1 Module Map

```
grid-bot-v2/
├── state_machine.py      # Pure reducer (ported unchanged from v1)
├── ledger.py             # Double-entry accounting + audit trail
├── slot_engine.py        # SlotRuntime, per-slot sticky/cycle, work bank
├── governor.py           # Ratio control, recovery TTL, auto-close, stuckness
├── scanner.py            # HMM (1h) + simple indicators → MarketCharacter
├── diagnostic.py         # AI diagnostic engine (snapshot → DeepSeek → recommendations)
├── telegram.py           # Telegram bot: digest, on-demand queries, confirm/execute
├── capacity.py           # Order headroom tracking, bands
├── kraken_adapter.py     # Generic pair support, REST API
├── factory_view.py       # Factory canvas + Bauhaus overlay (primary UI)
├── dashboard.py          # Operational control surface (settings panel)
├── server.py             # HTTP server, API routes, SSE, commentator stream
├── audio.py              # Sound design (optional, off by default)
├── simulator.py          # --simulate mode (synthetic price, no API keys)
├── config.py             # Pair-agnostic, env-driven
├── main.py               # Entry point, main loop orchestration
├── logs/
│   ├── state.json        # Snapshot persistence
│   ├── ledger.jsonl      # Append-only ledger
│   └── ohlcv.db          # Local candle store (SQLite, ~2MB)
└── plugins/              # Optional, not shipped by default
    └── intelligence/     # v1's research stack, wrapped as GovernorPlugin
```

**Target**: ~10,000-14,000 lines total. External dependencies: `numpy` (HMM), `python-telegram-bot` (Telegram interface). Everything else is stdlib. DeepSeek API calls are plain `urllib`/`httpx`.

### 4.2 Data Flow

```
                    ┌──────────────┐
                    │   Kraken     │
                    │   REST API   │
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │   Adapter    │  price, fills, order status, OHLCV
                    └──────┬───────┘
                           │
         ┌─────────────────┼─────────────────┐
         │                 │                 │
  ┌──────▼──────┐   ┌─────▼──────┐   ┌─────▼──────┐
  │  Scanner    │   │ Capacity   │   │ Main Loop  │
  │ (HMM+ind.) │   │ Tracker    │   │ (30s poll) │
  │             │   │            │   │            │
  │ Market      │   │ headroom   │   │            │
  │ Character   │   │ bands      │   │            │
  └──────┬──────┘   └─────┬──────┘   └─────┬──────┘
         │                │                │
         └────────┬───────┘                │
                  │                        │
           ┌──────▼──────┐                 │
           │  Governor   │ ◄───────────────┘
           │  (foreman)  │   factory telemetry
           │             │
           │ ratio, TTL, │
           │ stuck score │
           └──────┬──────┘
                  │
           ┌──────▼──────┐
           │ Slot Engine │  N slots, each with:
           │             │  - PairState (state_machine.py)
           │             │  - sticky: bool
           │             │  - ledger position
           └──────┬──────┘
                  │
     ┌────────────┼────────────┬────────────┐
     │            │            │            │
┌────▼───┐ ┌─────▼────┐ ┌────▼─────┐ ┌────▼──────┐
│Ledger  │ │ Factory  │ │Dashboard │ │Diagnostic │
│(audit) │ │ View     │ │(settings)│ │(DeepSeek) │
│        │ │ +Bauhaus │ │          │ │           │
│        │ │ +Audio   │ │          │ │ snapshot  │
│        │ │ +Comment.│ │          │ │ → prompt  │
└────────┘ └──────────┘ └──────────┘ │ → advice  │
                                     └─────┬─────┘
                                           │
                                     ┌─────▼─────┐
                                     │ Telegram  │
                                     │ (digest,  │
                                     │  on-demand,│
                                     │  confirm)  │
                                     └───────────┘
```

### 4.3 Architecture Decision: Async

v2 uses `asyncio` for the main loop. This is a deliberate shift from v1's synchronous design, driven by three async consumers: the HTTP server (SSE streams), the Telegram bot (long-polling), and potential future WebSocket support for the factory view. The Kraken adapter uses `httpx.AsyncClient`. The state machine remains a pure synchronous function — `transition()` is called with `await`-free code inside the async loop.

### 4.4 State Machine Knobs (Already Exist)

Claude Code confirmed that the governor's tuning knobs require **zero state machine changes**:

- **Entry spacing bias**: `EngineConfig.entry_pct_a` / `entry_pct_b` already exist. `_entry_pct_for_trade()` routes per-side. Governor just sets different values.
- **Profit target modulation**: `PairState.profit_pct_runtime` already exists. `_exit_price()` uses `st.profit_pct_runtime` or `cfg.profit_pct`. Governor sets this on the PairState.

The "state machine unchanged" constraint is mechanically verified, not just aspirational.

### 4.5 Main Loop (Simplified from v1's 34 steps)

```python
async def run_loop_once():
    # 1. Price + candles
    price = await adapter.fetch_price()
    candles = await adapter.fetch_ohlcv()  # Rolling buffer, local store
    
    # 2. Scanner update (every N minutes, not every loop)
    if scanner.should_update(now):
        market_character = scanner.update(candles)
    
    # 3. Capacity check
    capacity = capacity_tracker.update(await adapter.open_orders_count())
    
    # 4. Governor decisions
    telemetry = compute_factory_telemetry(slots)
    governor_actions = governor.evaluate(
        slots, telemetry, market_character, capacity
    )
    
    # 5. Per-slot transitions
    for slot in slots.values():
        cfg = build_slot_config(slot, governor_actions)
        state, actions = transition(slot.state, PriceTick(price, now), cfg, order_size)
        state, actions2 = transition(state, TimerTick(now), cfg, order_size)
        execute_actions(slot, actions + actions2)
    
    # 6. Fill detection + reconciliation
    await poll_fills()
    # Reconcile every 5th cycle (2.5 min) or on fill event, not every 30s
    # Saves ~2,500 private API calls/day
    if fill_occurred or loop_count % 5 == 0:
        ledger.reconcile(slots, await adapter.balances())
    
    # 7. Governor post-tick (apply ratio changes, TTL enforcement)
    governor.post_tick(slots, capacity)
    
    # 8. Persist
    save_snapshot()
    
    # 9. Broadcast (SSE → factory view, commentator, audio hooks)
    broadcast_status(slots, telemetry, market_character, governor_actions)
```

That's 9 steps. v1 had 34.

---

## 5. New Features (Prioritized)

### 5.1 Simulation Mode (Priority 1)

```bash
python main.py --simulate --volatility=1.2 --hours=24 --speed=10x
```

- Generates synthetic price data (random walk with configurable volatility and optional trend injection)
- Runs the full state machine, ledger, governor, and scanner against it
- Renders the factory in real-time at configurable speed (1x, 5x, 10x, 100x)
- Shows what would have happened with your config
- No API keys. No exchange account. No real money.

**Use cases:**
- New user onboarding: "Watch a factory spin up and make money in 30 seconds"
- Config testing: "What happens with 50 sticky / 10 cycle at 0.3% entry?"
- Governor tuning: "Does the dynamic recovery TTL actually reduce stuck exits?"
- Regression testing: deterministic seed for reproducible runs

The simulator replaces the need for dry-run mode against a real exchange. It's faster, safer, and more instructive.

### 5.2 Sound Design (Priority 2)

Optional, off by default. Enabled with `AUDIO_ENABLED=True` or `:audio on` command.

| Event | Sound | Why |
|-------|-------|-----|
| Cycle complete | Cash register ka-ching | Satisfying positive reinforcement |
| Entry fill | Soft click | Acknowledgment |
| Orphan created | Low thunk | Something got stuck |
| S2 entered | Warning tone | Both sides active, tension |
| S2 resolved | Relief chord | Back to normal |
| Write-off | Glass break | Loss accepted |
| Recovery fill | Slot machine jackpot | Lottery ticket paid off |
| Governor ratio change | Foreman whistle | Operational change |

Implementation: Web Audio API in the factory view. Sounds are procedurally generated (no audio files to ship). Each sound is a few lines of oscillator + envelope code.

### 5.3 Commentator Ticker (Priority 3)

SSE stream that narrates bot activity in plain English. Appears as a scrolling ticker at the bottom of the factory view.

```
[12:03:22] Slot #3 entry filled: bought 556 DOGE at $0.09012 (Trade B, cycle 7)
[12:03:22] Exit placed: sell 556 DOGE at $0.09102 (target: +$0.50 profit)
[12:04:51] Slot #7 exit getting stale (23 min, median is 15 min) — stuck score: 1.4
[12:05:22] Foreman: "Market trending — extending recovery TTL to 8 min"
[12:06:01] Slot #3 exit filled! Profit: $0.47 after fees. Cycle 7 complete. 🎉
[12:06:01] Fresh entry placed: buy 560 DOGE at $0.08994
[12:10:15] Foreman: "Orphan pressure at 0.65 — suggesting convert 2 cycle → sticky"
```

This is how people learn the system. They don't read a 1,400-line spec — they watch the bot explain itself. Teacher-brain at work.

### 5.4 Art Modes (Priority 4 — Future)

The Bauhaus overlay proves the architecture: same data, different skin. Future art modes are cosmetic skins that consume the same `/api/status` object:

| Mode | Aesthetic | When |
|------|-----------|------|
| Factory | Factorio machines + conveyors | Ships in v2.0 |
| Bauhaus | Kandinsky/Klee membrane + sprites | Ships in v2.0 (existing) |
| Mondrian | Colored rectangles proportional to profit | v2.1 |
| Zen | Black on white, just price line + glowing dots | v2.1 |
| Matrix | Falling green text, orders as characters | v2.2 (fun project) |

Each mode is a single JS render function. Hotkey `b` already toggles Factory↔Bauhaus. Extending to `1-5` for mode selection is trivial.

### 5.5 AI Diagnostic Console (via Telegram + DeepSeek)

The AI is not in the control loop. It is a **consulting doctor** — it does morning rounds (daily digest), is on-call (on-demand via Telegram), and can write prescriptions that require your signature (suggest + confirm).

#### Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  Bot State   │     │  Diagnostic  │     │   DeepSeek   │
│  (snapshot)  │────▶│   Engine     │────▶│   API        │
│              │     │              │     │              │
│ - all slots  │     │ builds prompt│     │ returns      │
│ - telemetry  │     │ from snapshot│     │ analysis +   │
│ - governor   │     │ + prompt     │     │ actionable   │
│ - scanner    │     │ templates    │     │ suggestions  │
│ - ledger     │     │              │     │              │
└──────────────┘     └──────┬───────┘     └──────────────┘
                            │
                     ┌──────▼───────┐
                     │  Telegram    │
                     │  Bot         │
                     │              │
                     │ - digest msg │
                     │ - on-demand  │
                     │ - [Approve]  │
                     │   buttons    │
                     └──────────────┘
```

#### The Snapshot

The diagnostic engine serializes the full system state into a structured prompt context:

```python
def build_diagnostic_snapshot():
    return {
        "timestamp": now(),
        "pair": config.PAIR,
        "price": current_price,
        "market_character": scanner.current,  # regime, direction, volatility
        
        # Factory floor
        "slots": {
            "total": len(slots),
            "sticky": count_sticky,
            "cycle": count_cycle,
            "by_phase": {"S0": n, "S1a": n, "S1b": n, "S2": n},
        },
        
        # Health
        "stuck_scores": {sid: score for sid, score in top_stuck},
        "orphan_pressure": governor.orphan_pressure,
        "headroom": capacity.headroom,
        "capacity_band": capacity.band,
        
        # Performance (last 24h)
        "cycles_completed_24h": n,
        "profit_24h_usd": x,
        "write_offs_24h_usd": y,
        "fees_24h_usd": z,
        "avg_fill_time_min": t,
        
        # Governor state
        "governor": {
            "current_ratio_target": "70/30 sticky/cycle",
            "recovery_ttl_sec": current_ttl,
            "recent_actions": [...],
        },
        
        # Ledger summary
        "total_profit_usd": ledger.total_profit,
        "total_fees_usd": ledger.total_fees,
        "total_write_offs_usd": ledger.total_write_offs,
        "reconciliation_drift_pct": ledger.drift,
    }
```

#### Prompt Templates

The diagnostic engine maintains a library of prompt templates, each tuned for a different query type:

| Template | Trigger | What It Asks DeepSeek |
|----------|---------|----------------------|
| `daily_digest` | Scheduled (configurable, e.g., 8am) | "Here's the factory state. Summarize health, flag concerns, suggest adjustments." |
| `health_check` | User sends `/check` | "Is everything working correctly? Any anomalies?" |
| `stuck_analysis` | User sends `/stuck` | "These exits are stuck. Why? What should I do about each one?" |
| `ratio_review` | User sends `/ratio` | "Is my sticky/cycle ratio appropriate for current market conditions?" |
| `performance` | User sends `/perf` | "How am I doing? Breakdown of profit sources, cost centers, efficiency." |
| `what_if` | User sends `/whatif <scenario>` | "What would happen if I changed X to Y?" |
| `explain` | User sends `/explain` | "Explain what the bot is doing right now in plain English." |

Every prompt includes:
1. **System context**: "You are analyzing a grid trading bot. Here is the system architecture in brief..." (fixed, ~200 tokens)
2. **Snapshot**: The serialized state above (~500 tokens)
3. **Template-specific question**: The actual query (~100 tokens)
4. **Response format**: "Respond with: 1) Assessment (2-3 sentences), 2) Concerns (bulleted, if any), 3) Suggested actions (each as a concrete parameter change)"

#### Suggest + Confirm Flow

When DeepSeek suggests an actionable change, the Telegram bot presents it as an inline button:

```
🏭 Daily Factory Digest — Feb 18, 2026

Health: ✅ Good
Cycles (24h): 47 completed, $4.82 profit
Write-offs: 3 ($0.27 total)
Market: Ranging (HMM confidence: 0.78)

⚠️ Observation: Orphan pressure at 0.52 and rising. 
8 cycle slots are producing orphans faster than 
they're resolving. Market is ranging, so exits 
should be filling quickly — the issue may be that 
profit_pct (1.0%) is too wide for current volatility 
(ATR suggests 0.6% swings).

Suggested actions:
1. Convert 3 cycle slots → sticky (slots 4, 7, 11 — 
   highest stuck scores)
2. Narrow profit_pct from 1.0% → 0.8% for cycle slots

[✅ Approve #1] [✅ Approve #2] [❌ Dismiss All]
```

When the user taps `[✅ Approve #1]`:
1. Telegram bot calls `POST /api/action {action: "toggle_sticky", slot_id: 4}`
2. Repeats for slots 7 and 11
3. Confirms: "✅ Done. Slots 4, 7, 11 converted to sticky."

When the user taps `[✅ Approve #2]`:
1. Telegram bot calls `POST /api/config {profit_pct_cycle: 0.8}`
2. Confirms: "✅ Done. Cycle slot profit target set to 0.8%."

**The AI never executes without user approval.** Every action goes through the confirm flow. The bot's API is the only execution path — the AI doesn't have direct access to the state machine or Kraken.

#### Scope of AI Recommendations

The AI can recommend changes to **operator-controlled parameters only**:

| Can Recommend | Cannot Recommend |
|--------------|-----------------|
| Sticky/cycle toggle per slot | Entry/exit price placement |
| Number of slots (add/remove) | Side suppression (banned) |
| profit_pct adjustment | Direct Kraken orders |
| entry_pct adjustment | State machine modifications |
| Governor mode (suggest/autonomous) | Ledger corrections |
| Pause/resume specific slots | Anything that bypasses the reducer |
| Write-off suggestion for specific sticky exits | |

#### Cost Management

DeepSeek is cheap, but the bot should still be frugal:

- Daily digest: 1 API call/day (~$0.001)
- On-demand queries: rate-limited to 10/hour
- Snapshot is pre-serialized and cached (rebuilt every main loop, not per query)
- Total estimated cost: ~$0.05-0.15/day with active usage

#### Degradation

If DeepSeek API is unreachable, Telegram key is missing, or the feature is disabled:
- No digest sent
- On-demand queries return "Diagnostic unavailable — check DEEPSEEK_API_KEY"
- Zero impact on trading loop. The factory keeps running. The foreman keeps working.
- The AI is a luxury, not a dependency.

#### Configuration

```python
# === AI DIAGNOSTIC ===
DIAGNOSTIC_ENABLED = False             # Master switch (opt-in)
DEEPSEEK_API_KEY = ""                  # Required if enabled
DEEPSEEK_MODEL = "deepseek-chat"       # Model selection
DIAGNOSTIC_DAILY_DIGEST = True         # Send daily digest
DIAGNOSTIC_DIGEST_HOUR_UTC = 13        # 8am EST
DIAGNOSTIC_MAX_QUERIES_PER_HOUR = 10   # Rate limit
DIAGNOSTIC_CONFIRM_REQUIRED = True     # Always require user approval

# === TELEGRAM ===
TELEGRAM_ENABLED = False               # Master switch (opt-in)
TELEGRAM_BOT_TOKEN = ""                # Required if enabled
TELEGRAM_CHAT_ID = ""                  # Authorized chat only
```

---

## 6. What Ships (Component Budget)

| # | Component | Est. Lines | Description |
|---|-----------|-----------|-------------|
| 1 | `state_machine.py` | ~1,300 | Unchanged from v1. The engine. |
| 2 | `ledger.py` | ~800 | Double-entry accounting, reconciliation, audit trail |
| 3 | `slot_engine.py` + `capacity.py` | ~1,200 | SlotRuntime, work bank, per-slot toggle, headroom |
| 4 | `governor.py` | ~800 | Ratio control, dynamic recovery TTL, stuckness score, auto-close |
| 5 | `scanner.py` | ~600 | 1h HMM + simple indicators → MarketCharacter |
| 6 | `diagnostic.py` | ~500 | Snapshot builder, prompt templates, DeepSeek API client |
| 7 | `telegram.py` | ~400 | Telegram bot: commands, digest scheduler, confirm/execute flow |
| 8 | `factory_view.py` + Bauhaus | ~5,000 | The killer feature (mostly existing, cleaned) |
| 9 | `dashboard.py` | ~1,500 | Settings panel, stripped of v1 cruft |
| 10 | `server.py` + `config.py` + `main.py` | ~1,500 | HTTP, API, SSE, commentator, config |
| 11 | `simulator.py` | ~500 | Synthetic price + full engine at speed |
| 12 | `audio.py` | ~200 | Web Audio procedural sounds |
| **Total** | | **~14,300** | Clean. Down from 51K. |

---

## 7. Build Plan

### Phase 0: Scaffold (Day 1)

- New repo `grid-bot-v2`
- Copy `state_machine.py` unchanged (frozen, do not touch)
- Write `config.py` (pair-agnostic, `.env.example` with empty values)
- Write `capacity.py` (order headroom math)
- Stub every other module with docstrings and interfaces
- First test: state machine transitions pass with new config object

### Phase 1: Core That Trades (Days 2-5)

- `ledger.py` — LedgerEntry, SlotAccount, reconciliation, persistence to JSONL
- `slot_engine.py` — SlotRuntime, work bank math, per-slot sticky/cycle toggle
- `kraken_adapter.py` — extract from v1's `kraken_client.py`, make pair-agnostic
- `main.py` — main loop (price → transitions → fills → actions → save)
- Test: 2 slots (1 sticky, 1 cycle) trading live on Kraken with ledger recording

### Phase 2: Factory First (Days 6-10)

> **Scope note**: The factory JS is not a "cleanup" — v1's data bindings reference rangers, churners, self-healing, and a v1-shaped status payload. Every render binding needs rewriting for v2's payload shape. This is closer to a 60% JS rewrite. Budget accordingly.

- Port `factory_view.py` — strip DOGE references, add slot-type badges [S]/[C]
- **Rewrite all JS data bindings** to consume v2 status payload (no rangers, no churners, governor shape, capacity shape)
- Port Bauhaus overlay — same cleanup
- `server.py` — HTTP server, `/api/status`, SSE stream
- Boot sequence: `python main.py` → opens browser to `/factory`
- Dashboard as `/dashboard` route (stripped, settings-panel framing)
- Commentator ticker at bottom of factory view
- Test: watch 2 slots run in factory view, read commentator, toggle Bauhaus

### Phase 3: Simulation Mode (Days 11-14)

> **Why before governor**: The simulator provides synthetic candle data, which the scanner needs for testing. Building it first solves the cold-start problem — the 1h HMM needs ~500 candles (21 days) to train. The simulator can generate that in seconds.

- `simulator.py`:
  - Synthetic price generator (random walk + configurable volatility + optional trend injection)
  - **Mock order book** (~100-150 lines): holds limit orders, checks if synthetic price crosses them, generates FillEvent. No partial fills (simplicity). Fees at configured maker rate.
  - Adapter mock that feeds synthetic price into main loop
  - **Synthetic OHLCV generator**: produces 1h candles from the price walk for scanner consumption
  - Speed control (1x, 5x, 10x, 100x)
  - Deterministic seed for reproducible runs
- CLI: `python main.py --simulate --volatility=1.2 --hours=24 --speed=10x`
- Factory renders simulation in real-time at chosen speed
- Test: run simulation, watch factory, verify fill mechanics and ledger accounting

### Phase 4: Governor + Scanner (Days 15-18)

- `scanner.py` — port 1h HMM from v1 (simplified: single timeframe, no consensus)
  - Add simple indicators (ATR, ADX, directional efficiency, BB width)
  - Local candle store: SQLite `logs/ohlcv.db`, rolling 500 bars
  - Output: `MarketCharacter` struct
- `governor.py`:
  - Orphan pressure → ratio suggestions (reactive)
  - Market character → ratio bias (predictive)
  - Dynamic recovery TTL computation + **enforcement plumbing**: each cycle, iterate recovery orders on cycle slots; if `now - recovery.orphaned_at > compute_recovery_ttl(...)`, cancel on Kraken, feed `RecoveryCancelEvent` back through `transition()` (state machine already handles this event)
  - Stuckness score per slot
  - Factory integration (hue shift, gauge, toasts)
  - **Sticky write-off escape hatch**: Dashboard "Write Off" button per sticky slot → `POST /api/action {action: "write_off", slot_id: N}` → cancel exit on Kraken, close position in ledger with `close_reason="operator_writeoff"`, slot returns to S0
- Test: **use simulator with trend injection** to verify governor ratio suggestions, TTL changes, and recovery expiry across regime transitions

### Phase 5: Sound + Polish (Days 19-21)

- `audio.py` — Web Audio procedural sounds for all events
- Sound toggle in factory view (`:audio on/off` or hotkey)
- Keyboard navigation (VIM-style, from v1 dashboard UX spec)
- `README.md` with screenshots/GIFs
- `.env.example` — clean, no personal data
- `Dockerfile` — verify, clean
- Final code review: zero DOGE-specific references outside config, zero personal data

### Phase 6: AI Diagnostic Console (Days 22-25)

> **Scope note**: Two external service integrations (DeepSeek API + Telegram Bot API) plus prompt engineering. Budget 4 days, not 3.

- `diagnostic.py`:
  - Snapshot builder (serializes full system state for LLM context)
  - Prompt template library (digest, health_check, stuck, ratio, performance, whatif, explain)
  - DeepSeek API client (plain httpx, no SDK dependency)
  - Response parser: extracts assessment, concerns, and actionable suggestions
  - Action mapper: converts AI suggestions into valid `/api/action` payloads
- `telegram.py`:
  - Bot setup with `python-telegram-bot` (async, handles polling + retries)
  - Command handlers: `/check`, `/stuck`, `/ratio`, `/perf`, `/whatif`, `/explain`
  - Scheduled daily digest (configurable hour)
  - Inline keyboard buttons for suggest + confirm flow
  - Action execution: calls bot's own HTTP API on user approval
  - Rate limiting (10 queries/hour)
  - Auth: single authorized `TELEGRAM_CHAT_ID` only
- Test: send `/check` via Telegram, receive analysis, approve a suggestion, verify execution

### Phase 7: Migration + Cutover (Days 26-28)

- `migrate_v1_state.py` — one-time script that reads v1 `logs/state.json` and converts to v2 format
- Parallel run: v2 in dry-run alongside v1 for 48h, compare decisions
- Cutover: stop v1, start v2 with real orders
- Tag v1 as `v1.0.0`, archive

**Total: ~28 focused days.**

---

## 8. Open Questions — Now Resolved

| # | Question (from v2 spec) | Resolution |
|---|------------------------|------------|
| 1 | Auto-slots? | Yes. Profit-funded scaling with `SLOT_PROFIT_THRESHOLD`. Governor controls whether new slots are sticky or cycle based on current ratio target. |
| 2 | Recovery orders for cycle slots? | **Dynamic TTL.** Governor computes TTL based on market character, fill-time stats, and headroom. Not a static config — the HMM earns its keep here. |
| 3 | Sticky slot repricing? | **No.** Sticky means wait forever. Manual intervention only. The foreman can *suggest* a write-off via dashboard, but never auto-reprices. |
| 4 | Plugin API surface? | Minimal. `GovernorPlugin.recommend_ratio()` and `GovernorPlugin.recommend_close()`. Governor hooks only. Main loop is not hookable. |

---

## 9. What the Intelligence Layer Earns

To be explicit about where the HMM and indicators provide concrete value (and nowhere else):

| Parameter | Without Scanner | With Scanner |
|-----------|----------------|--------------|
| Recovery TTL | Static config (e.g., 5 min) | Dynamic: 2 min in ranging, 10 min in strong trend |
| Orphan timeout | Static `S1_ORPHAN_AFTER_SEC` | Governor-tuned: shorter in ranging (fast churn), longer in mild trend |
| Sticky/cycle ratio | Reactive only (orphan pressure) | Predictive bias before orphans accumulate |
| Entry spacing | Fixed `entry_pct` for both sides | Slight widening on against-trend side in strong trends |
| Profit target | Fixed `profit_pct` | Optional nudge: down when exits drag, up when they fill fast |

If the scanner is disabled (or in simulation without candle history), every parameter falls back to static config. The bot still works. It just reacts slower.

---

## 10. Implementation Notes (from Claude Code Review)

These are resolved design details to address during each build phase. None require reopening locked decisions.

### 10.1 Recovery TTL Enforcement (Phase 4)

The state machine creates recovery orders via `_orphan_exit()` and stores `orphaned_at` on each `RecoveryOrder`. But the state machine doesn't enforce TTL — it has no timer-based recovery expiry. The governor handles this:

```python
# In governor.post_tick(), every cycle:
for slot in cycle_slots:
    for recovery in slot.state.recovery_orders:
        ttl = compute_recovery_ttl(market_character, fill_stats, headroom)
        if now - recovery.orphaned_at > ttl:
            await adapter.cancel_order(recovery.txid)
            state, actions = transition(slot.state, RecoveryCancelEvent(recovery.local_id), cfg, size)
            # State machine already handles RecoveryCancelEvent (removes recovery record)
```

~30 lines of plumbing, architecturally important.

### 10.2 Reconciliation Rate-Limiting (Phase 1)

Balance check is a private API call (1 rate-limit counter). At every-30s polling, that's 2,880 calls/day. Reconcile every 5th cycle (2.5 min) or on fill event. Already reflected in the main loop pseudocode (§4.5).

### 10.3 Scanner Cold Start (Phase 4)

1h HMM with 500 candles = 21 days before full training. During cold start, governor operates on factory telemetry only (reactive). The simulator (built in Phase 3) can generate synthetic candle history for testing. For live deployment, the scanner gracefully degrades: `MarketCharacter.confidence = 0.0` until sufficient candles accumulate, and the governor ignores low-confidence scanner output.

### 10.4 Sticky Write-Off Escape Hatch (Phase 4)

The operator's manual intervention path for stuck sticky exits:

- **Dashboard**: "Write Off" button per sticky slot (visible only in S1 with stale exit)
- **API**: `POST /api/action {action: "write_off", slot_id: N}`
- **Runtime**: cancel exit on Kraken → close position in ledger with `close_reason="operator_writeoff"` → feed through state machine orphan flow → slot returns to S0 → fresh entry placed
- **Factory**: visual indicator "operator wrote off slot #7" + glass-break sound if audio enabled
- **Telegram**: AI diagnostic can *suggest* write-offs, operator confirms via button

### 10.5 Simulation Fill Engine (Phase 3)

The simulator needs a mock order book to convert `PlaceOrderAction` into `FillEvent`:

- Hold limit orders in a dict keyed by local_id
- Each price tick: check if synthetic price crosses any open limit order
- If crossed: generate `FillEvent` with exact volume, synthetic txid, fee at configured maker rate
- No partial fills (simplicity — revisit if needed)
- Fill timing: fills arrive on the same tick the price crosses (instantaneous for simulation)
- ~100-150 lines

### 10.6 Degradation Guarantees

The bot runs identically with `DIAGNOSTIC_ENABLED=False` and `TELEGRAM_ENABLED=False`. These are conveniences, not requirements. The core trading loop (state machine → governor → slot engine → ledger) has zero dependency on DeepSeek, Telegram, or any external service beyond Kraken itself.

---

## 11. Non-Negotiable Constraints

1. **Reducer purity**: `state_machine.py` has zero side effects. All exchange I/O happens in runtime after reducer returns. This is inviolable.
2. **The governor never suppresses a side.** No asymmetric blocking of buy or sell entries. The v1 USD leak was caused by this pattern. It is banned.
3. **Every dollar is ledgered.** No floating-point accumulators. The ledger is the source of truth.
4. **Factory is the primary interface.** The dashboard is a settings panel. You operate the bot by watching the factory.
5. **Sticky means sticky.** No auto-repricing. No auto-write-off. The operator decides when to intervene. The governor can *suggest*, never force.
6. **Local-first persistence.** No external dependencies for basic operation. Supabase is optional for users who want cloud sync.
7. **Pair-agnostic.** No DOGE-specific code outside of config defaults. The bot works with any Kraken pair.
8. **Zero personal data.** No API keys, no usernames, no personal references in the distributed codebase.
9. **AI is never in the loop.** The diagnostic console reads snapshots and talks to the operator. It never touches the state machine, the governor, or Kraken directly. Every AI-suggested action requires explicit user confirmation via Telegram button before execution. The AI is a consulting doctor, not a surgeon.

---

## 12. The Elevator Pitch (Updated)

A grid trading bot for Kraken that you can actually see. Two order modes — sticky (patient) and cycle (aggressive) — managed per-slot by a factory foreman who watches the floor and reads a thermometer. The thermometer is a lightweight market scanner that tells the foreman "the market is trending" so he can adjust recovery timeouts, ratio targets, and entry spacing before the orphan backlog proves it. Every dollar is ledgered. Every order is a machine on the factory floor. When things are good, the factory hums. When things break, you see it break. And when you want a second opinion, you text the factory's consulting doctor on Telegram — he reads the full chart, explains what's happening in plain English, and suggests what to change. You approve with a button tap, or dismiss and move on. No black boxes. No PhD required. Just a factory making money from price oscillation, with a foreman smart enough to tighten the bolts before they rattle loose, and a doctor on call when you want to ask "is this thing working right?"

---

*This document supersedes V2_CLEAN_SLATE_SPEC.md, which remains valid for technical details (ledger schema, work bank math, slot lifecycle) but is overridden on all architectural and intelligence-layer decisions.*