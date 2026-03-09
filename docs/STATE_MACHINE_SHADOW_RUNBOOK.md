# State-Machine Shadow Runbook (Phase 3 Gate)

Use this runbook to execute the Phase 3 production-shadow gate for the Haskell state-machine path.

Scope:
- Python remains authoritative.
- Haskell runs in shadow via `DOGE_CORE_SHADOW=1`.
- Divergences/failures are observed via `/api/status.state_machine_shadow`.

## 1) Preconditions

1. Branch includes:
- `doge_core.py` shadow path
- `state_machine_shadow` block in `/api/status`
- dashboard widget for state-machine shadow telemetry

2. Runtime artifacts available:
- `doge-core-exe` present on host
- bot starts cleanly with current config and credentials

3. Baseline quality gate:
- Unit suite green locally (`python3 -m unittest discover -s tests`)

## 2) Shadow Enablement

Set environment variables for the bot process:

```bash
DOGE_CORE_BACKEND=python
DOGE_CORE_SHADOW=1
DOGE_CORE_EXE=/absolute/path/to/doge-core-exe
```

Notes:
- Keep `DOGE_CORE_BACKEND=python` during shadow.
- Shadow mode still calls Haskell for comparison.

## 3) Initial Smoke (10-15 min)

After startup, verify status payload:

```bash
BASE_URL="http://127.0.0.1:${PORT:-8080}"
curl -s "$BASE_URL/api/status" | jq '{
  mode,
  pair,
  top_phase,
  state_machine_shadow
}'
```

Expected:
- `state_machine_shadow.enabled = true`
- `state_machine_shadow.executable_available = true`
- `transition_checks` and/or `invariant_checks` increase over time
- `shadow_failures = 0`

## 4) 48h Monitoring Loop

Sample every minute and append to JSONL for postmortem:

```bash
BASE_URL="http://127.0.0.1:${PORT:-8080}"
OUT="logs/state_machine_shadow_$(date -u +%Y%m%dT%H%M%SZ).jsonl"
for i in {1..2880}; do
  ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  j=$(curl -s "$BASE_URL/api/status")
  echo "$j" | jq --arg ts "$ts" '{
    ts: $ts,
    mode,
    top_phase,
    state_machine_shadow
  }' >> "$OUT"
  sleep 60
done
```

Operational watch fields:
- `state_machine_shadow.total_divergences`
- `state_machine_shadow.transition_divergences`
- `state_machine_shadow.invariant_divergences`
- `state_machine_shadow.shadow_failures`
- `state_machine_shadow.last_divergence_kind`
- `state_machine_shadow.last_divergence_event`
- `state_machine_shadow.last_shadow_error`

## 5) Gate Criteria

Pass (promote toward primary consideration) if all hold for 48h+:

1. `shadow_failures == 0` (or only transient startup noise with clear root cause and no recurrence)
2. No high-severity divergence clusters (same event repeatedly diverging)
3. No operator-visible instability attributable to shadow path
4. Divergences (if any) are explained, reproducible, and triaged

Fail (hold promotion) if any occur:

1. Persistent or rising `shadow_failures`
2. Repeated divergence on common hot-path events (`FillEvent`, `TimerTick`, `PriceTick`)
3. Any evidence of shadow path impacting authoritative behavior (should never happen)

## 6) Rollback / Safe Disable

Immediate safe disable (no deploy artifact change required):

```bash
DOGE_CORE_SHADOW=0
```

Optional hard fallback:

```bash
DOGE_CORE_BACKEND=python
DOGE_CORE_SHADOW=0
```

Then restart process and verify:

```bash
curl -s "$BASE_URL/api/status" | jq '.state_machine_shadow'
```

Expected:
- `enabled = false`

## 7) Evidence Package (for sign-off)

Collect:

1. 48h JSONL telemetry log
2. Screenshot/export of dashboard shadow widget over interval
3. Count summary:
- max/last `total_divergences`
- max/last `shadow_failures`
4. Triage notes for every non-zero divergence/failure event

Store under `logs/` and link from change-control notes before Phase 3 closeout.
