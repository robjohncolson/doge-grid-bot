# STATE_MACHINE.md Update Spec

**Version**: 0.1.0
**Date**: 2026-02-17
**Purpose**: Bring `STATE_MACHINE.md` (rev 3, 1014 lines) into parity with the current runtime (~46K lines across 23 modules).

## Background

`STATE_MACHINE.md` was last substantively updated on 2026-02-15 (commit `ab915fa`), with a minor addition in `ac07263` (Bayesian Intelligence, Feb 17 — added 3 lines about dual realized trackers). Since then, **~90 commits** have landed adding major subsystems. The document currently covers §1–§26 but has significant gaps, stale references, and missing sections.

## Audit: Current STATE_MACHINE.md vs Reality

### Stale / Incorrect Sections

| Section | Issue |
|---------|-------|
| **§9 Order Sizing** | Documents Kelly Criterion (`kelly_sizer.py`) as the sizing layer. **Kelly was replaced by Throughput Sizer** (`throughput_sizer.py`) in commit `aa9d203` (Feb 16). The entire §9.1 Kelly Config table is dead. |
| **§9 B-side sizing** | Mentions "dust dividend" but doesn't document the **account-aware B-side sizing** model (`available_usd / slot_count`) that replaced per-slot compounding (commit `62a5cb2`). |
| **§1 Scope** | Lists `state_machine.py` as primary code reference but doesn't list new modules: `throughput_sizer.py`, `bayesian_engine.py`, `survival_model.py`, `bocpd.py`, `position_ledger.py`, `ai_advisor.py`. |
| **§4 Main Loop** | Missing steps for: self-healing slot checks, Bayesian engine updates, AI regime advisor scheduling, DCA accumulation ticks, 1h tertiary OHLCV sync, balance intelligence checks. |
| **§11 Recovery/Orphan** | Doesn't mention `RECOVERY_ORDERS_ENABLED=False` deprecation gate or startup stale-recovery cancellation. |
| **§16 OHLCV Pipeline** | `HMM_TRAINING_CANDLES` default listed as 720 — actual is now **4000** (deep training). `HMM_SECONDARY_TRAINING_CANDLES` listed as 720, actual is **1440**. |
| **§17 HMM Regime** | Missing tertiary (1h) timeframe entirely. Missing deep training quality tiers. Missing confidence modifier from quality tiers. |
| **§22 Persistence** | Missing snapshot fields for: throughput sizer state, Bayesian engine state, self-healing state, accumulation engine state, balance intelligence state, position ledger state, tertiary HMM state, AI regime advisor state. |
| **§23 Dashboard/API** | Missing status payload blocks for: throughput sizer, Bayesian intelligence, self-healing, accumulation engine, balance intelligence, AI advisor, tertiary HMM, Manifold score. |
| **§23 Actions** | Missing new dashboard actions: self-healing controls, AI override accept/reject, accumulation start/stop, manual DCA triggers. |
| **§24 Telegram** | Missing new commands for: self-healing, DCA, AI regime override. |

### Missing Sections (New Subsystems — All Implemented in Code)

| New Section Needed | Module(s) | Commits | Lines Added |
|--------------------|-----------|---------|-------------|
| **Throughput Sizer** (replaces §9.1 Kelly) | `throughput_sizer.py` (718 lines) | `aa9d203` | ~1700 |
| **HMM Deep Training Window** | `hmm_regime_detector.py`, `bot.py` | `88ca979` | ~1500 |
| **AI Regime Advisor** | `ai_advisor.py` (1572 lines) | `88ca979`, `c9429e9`, `395a68b`, `8467ea6`, `11a2c04` | ~2400 |
| **1h HMM (Tertiary Timeframe)** | `bot.py`, `hmm_regime_detector.py` | `30d7e60` | Part of strategic capital |
| **DCA Accumulation Engine** | `bot.py` | `30d7e60` | Part of strategic capital |
| **Recovery Order Deprecation** | `bot.py`, `config.py` | `30d7e60` | Part of strategic capital |
| **Durable Profit Settlement** | `grid_strategy.py`, `bot.py` | `b4738f7` | ~590 |
| **Account-Aware B-Side Sizing** | `bot.py` | `62a5cb2` | ~70 |
| **USD Dust Sweep** | `bot.py`, `config.py` | `148be57` | ~740 |
| **Balance Intelligence** | `bot.py`, `kraken_client.py` | `b80a104` | ~1800 |
| **Self-Healing Slots** | `bot.py`, `position_ledger.py` (574 lines), `dashboard.py` | `e6d015f`, `5e948ff` | ~4600 |
| **Bayesian Intelligence** | `bayesian_engine.py` (753), `bocpd.py` (307), `survival_model.py` (739) | `ac07263` | ~6400 |
| **Manifold Score / Ops Panel / Churner** | `bot.py`, `bayesian_engine.py`, `position_ledger.py`, `config.py`, `dashboard.py` | `ac07263`, `e6d015f` | Part of Bayesian + self-healing |

