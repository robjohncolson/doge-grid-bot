# Factory Lens Specification

Version: v0.2.3  
Date: 2026-02-11  
Status: Draft (consolidated)

## 1. Purpose

Factory Lens is a separate diagnostic client for the DOGE v1 runtime. It provides:

1. A forward production view (`runtime -> factory`).
2. A reverse diagnosis view (`symptom -> cause -> action`).
3. Action-oriented operator guidance, including starter prompts for Codex/Claude.

It is intentionally separate from core order execution logic and should not change trading behavior.

## 2. Scope and Non-Goals

### 2.1 In Scope

1. Deterministic diagnosis from runtime payloads.
2. A text UI (80x24-first) using a factory metaphor.
3. `/api/diagnosis` endpoint from bot runtime.
4. Prompt launcher context for engineering remediation.

### 2.2 Out of Scope

1. Executing trades directly from Factory Lens.
2. AI-in-the-loop order decisions.
3. Replacing strategy logic in `state_machine.py`.
4. Coupling Lens lifecycle to bot process lifecycle.

## 3. Core Concept Mapping (Factorio -> Runtime)

| Factory Concept | Runtime Meaning | Program Mapping |
|---|---|---|
| Electricity | Price freshness + API health | `bot.py` price refresh, stale guards |
| Inserter | Slot worker | per-slot loop execution in `bot.py` |
| Conveyor belt | Event/action flow | `_apply_event`, `_execute_actions`, `_poll_order_status` |
| S0 assembler | Entry production | `_ensure_slot_bootstrapped`, `sm.add_entry_order` |
| S1a/S1b assembler | In-position cycle work | phase transitions in `state_machine.py` |
| S2 assembler | Deadlock handling | timer-orphan logic in `state_machine.py` |
| Input chests | Free USD / DOGE | `_safe_balance`, `_usd_balance`, `_doge_balance` |
| Output chest | Realized cycles/PnL | `completed_cycles`, `total_profit` |
| Circuit network | Guardrails and controls | pause/halt, invariants, budget limits |
| Logistic bots | Self-heal layer | `_auto_repair_degraded_slot` and normalization |

## 4. UX Model

Factory Lens is a parallel client, not an in-process overlay.

### 4.1 Keybindings

1. `F2` toggles Factory Lens view.
2. `Tab` switches Forward/Reverse pane focus.
3. `Left/Right` cycles subsystem tabs.
4. `Up/Down` changes selected slot/symptom.
5. `Enter` opens detail drawer.
6. `Esc` closes drawer or exits.
7. Fallback on terminals without function keys: `Ctrl+O`.

### 4.2 80x24-First Layout

1. Header: lifecycle mode, operating state, pair, price, price age, power state.
2. Left pane: compact factory rows (slots/assemblers/power/production).
3. Right pane: symptom cards sorted by priority.
4. Bottom console: interpretation, evidence, actions, prompt snippets.

Compact slot row format:

`07 S1a SO miss:B blk:budget heal:retry`

## 5. Diagnosis Semantics

Diagnosis uses deterministic rules. No probabilistic confidence score in v0.2.3.

Each symptom card includes:

1. `symptom_id`
2. `severity` (`info`, `warn`, `crit`)
3. `priority` (rank)
4. `rule_id`
5. `summary`
6. `interpretation`
7. `evidence` (1-8 lines)
8. `actions`
9. `affected_slots`
10. `signal_code`
11. `visual_signals`
12. `prompts` (`codex_prompt`, `claude_prompt`) with context cap

## 6. Symptom Taxonomy v0.2.3

1. `IDLE_NORMAL`
2. `BELT_JAM`
3. `POWER_BROWNOUT`
4. `LANE_STARVATION`
5. `RECOVERY_BACKLOG`
6. `CIRCUIT_TRIP_RISK`

## 7. Telemetry Isolation Contract (Hard Requirement)

1. Runtime telemetry is stored in a dedicated `TelemetryAccumulator`.
2. Trading code may write/increment telemetry but must never read telemetry for decisions.
3. Only status/diagnosis endpoints may read telemetry.
4. Telemetry failures must degrade to missing metrics; trading behavior must remain unchanged.

## 8. Time-Window Contract

Time windows are computed in runtime (not diagnosis engine) so the diagnosis engine remains pure.

Required windows:

1. 1 minute
2. 5 minutes
3. 24 hour event counters where specified

## 9. Industrial Aesthetic Telemetry Requirements

To support a true "factory diagnostics" feel:

1. `grid_load` models power/utilization/brownout before hard skips.
2. `production_stats` models throughput (cycles, fee consumption).
3. `assembler_states` models bottleneck classes as distinct categories.
4. Diagnosis cards include `signal_code` and `visual_signals` for compact icon-like rendering.

Assembler bottleneck classification rules:

1. `bottleneck_input_starved`: slot is missing a required entry leg and cannot place it due to insufficient USD or DOGE against current bootstrap minimums.
2. `bottleneck_output_full`: slot is trapped in dual-exit pressure (`S2`) beyond timeout/deadlock thresholds.
3. `bottleneck_low_power`: slot needed an order lifecycle action but it was skipped/blocked by API budget saturation or stale-price power gating.

## 10. Prompt Context Cap

Prompt payload must be concise and operator-usable:

1. Top symptom card only.
2. 1-8 evidence lines.
3. Up to 5 compact slot rows.
4. No full raw status payload dumps.
5. Prompt length cap: `<= 1800` chars each.
6. `context_evidence` may be a curated subset of symptom `evidence` for prompt brevity; it may also be identical when all lines are relevant.

## 11. API Contracts

### 11.1 `/api/status` Extension

Add `telemetry` object under status payload using schema `factory-lens-telemetry.v1`.

### 11.2 `/api/diagnosis`

Returns deterministic diagnosis report using schema `factory-lens-diagnosis.v1`.

The report carries both:

1. `mode`: lifecycle mode (`INIT`, `RUNNING`, `PAUSED`, `HALTED`).
2. `operating_state`: operator-facing health (`normal`, `degraded`, `paused`, `halted`).

Compatibility rules:

1. Always include `schema_version` and `engine_version`.
2. Lens clients must render unknown `symptom_id`/`signal_code` cards as raw fallback.
3. Lens clients should show warning banner: `engine newer than client`.
4. `warnings` is optional; clients must default missing `warnings` to an empty array.
5. v0.2.3 assumes one diagnosis report per pair (`pair` scoped). Aggregate multi-pair diagnosis endpoints are future work.

