#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
import sys
from typing import Any

# Ensure repository root is importable when executed from doge-core/stub.
ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import state_machine as sm


def _event_from_payload(payload: dict[str, Any]) -> sm.Event:
    keys = set(payload.keys())

    if {"order_local_id", "txid", "side", "price", "volume", "fee", "timestamp"}.issubset(keys):
        return sm.FillEvent(**payload)

    if {"recovery_id", "txid", "side", "price", "volume", "fee", "timestamp"}.issubset(keys):
        return sm.RecoveryFillEvent(**payload)

    if {"recovery_id", "txid", "timestamp"}.issubset(keys):
        return sm.RecoveryCancelEvent(**payload)

    if keys == {"timestamp"}:
        return sm.TimerTick(**payload)

    if {"price", "timestamp"}.issubset(keys):
        return sm.PriceTick(**payload)

    raise ValueError(f"Unsupported event payload keys: {sorted(keys)}")


def _actions_to_payload(actions: list[sm.Action]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for action in actions:
        if is_dataclass(action):
            out.append(asdict(action))
        elif isinstance(action, dict):
            out.append(dict(action))
        else:
            raise TypeError(f"Unsupported action type: {type(action)!r}")
    return out


def _handle_transition(request: dict[str, Any]) -> dict[str, Any]:
    state = sm.from_dict(request["state"])
    cfg = sm.EngineConfig(**request["config"])
    event = _event_from_payload(request["event"])
    order_size_usd = float(request["order_size_usd"])
    raw_order_sizes = request.get("order_sizes")
    order_sizes = None
    if isinstance(raw_order_sizes, dict):
        order_sizes = {str(k): float(v) for k, v in raw_order_sizes.items()}

    next_state, actions = sm.transition(
        state,
        event,
        cfg,
        order_size_usd=order_size_usd,
        order_sizes=order_sizes,
    )
    return {
        "state": sm.to_dict(next_state),
        "actions": _actions_to_payload(actions),
    }


def _handle_check_invariants(request: dict[str, Any]) -> dict[str, Any]:
    state = sm.from_dict(request["state"])
    return {"violations": sm.check_invariants(state)}


def _dispatch(request: dict[str, Any]) -> dict[str, Any]:
    method = str(request.get("method", "")).strip()
    if method == "transition":
        return _handle_transition(request)
    if method == "check_invariants":
        return _handle_check_invariants(request)
    raise ValueError(f"Unsupported method: {method}")


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        print("empty request", file=sys.stderr)
        return 2
    try:
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise TypeError("request must be a JSON object")
        response = _dispatch(request)
        sys.stdout.write(json.dumps(response))
        return 0
    except Exception as exc:
        print(f"doge-core stub backend error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