### Sections That Are Fine (No Changes Needed)

| Section | Status |
|---------|--------|
| §2 System Overview | OK (minor module list expansion needed) |
| §3 Top-Level Lifecycle | OK (add new init steps) |
| §5 Pair Phases | OK — core S0/S1/S2 unchanged |
| §6 Reducer Contract | OK — reducer itself is still pure |
| §7 Event Transition Rules | OK (minor: add settlement tracking to exit fill) |
| §8 Bootstrap and Reseed | OK |
| §10 Invariants and Halt | OK |
| §13 Daily Loss Lock | OK |
| §14 Inventory Rebalancer | OK (still disabled, still documented) |
| §15 Dynamic Idle Target | OK |
| §19 Capital Layers | OK |
| §20 Slot Aliases | OK |
| §21 Reconciliation | OK |
| §25 Operational Guardrails | OK |

## Specification: What to Change

### Phase 1: Fix Stale Content

#### 1a. Replace Kelly with Throughput Sizer (§9)

Replace the entire Kelly subsection (§9, lines 261–310) with Throughput Sizer documentation:

- Core signal: `profit / time_locked` per regime (renewal reward theory)
- 6 buckets: bearish/ranging/bullish × trade A/trade B + aggregate fallback
- Censored observations: open exits included at 0.5 weight (Kaplan-Meier lite)
- Age pressure: p90 percentile for exits
- Capital utilization penalty: throttles when >70% capital locked
- Config prefix: `TP_*` (replaces `KELLY_*`)
- Integration: 4 swap points (constructor, update, sizing, status payload)
- Config table: `TP_ENABLED`, `TP_MIN_SAMPLES`, `TP_LOOKBACK`, `TP_FLOOR_MULT`, `TP_CEILING_MULT`, `TP_AGE_PRESSURE_PERCENTILE`, `TP_CAPITAL_UTILIZATION_THRESHOLD`, `TP_CAPITAL_UTILIZATION_PENALTY`

Source of truth: `docs/THROUGHPUT_SIZER_SPEC.md`, `throughput_sizer.py`

#### 1b. Update B-Side Sizing (§9)

Replace the vague "dust dividend" mention with account-aware sizing:

- B-side order size = `available_usd / active_buy_slot_count`, floored by `ORDER_SIZE_USD`
- Replaces per-slot profit compounding for B-side
- A-side still uses per-slot compounding (`ORDER_SIZE_USD + total_profit`)
- USD dust sweep (when enabled): redistributes free-USD surplus across buy-ready slots each loop

Source: `docs/ACCOUNT_AWARE_B_SIDE_SIZING_SPEC.md`, `docs/USD_DUST_SWEEP_SPEC.md`

#### 1c. Update HMM Training Defaults (§16, §17)

- `HMM_TRAINING_CANDLES`: 720 → **4000**
- `HMM_SECONDARY_TRAINING_CANDLES`: 720 → **1440**
- Add quality tier system: Shallow (<1000, ×0.70), Baseline (1000-2499, ×0.85), Deep (2500-3999, ×0.95), Full (4000+, ×1.00)
- Confidence modifier applied in `_update_regime_tier()` AFTER `_policy_hmm_signal()`
- Dashboard progress bar with color-coded tier + ETA

Source: `docs/HMM_DEEP_TRAINING_SPEC.md`

#### 1d. Update Recovery/Orphan (§11)

- Add `RECOVERY_ORDERS_ENABLED` feature flag (default `False`)
- When disabled: orphan timers → infinity (exits never orphaned), no new recovery orders created
- Startup: cancels stale recovery orders on Kraken when flag is False
- All dashboard/API/throughput sizer code gated behind this flag

Source: `docs/STRATEGIC_CAPITAL_DEPLOYMENT_SPEC.md` §3

#### 1e. Update Module List (§1, §2)

Add to primary code references:
- `throughput_sizer.py` — fill-time order sizing
- `ai_advisor.py` — AI regime advisor + trade analysis
- `bayesian_engine.py` — BOCPD belief engine
- `survival_model.py` — exit survival curves
- `bocpd.py` — Bayesian Online Changepoint Detection
- `position_ledger.py` — position tracking for self-healing

#### 1f. Update Persistence (§22)

Add these snapshot fields:
- Throughput sizer state (`tp_state`)
- AI regime advisor state (`ai_regime_*`)
- Tertiary HMM state (`hmm_state_tertiary`)
- Accumulation engine state (`accum_*`)
- Balance intelligence state (`balance_intel_*`)
- Self-healing state (per-slot subsidy trackers)
- Bayesian engine state (`bayesian_*`)
- Position ledger state