## 12. JSON Schema: `status.telemetry` (`factory-lens-telemetry.v1`)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://doge-grid-bot/schemas/status.telemetry.v1.json",
  "title": "DOGE Bot status.telemetry v1",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "telemetry_version",
    "generated_at",
    "loop_seq",
    "window_seconds",
    "max_api_calls_per_loop",
    "loop_private_calls_last",
    "grid_load",
    "budget_skips_1m",
    "budget_skips_5m",
    "place_skips_budget_1m",
    "place_skips_budget_5m",
    "cancel_skips_budget_1m",
    "cancel_skips_budget_5m",
    "query_skips_budget_1m",
    "query_skips_budget_5m",
    "price_refresh_failures_1m",
    "price_refresh_failures_5m",
    "api_errors_1m",
    "api_errors_5m",
    "stale_price_pause_events_5m",
    "degraded_slots_current",
    "degraded_slots_avg_5m",
    "degraded_transitions_5m",
    "auto_repair_attempts_5m",
    "auto_repair_success_5m",
    "auto_repair_blocked_5m",
    "auto_repair_block_reasons_5m",
    "recovery_orders_current",
    "recovery_backlog_growth_5m",
    "invariant_bypass_min_size_5m",
    "invariant_bypass_bootstrap_pending_5m",
    "pause_events_24h",
    "halt_events_24h",
    "production_stats",
    "assembler_states",
    "window_samples_5m"
  ],
  "properties": {
    "telemetry_version": {
      "type": "string",
      "const": "factory-lens-telemetry.v1"
    },
    "generated_at": {
      "type": "number",
      "minimum": 0
    },
    "loop_seq": {
      "type": "integer",
      "minimum": 0
    },
    "window_seconds": {
      "type": "object",
      "additionalProperties": false,
      "required": ["one_min", "five_min"],
      "properties": {
        "one_min": { "type": "integer", "const": 60 },
        "five_min": { "type": "integer", "const": 300 }
      }
    },
    "max_api_calls_per_loop": {
      "type": "integer",
      "minimum": 1
    },
    "loop_private_calls_last": {
      "type": "integer",
      "minimum": 0
    },
    "grid_load": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "capacity_per_loop",
        "load_avg_1m",
        "utilization_pct_1m",
        "saturation_state"
      ],
      "properties": {
        "capacity_per_loop": {
          "type": "integer",
          "minimum": 1
        },
        "load_avg_1m": {
          "type": "number",
          "minimum": 0
        },
        "utilization_pct_1m": {
          "type": "number",
          "minimum": 0
        },
        "saturation_state": {
          "type": "string",
          "enum": ["stable", "brownout", "blackout"]
        }
      }
    },
    "budget_skips_1m": {
      "type": "integer",
      "minimum": 0
    },
    "budget_skips_5m": {
      "type": "integer",
      "minimum": 0
    },
    "place_skips_budget_1m": {
      "type": "integer",
      "minimum": 0
    },
    "place_skips_budget_5m": {
      "type": "integer",
      "minimum": 0
    },
    "cancel_skips_budget_1m": {
      "type": "integer",
      "minimum": 0
    },
    "cancel_skips_budget_5m": {
      "type": "integer",
      "minimum": 0
    },
    "query_skips_budget_1m": {
      "type": "integer",
      "minimum": 0
    },
    "query_skips_budget_5m": {
      "type": "integer",
      "minimum": 0
    },
    "price_refresh_failures_1m": {
      "type": "integer",
      "minimum": 0
    },
    "price_refresh_failures_5m": {
      "type": "integer",
      "minimum": 0
    },
    "api_errors_1m": {
      "type": "integer",
      "minimum": 0
    },
    "api_errors_5m": {
      "type": "integer",
      "minimum": 0
    },
    "stale_price_pause_events_5m": {
      "type": "integer",
      "minimum": 0
    },
    "degraded_slots_current": {
      "type": "integer",
      "minimum": 0
    },
    "degraded_slots_avg_5m": {
      "type": "number",
      "minimum": 0
    },
    "degraded_transitions_5m": {
      "type": "integer",
      "minimum": 0
    },
    "auto_repair_attempts_5m": {
      "type": "integer",
      "minimum": 0
    },
    "auto_repair_success_5m": {
      "type": "integer",
      "minimum": 0
    },
    "auto_repair_blocked_5m": {
      "type": "integer",
      "minimum": 0
    },
    "auto_repair_block_reasons_5m": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "insufficient_usd",
        "insufficient_doge",
        "api_budget_exhausted",
        "stale_price",
        "paused_or_halted",
        "below_exchange_minimum",
        "placement_exception_other",
        "no_missing_leg",
        "other"
      ],
      "properties": {
        "insufficient_usd": {
          "type": "integer",
          "minimum": 0
        },
        "insufficient_doge": {
          "type": "integer",
          "minimum": 0
        },
        "api_budget_exhausted": {
          "type": "integer",
          "minimum": 0
        },
        "stale_price": {
          "type": "integer",
          "minimum": 0
        },
        "paused_or_halted": {
          "type": "integer",
          "minimum": 0
        },
        "below_exchange_minimum": {
          "type": "integer",
          "minimum": 0
        },
        "placement_exception_other": {
          "type": "integer",
          "minimum": 0
        },
        "no_missing_leg": {
          "type": "integer",
          "minimum": 0
        },
        "other": {
          "type": "integer",
          "minimum": 0
        }
      }
    },
    "recovery_orders_current": {
      "type": "integer",
      "minimum": 0
    },
    "recovery_backlog_growth_5m": {
      "type": "integer"
    },
    "invariant_bypass_min_size_5m": {
      "type": "integer",
      "minimum": 0
    },
    "invariant_bypass_bootstrap_pending_5m": {
      "type": "integer",
      "minimum": 0
    },
    "pause_events_24h": {
      "type": "integer",
      "minimum": 0
    },
    "halt_events_24h": {
      "type": "integer",
      "minimum": 0
    },
    "production_stats": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "cycles_completed_1m",
        "cycles_completed_5m",
        "fees_consumed_5m",
        "throughput_state"
      ],
      "properties": {
        "cycles_completed_1m": {
          "type": "integer",
          "minimum": 0
        },
        "cycles_completed_5m": {
          "type": "integer",
          "minimum": 0
        },
        "fees_consumed_5m": {
          "type": "number",
          "minimum": 0
        },
        "throughput_state": {
          "type": "string",
          "enum": ["idle", "low", "normal", "high"]
        }
      }
    },
    "assembler_states": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "idle_s0",
        "working_s1",
        "working_s2",
        "bottleneck_input_starved",
        "bottleneck_output_full",
        "bottleneck_low_power"
      ],
      "properties": {
        "idle_s0": {
          "type": "integer",
          "minimum": 0
        },
        "working_s1": {
          "type": "integer",
          "minimum": 0
        },
        "working_s2": {
          "type": "integer",
          "minimum": 0
        },
        "bottleneck_input_starved": {
          "type": "integer",
          "minimum": 0
        },
        "bottleneck_output_full": {
          "type": "integer",
          "minimum": 0
        },
        "bottleneck_low_power": {
          "type": "integer",
          "minimum": 0
        }
      }
    },
    "window_samples_5m": {
      "type": "integer",
      "minimum": 0
    }
  }
}
```

## 13. JSON Schema: `/api/diagnosis` (`factory-lens-diagnosis.v1`)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://doge-grid-bot/schemas/api.diagnosis.v1.json",
  "title": "Factory Lens Diagnosis API v1",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "schema_version",
    "engine_version",
    "generated_at",
    "pair",
    "mode",
    "operating_state",
    "status_snapshot_hash",
    "top_symptom_id",
    "top_symptom_severity",
    "symptoms"
  ],
  "properties": {
    "schema_version": {
      "type": "string",
      "const": "factory-lens-diagnosis.v1"
    },
    "engine_version": {
      "type": "string",
      "minLength": 1,
      "maxLength": 64
    },
    "generated_at": {
      "type": "number",
      "minimum": 0
    },
    "pair": {
      "type": "string",
      "minLength": 3,
      "maxLength": 32
    },
    "mode": {
      "type": "string",
      "enum": ["INIT", "RUNNING", "PAUSED", "HALTED"]
    },
    "operating_state": {
      "type": "string",
      "enum": ["normal", "degraded", "paused", "halted"]
    },
    "status_snapshot_hash": {
      "type": "string",
      "pattern": "^[a-f0-9]{8,128}$"
    },
    "top_symptom_id": {
      "type": "string",
      "pattern": "^[A-Z0-9_]{2,64}$"
    },
    "top_symptom_severity": {
      "type": "string",
      "enum": ["info", "warn", "crit"]
    },
    "symptoms": {
      "type": "array",
      "minItems": 1,
      "maxItems": 32,
      "items": {
        "$ref": "#/$defs/symptom_card"
      }
    },
    "warnings": {
      "type": "array",
      "maxItems": 16,
      "items": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200
      }
    }
  },
  "$defs": {
    "symptom_card": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "symptom_id",
        "severity",
        "priority",
        "rule_id",
        "summary",
        "interpretation",
        "evidence",
        "actions",
        "affected_slots",
        "signal_code",
        "visual_signals",
        "prompts"
      ],
      "properties": {
        "symptom_id": {
          "type": "string",
          "pattern": "^[A-Z0-9_]{2,64}$"
        },
        "severity": {
          "type": "string",
          "enum": ["info", "warn", "crit"]
        },
        "priority": {
          "type": "integer",
          "minimum": 1,
          "maximum": 999
        },
        "rule_id": {
          "type": "string",
          "pattern": "^[a-z0-9_.:-]{3,128}$"
        },
        "summary": {
          "type": "string",
          "minLength": 1,
          "maxLength": 120
        },
        "interpretation": {
          "type": "string",
          "minLength": 1,
          "maxLength": 320
        },
        "evidence": {
          "type": "array",
          "minItems": 1,
          "maxItems": 8,
          "items": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          }
        },
        "actions": {
          "type": "array",
          "minItems": 1,
          "maxItems": 8,
          "items": {
            "type": "string",
            "minLength": 1,
            "maxLength": 180
          }
        },
        "affected_slots": {
          "type": "array",
          "uniqueItems": true,
          "maxItems": 64,
          "items": {
            "type": "integer",
            "minimum": 0
          }
        },
        "signal_code": {
          "type": "string",
          "pattern": "^[A-Z0-9_]{2,64}$"
        },
        "visual_signals": {
          "type": "array",
          "maxItems": 6,
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": ["icon", "color", "value"],
            "properties": {
              "icon": {
                "type": "string",
                "enum": ["electricity", "fluid", "inserter", "belt", "hazard", "factory", "chest"]
              },
              "color": {
                "type": "string",
                "enum": ["red", "yellow", "green"]
              },
              "value": {
                "type": "string",
                "minLength": 1,
                "maxLength": 40
              }
            }
          }
        },
        "prompts": {
          "$ref": "#/$defs/prompt_bundle"
        }
      }
    },
    "prompt_bundle": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "codex_prompt",
        "claude_prompt",
        "context_slot_rows",
        "context_evidence",
        "estimated_chars"
      ],
      "properties": {
        "codex_prompt": {
          "type": "string",
          "minLength": 1,
          "maxLength": 1800
        },
        "claude_prompt": {
          "type": "string",
          "minLength": 1,
          "maxLength": 1800
        },
        "context_slot_rows": {
          "type": "array",
          "maxItems": 5,
          "items": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80
          }
        },
        "context_evidence": {
          "type": "array",
          "minItems": 1,
          "maxItems": 8,
          "items": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160
          }
        },
        "estimated_chars": {
          "type": "integer",
          "minimum": 1,
          "maximum": 2000
        }
      }
    }
  }
}
```

## 14. Diagnosis Engine Structure

Recommended module contract:

1. File: `diagnosis_engine.py`
2. No imports from runtime modules.
3. Pure entry point: `diagnose(status_payload: dict) -> dict`
4. CLI mode: `python diagnosis_engine.py < status.json`
5. Importable by `bot.py` for `/api/diagnosis`

## 15. Saturation and Throughput Guidance

### 15.1 `grid_load.saturation_state` Suggested Thresholds

1. `stable`: utilization < 70% and no recent budget skips.
2. `brownout`: utilization between 70% and 95%, or low nonzero skip rate.
3. `blackout`: utilization > 95% or repeated skips impacting place/cancel/query.

### 15.2 `production_stats.throughput_state` Suggested Thresholds

Default 5-minute banding (engine-configurable per pair/runtime profile):

1. `idle`: `0` cycles in 5m, no symptom-level faults.
2. `low`: `1-2` cycles in 5m.
3. `normal`: `3-9` cycles in 5m.
4. `high`: `>=10` cycles in 5m.

## 16. Milestone Plan

### M0: Spec Lock

1. Freeze schema fields and keybindings.
2. Freeze symptom taxonomy and severity levels.
3. Freeze compact row format.

Acceptance:

1. Canonical spec committed.
2. Example payloads validated manually.