### Phase 2: Add New Sections

Each new section should follow the existing pattern: description, architecture diagram (if applicable), lifecycle, config table, degradation guarantees.

#### §27. AI Regime Advisor

- Architecture: LLM second opinion on HMM regime; manual override via dashboard
- Provider chain: DeepSeek-V3 primary + Groq Llama-70B fallback
- Accumulation signal: `accumulation_signal` ("accumulate_doge"|"hold"|"accumulate_usd") + `accumulation_conviction` (0-100)
- Context fed to LLM: HMM 1m+15m+1h state, transition matrices, consensus, training quality, regime_history_30m, mechanical tier, capital block, accumulation context
- Threading: daemon thread writes to `_ai_regime_pending_result`; main loop processes next cycle
- Scheduler: periodic (5min) + event-triggered (tier/consensus change) + 60s debounce
- AI-suggested TTL: LLM estimates override duration (10-60 min)
- Config table: `AI_REGIME_ADVISOR_ENABLED`, `AI_REGIME_INTERVAL_SEC`, `AI_REGIME_DEBOUNCE_SEC`, `AI_OVERRIDE_TTL_SEC`, `AI_OVERRIDE_MAX_TTL_SEC`, `AI_OVERRIDE_MIN_CONVICTION`, `AI_REGIME_HISTORY_SIZE`, `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`, etc.
- Degradation: if no API key configured or all providers fail, advisor is simply inactive

Source: `docs/AI_REGIME_ADVISOR_SPEC.md`, `docs/AI_MULTI_PROVIDER_SPEC.md`, `docs/AI_SUGGESTED_TTL_SPEC.md`, `ai_advisor.py`

#### §28. 1h HMM (Tertiary Timeframe)

- Third RegimeDetector: 60m candles, 500 training window, 150 min samples, 30 inference
- Bootstrap: resamples 4×15m → 1h candles (360 synthetic on day one)
- Strategic signal only — NOT in tactical 1m+15m consensus
- Transition tracking: `from_regime`, `to_regime`, `confirmation_count` (default 2 candles)
- Config: `HMM_TERTIARY_ENABLED`, `HMM_TERTIARY_INTERVAL_MIN=60`, `HMM_TERTIARY_TRAINING_CANDLES=500`

Source: `docs/STRATEGIC_CAPITAL_DEPLOYMENT_SPEC.md` §1

#### §29. DCA Accumulation Engine

- State machine: IDLE → ARMED → ACTIVE → COMPLETED/STOPPED
- ARMED on confirmed 1h regime transition; ACTIVE on AI conviction ≥ 60
- Market buys via `place_order(ordertype="market")`, chunked ($2 default)
- Abort conditions: 1h regime revert, AI hold streak, drawdown breach, capacity stop, manual stop
- Config: `ACCUM_ENABLED`, `ACCUM_MIN_CONVICTION=60`, `ACCUM_RESERVE_USD=50`, `ACCUM_MAX_BUDGET_USD=50`, `ACCUM_CHUNK_USD=2`, `ACCUM_INTERVAL_SEC=120`, `ACCUM_MAX_DRAWDOWN_PCT=3.0`

Source: `docs/STRATEGIC_CAPITAL_DEPLOYMENT_SPEC.md` §2

#### §30. Durable Profit Settlement

- Dual realized trackers on `PairState`: `total_profit` (net PnL) + `total_settled_usd` (estimated quote-balance delta)
- Per-cycle fee split: `entry_fee`, `exit_fee`, `quote_fee`, `settled_usd`
- Quote-first allocation: B-side sizing uses settled USD rather than estimated profit
- Eliminates value leak from fee estimation drift over many cycles

Source: `docs/DURABLE_PROFIT_EXIT_ACCOUNTING_SPEC.md`

#### §31. Balance Intelligence

- Three capabilities:
  1. External flow detection: deposits/withdrawals via Kraken Ledger API
  2. Persistent DOGE-eq time-series: Supabase snapshots every N minutes
  3. Baseline auto-adjustment: recalibrates expected balance after detected flows
- Observability only — no behavioral changes to trading
- Dashboard: equity history chart, flow event log
- Config: `BALANCE_INTEL_ENABLED`, `BALANCE_INTEL_INTERVAL_SEC`, `BALANCE_INTEL_SNAPSHOT_INTERVAL_SEC`

Source: `docs/BALANCE_INTELLIGENCE_SPEC.md`

#### §32. Self-Healing Slots

- Replaces orphan/recovery system for stuck exits
- Per-slot subsidy accounting: profits fund repricing of stuck exits
- Position ledger (`position_ledger.py`): tracks exact per-slot exposure
- Stale exits are repriced progressively toward market price
- Dashboard: self-healing status panel, per-slot subsidy meters
- API actions: self-healing manual triggers
- Config: self-healing thresholds, subsidy rates, repricing intervals