### M1a: Telemetry Instrumentation Core (Highest Runtime Risk)

1. Add `TelemetryAccumulator`.
2. Implement rolling-window counters.
3. Add `grid_load`, `production_stats`, `assembler_states` counters.
4. Ensure write-only usage from trading loop.
5. Do not expose new fields on API yet (shadow phase).

Acceptance:

1. No trading behavior changes from instrumentation.
2. Unit tests enforce no telemetry-read decision branches.
3. Runtime remains stable in shadow mode.

### M1b: Telemetry Exposure and Validation

1. Serialize telemetry under `/api/status` as `telemetry`.
2. Validate payload against `factory-lens-telemetry.v1`.
3. Add compatibility tests for missing/partial telemetry fallback.

Acceptance:

1. Status payload schema-valid in normal operation.
2. Missing telemetry does not break runtime/status endpoint.

### M2: Diagnosis Engine

1. Implement pure rule engine in standalone module.
2. Emit `signal_code` and `visual_signals`.
3. Add fixture-driven tests.

Acceptance:

1. Deterministic outputs across fixed fixtures.
2. Stable priority ordering.

### M3: API Integration

1. Add `/api/diagnosis`.
2. Include schema and engine version fields.
3. Add unknown-version fallback signaling.

Acceptance:

1. Endpoint latency acceptable.
2. Contract-valid payloads.

### M4: Factory Lens Client (Textual)

1. Build two-pane UI with compact rows.
2. Render power/load and production bars.
3. Add slot/symptom detail drawers.

Acceptance:

1. Usable on 80x24 and wider terminals.
2. Windows Terminal compatibility.

### M5: Action Console + Prompt Launcher

1. Add concise action cards.
2. Add codex/claude prompt generation with size cap.

Acceptance:

1. Prompts are copy-pasteable and bounded.
2. Operators can act without raw JSON inspection.

### M6: Pilot and Threshold Tuning

1. Run in production-adjacent mode.
2. Tune thresholds for false-positive control.
3. Finalize operator runbook.

Acceptance:

1. Operators can diagnose common incidents without log deep-dive.
2. False-positive rate acceptable for daily operations.

## 17. Framework Decision

Recommended stack:

1. Runtime: existing Python bot process (`bot.py`) with diagnosis endpoint.
2. Client: separate Textual app.

Rationale:

1. Keeps trading runtime isolated.
2. Supports restart/update of UI without touching bot.
3. Provides terminal-native interactions on Windows/Linux/macOS.