Source: `docs/SELF_HEALING_SLOTS_SPEC.md`, `position_ledger.py`

#### §33. Bayesian Intelligence Stack

Multi-module upgrade replacing discrete HMM labels with continuous belief state:

- **BOCPD** (`bocpd.py`): Bayesian Online Changepoint Detection — detects regime transitions in real-time with run-length posterior
- **Survival Model** (`survival_model.py`): Per-trade exit time survival curves — provides probability of fill within T minutes by regime
- **Belief Engine** (`bayesian_engine.py`): 9-dimensional posterior (3 regimes × 3 timeframes) with evidence accumulation, replacing discrete argmax labels
- Integration: fed by HMM posteriors + OHLCV features, outputs continuous belief state consumed by throughput sizer and AI advisor
- Phases: Phase 0 (shadow), Phase 1 (belief-informed sizing), Phase 2 (per-trade management), Phase 3 (adaptive action knobs)

Source: `docs/BAYESIAN_INTELLIGENCE_SPEC.md`, `bayesian_engine.py`, `bocpd.py`, `survival_model.py`

#### §34. Manifold Score, Operations Panel, Churner (Spec Only — Partial Implementation)

- **Manifold Trading Score (MTS)**: Unified geometric mean of 20+ trading signals (regime, volatility, fill rate, capacity, survival, belief entropy)
- **Operations Panel**: Runtime feature toggles, statistical SVG visualizations
- **Churner**: Slot spawning/recycling based on MTS thresholds
- Status: spec complete, partially wired into Bayesian engine and self-healing. Document what's implemented.

Source: `docs/MANIFOLD_SCORE_OPS_PANEL_CHURNER_SPEC.md`

### Phase 3: Update Cross-Cutting Sections

#### 3a. Main Loop (§4)

Add these steps to the main loop sequence (approximate positions):

- After step 8 (regime tier): **AI regime advisor tick** (`_maybe_run_ai_regime_advisor`)
- After step 8: **Bayesian engine update** (`_update_bayesian_engine`)
- After step 10 (entry scheduler): **DCA accumulation tick** (`_tick_accumulation`)
- After step 14: **Tertiary OHLCV sync** (`_sync_ohlcv_candles_tertiary`)
- After step 15: **Self-healing slot check** (`_check_self_healing`)
- After step 16: **Balance intelligence check** (`_maybe_check_balance_intelligence`)

Verify exact ordering from `bot.py` main loop before writing.

#### 3b. Dashboard/API (§23)

Add new status payload blocks:
- `throughput_sizer`: replaces `kelly` block
- `ai_regime_advisor`: advisor state, last recommendation, conviction, suggested TTL
- `bayesian`: belief state, BOCPD run length, survival estimates
- `self_healing`: per-slot subsidy, repricing state, position ledger
- `accumulation`: DCA state machine, budget remaining, drawdown
- `balance_intelligence`: equity snapshots, detected flows
- `hmm_tertiary`: 1h regime state

Add new dashboard actions:
- AI regime override (accept/reject/dismiss)
- Accumulation start/stop
- Self-healing manual trigger

#### 3c. Telegram (§24)

Add new commands if they exist. Verify from `bot.py` or `telegram_menu.py`.

## Formatting & Style Requirements

- Match the existing document's voice: declarative, code-truth-first, config-table-heavy
- Every new section gets: prose description, config table, degradation guarantees where applicable
- Section numbers continue from §26 (§27, §28, etc.)
- Keep each new section 30-60 lines (matching existing density)
- No architecture diagrams unless the existing doc already has them for similar features
- Reference spec docs for full details: "See `docs/FOO_SPEC.md` for full spec."
- Update the "Last updated" line to `2026-02-17 (rev 4)`
- Update the "Primary code references" line to include new modules
- Update §26 Developer Notes to include new modules in the "update these files together" list

## Verification Checklist

After the update, the implementor should verify:

1. Every `.py` module in the root directory is referenced somewhere in the doc
2. Every `*_ENABLED` config flag in `config.py` is documented
3. Every `/api/status` payload block name in `bot.py` `status_payload()` is documented in §23
4. Every dashboard action in `bot.py` `_handle_action()` is documented in §23
5. Every Telegram command in `bot.py` or `telegram_menu.py` is documented in §24
6. No references to Kelly sizer remain (replaced by throughput sizer)
7. `pair_model.py` is NOT in scope for this update (separate task)

## Out of Scope

- `pair_model.py` executable model updates (separate effort)
- `docs/FACTORY_LENS_SPEC.md` implementation
- `docs/DASHBOARD_UX_SPEC.md` implementation (VIM keyboard, etc.)
- Code changes — this is a documentation-only update
- Spec documents themselves — only `STATE_MACHINE.md` is modified
