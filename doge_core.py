"""
Compatibility adapter for the state machine backend.

Default behavior keeps Python state_machine as the authoritative backend.
When a Haskell executable is available, transition/invariant checks can be
routed through subprocess JSON calls while preserving the Python API surface.
"""

from __future__ import annotations

import atexit
from collections import deque
from dataclasses import asdict, is_dataclass
import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from typing import Any, cast

import state_machine as _sm
from state_machine import (
    Action,
    BookCycleAction,
    CancelOrderAction,
    CycleRecord,
    EngineConfig,
    Event,
    FillEvent,
    OrphanOrderAction,
    OrderState,
    PairPhase,
    PairState,
    PlaceOrderAction,
    PriceTick,
    RecoveryCancelEvent,
    RecoveryFillEvent,
    RecoveryOrder,
    Role,
    Side,
    TimerTick,
    TradeId,
    add_entry_order,
    apply_order_txid,
    bootstrap_orders,
    compute_order_volume,
    derive_phase,
    find_order,
    from_dict,
    remove_order,
    remove_recovery,
    to_dict,
)


logger = logging.getLogger(__name__)

_BACKEND_ENV = "DOGE_CORE_BACKEND"
_EXE_ENV = "DOGE_CORE_EXE"
_TIMEOUT_ENV = "DOGE_CORE_TIMEOUT_SEC"
_PERSISTENT_ENV = "DOGE_CORE_PERSISTENT"
_SHADOW_ENV = "DOGE_CORE_SHADOW"


def _backend_mode() -> str:
    mode = str(os.getenv(_BACKEND_ENV, "auto")).strip().lower()
    if mode in {"python", "haskell", "auto"}:
        return mode
    return "auto"


def _timeout_sec() -> float:
    raw = os.getenv(_TIMEOUT_ENV, "5")
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return 5.0
    if timeout <= 0:
        return 5.0
    return timeout


def _shadow_enabled() -> bool:
    raw = str(os.getenv(_SHADOW_ENV, "0")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


_SHADOW_METRICS_LOCK = threading.Lock()
_SHADOW_METRICS: dict[str, Any] = {
    "transition_checks": 0,
    "invariant_checks": 0,
    "transition_divergences": 0,
    "invariant_divergences": 0,
    "shadow_failures": 0,
    "last_divergence_ts": None,
    "last_divergence_kind": "",
    "last_divergence_event": "",
    "last_shadow_error": "",
}


def _shadow_metric_inc(key: str, delta: int = 1) -> None:
    with _SHADOW_METRICS_LOCK:
        _SHADOW_METRICS[key] = int(_SHADOW_METRICS.get(key, 0)) + int(delta)


def _shadow_metric_set(**values: Any) -> None:
    with _SHADOW_METRICS_LOCK:
        for key, value in values.items():
            _SHADOW_METRICS[key] = value


def _record_shadow_divergence(kind: str, event_name: str = "") -> None:
    now_ts = float(time.time())
    if kind == "transition":
        _shadow_metric_inc("transition_divergences")
    elif kind == "invariant":
        _shadow_metric_inc("invariant_divergences")
    _shadow_metric_set(
        last_divergence_ts=now_ts,
        last_divergence_kind=str(kind),
        last_divergence_event=str(event_name),
    )


def _record_shadow_failure(exc: Exception) -> None:
    _shadow_metric_inc("shadow_failures")
    _shadow_metric_set(last_shadow_error=str(exc))


def get_shadow_metrics() -> dict[str, Any]:
    with _SHADOW_METRICS_LOCK:
        snapshot = dict(_SHADOW_METRICS)
    transition_divergences = int(snapshot.get("transition_divergences", 0) or 0)
    invariant_divergences = int(snapshot.get("invariant_divergences", 0) or 0)
    return {
        "enabled": bool(_shadow_enabled()),
        "executable_available": bool(_haskell_executable_available()),
        "transition_checks": int(snapshot.get("transition_checks", 0) or 0),
        "invariant_checks": int(snapshot.get("invariant_checks", 0) or 0),
        "transition_divergences": transition_divergences,
        "invariant_divergences": invariant_divergences,
        "total_divergences": transition_divergences + invariant_divergences,
        "shadow_failures": int(snapshot.get("shadow_failures", 0) or 0),
        "last_divergence_ts": snapshot.get("last_divergence_ts"),
        "last_divergence_kind": str(snapshot.get("last_divergence_kind", "") or ""),
        "last_divergence_event": str(snapshot.get("last_divergence_event", "") or ""),
        "last_shadow_error": str(snapshot.get("last_shadow_error", "") or ""),
    }


def reset_shadow_metrics() -> None:
    with _SHADOW_METRICS_LOCK:
        _SHADOW_METRICS.update(
            {
                "transition_checks": 0,
                "invariant_checks": 0,
                "transition_divergences": 0,
                "invariant_divergences": 0,
                "shadow_failures": 0,
                "last_divergence_ts": None,
                "last_divergence_kind": "",
                "last_divergence_event": "",
                "last_shadow_error": "",
            }
        )


_SERVER_EOF = object()


class _HaskellServerClient:
    def __init__(self, executable: Path, timeout_sec: float) -> None:
        self._executable = Path(executable)
        self._timeout_sec = timeout_sec
        self._proc: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[str | object] = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._stop_locked()

    def request(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_obj = {"method": method, **payload}
        request_raw = json.dumps(request_obj, separators=(",", ":"))

        with self._lock:
            self._ensure_started_locked()
            proc = self._proc
            if proc is None or proc.stdin is None:
                raise RuntimeError("haskell server stdin unavailable")

            try:
                proc.stdin.write(request_raw + "\n")
                proc.stdin.flush()
            except Exception as exc:
                self._stop_locked()
                raise RuntimeError("failed to write request to haskell server") from exc

            try:
                raw_response = self._responses.get(timeout=self._timeout_sec)
            except queue.Empty as exc:
                self._stop_locked()
                raise TimeoutError(f"haskell server timeout after {self._timeout_sec:.2f}s") from exc

            if raw_response is _SERVER_EOF:
                details = self._stderr_details_locked()
                self._stop_locked()
                if details:
                    raise RuntimeError(f"haskell server exited unexpectedly: {details}")
                raise RuntimeError("haskell server exited unexpectedly")

            if not isinstance(raw_response, str):
                self._stop_locked()
                raise RuntimeError("haskell server returned non-text response")

            parsed = json.loads(raw_response)
            if not isinstance(parsed, dict):
                raise RuntimeError("invalid response payload")

            response = cast(dict[str, Any], parsed)
            if "error" in response:
                raise RuntimeError(str(response["error"]))
            return response

    def _ensure_started_locked(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return

        self._stop_locked()
        self._responses = queue.Queue()
        self._stderr_tail.clear()

        proc = subprocess.Popen(
            [str(self._executable), "--server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._proc = proc

        stdout_thread = threading.Thread(target=self._pump_stdout, daemon=True)
        stdout_thread.start()

        stderr_thread = threading.Thread(target=self._pump_stderr, daemon=True)
        stderr_thread.start()

    def _pump_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            self._responses.put(_SERVER_EOF)
            return

        try:
            for raw_line in proc.stdout:
                self._responses.put(raw_line.rstrip("\r\n"))
        finally:
            self._responses.put(_SERVER_EOF)

    def _pump_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return

        for raw_line in proc.stderr:
            line = raw_line.rstrip()
            if line:
                self._stderr_tail.append(line)

    def _stderr_details_locked(self) -> str:
        if not self._stderr_tail:
            return ""
        return " | ".join(self._stderr_tail)

    def _stop_locked(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return

        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass

        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1.0)
        except Exception:
            pass

        for stream_name in ("stdout", "stderr"):
            stream = getattr(proc, stream_name, None)
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass


_SERVER_CLIENT: _HaskellServerClient | None = None
_SERVER_CLIENT_KEY: tuple[str, float] | None = None
_SERVER_CLIENT_LOCK = threading.Lock()


def _close_server_client() -> None:
    global _SERVER_CLIENT, _SERVER_CLIENT_KEY
    with _SERVER_CLIENT_LOCK:
        if _SERVER_CLIENT is not None:
            _SERVER_CLIENT.close()
        _SERVER_CLIENT = None
        _SERVER_CLIENT_KEY = None


atexit.register(_close_server_client)


def _persistent_enabled() -> bool:
    raw = str(os.getenv(_PERSISTENT_ENV, "1")).strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False

    # Stub wrappers are one-shot only; avoid server mode for them.
    exe = _haskell_executable()
    if exe.parent.name == "stub" or "stub" in exe.parts:
        return False
    return True


def _get_server_client() -> _HaskellServerClient:
    global _SERVER_CLIENT, _SERVER_CLIENT_KEY
    exe = _haskell_executable()
    timeout = _timeout_sec()
    key = (str(exe), timeout)

    with _SERVER_CLIENT_LOCK:
        if _SERVER_CLIENT is None or _SERVER_CLIENT_KEY != key:
            if _SERVER_CLIENT is not None:
                _SERVER_CLIENT.close()
            _SERVER_CLIENT = _HaskellServerClient(exe, timeout)
            _SERVER_CLIENT_KEY = key
        return _SERVER_CLIENT


def _haskell_executable() -> Path:
    configured = os.getenv(_EXE_ENV)
    if configured:
        return Path(configured).expanduser()
    default_name = "doge-core-exe.exe" if sys.platform == "win32" else "doge-core-exe"
    return Path(__file__).resolve().parent / default_name


def _haskell_executable_available() -> bool:
    return _haskell_executable().exists()


def _haskell_enabled() -> bool:
    mode = _backend_mode()
    if mode == "python":
        _close_server_client()
        return False

    exe = _haskell_executable()
    if _haskell_executable_available():
        return True

    if mode == "haskell":
        logger.warning(
            "DOGE_CORE_BACKEND=haskell but executable not found at %s. Falling back to Python backend.",
            exe,
        )
    return False


def _call_haskell_oneshot(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = json.dumps({"method": method, **payload})
    result = subprocess.run(
        [str(_haskell_executable())],
        input=request,
        capture_output=True,
        text=True,
        timeout=_timeout_sec(),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"exit {result.returncode}")

    stdout = result.stdout.strip()
    if not stdout:
        raise RuntimeError("empty response")
    parsed = json.loads(stdout)
    if not isinstance(parsed, dict):
        raise RuntimeError("invalid response payload")
    return cast(dict[str, Any], parsed)


def _call_haskell(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    if _persistent_enabled():
        try:
            return _get_server_client().request(method, payload)
        except Exception as exc:
            logger.warning("Persistent Haskell backend failed, retrying one-shot: %s", exc)
            _close_server_client()

    return _call_haskell_oneshot(method, payload)


def _state_payload(state: PairState | dict[str, Any]) -> dict[str, Any]:
    if isinstance(state, PairState):
        return to_dict(state)
    if isinstance(state, dict):
        return dict(state)
    raise TypeError(f"Unsupported state type: {type(state)!r}")


def _event_payload(event: Event | dict[str, Any]) -> dict[str, Any]:
    if isinstance(event, dict):
        return dict(event)
    if is_dataclass(event):
        return cast(dict[str, Any], asdict(event))
    raise TypeError(f"Unsupported event type: {type(event)!r}")


def _cfg_payload(cfg: EngineConfig | dict[str, Any]) -> dict[str, Any]:
    if isinstance(cfg, dict):
        return dict(cfg)
    if is_dataclass(cfg):
        return cast(dict[str, Any], asdict(cfg))
    raise TypeError(f"Unsupported config type: {type(cfg)!r}")


def _action_from_dict(raw: dict[str, Any]) -> Action:
    if {
        "local_id",
        "side",
        "role",
        "price",
        "volume",
        "trade_id",
        "cycle",
    }.issubset(raw):
        return PlaceOrderAction(
            local_id=int(raw["local_id"]),
            side=str(raw["side"]),
            role=str(raw["role"]),
            price=float(raw["price"]),
            volume=float(raw["volume"]),
            trade_id=str(raw["trade_id"]),
            cycle=int(raw["cycle"]),
            post_only=bool(raw.get("post_only", True)),
            reason=str(raw.get("reason", "")),
        )

    if "recovery_id" in raw and "local_id" in raw and "trade_id" not in raw:
        return OrphanOrderAction(
            local_id=int(raw["local_id"]),
            recovery_id=int(raw["recovery_id"]),
            reason=str(raw.get("reason", "")),
        )

    if {"trade_id", "cycle", "net_profit", "gross_profit", "fees"}.issubset(raw):
        return BookCycleAction(
            trade_id=str(raw["trade_id"]),
            cycle=int(raw["cycle"]),
            net_profit=float(raw["net_profit"]),
            gross_profit=float(raw["gross_profit"]),
            fees=float(raw["fees"]),
            settled_usd=float(raw.get("settled_usd", 0.0)),
            from_recovery=bool(raw.get("from_recovery", False)),
        )

    if "local_id" in raw and "txid" in raw:
        return CancelOrderAction(
            local_id=int(raw["local_id"]),
            txid=str(raw["txid"]),
            reason=str(raw.get("reason", "")),
        )

    raise ValueError(f"Unknown action payload: {raw}")


def _parse_actions(raw_actions: Any) -> list[Action]:
    if not isinstance(raw_actions, list):
        raise TypeError("actions payload must be a list")

    parsed: list[Action] = []
    for item in raw_actions:
        if isinstance(item, (PlaceOrderAction, CancelOrderAction, OrphanOrderAction, BookCycleAction)):
            parsed.append(item)
            continue
        if isinstance(item, dict):
            parsed.append(_action_from_dict(cast(dict[str, Any], item)))
            continue
        raise TypeError(f"Unsupported action payload: {type(item)!r}")
    return parsed


def _parse_state(raw_state: Any) -> PairState:
    if isinstance(raw_state, PairState):
        return raw_state
    if isinstance(raw_state, dict):
        return from_dict(cast(dict[str, Any], raw_state))
    raise TypeError(f"Unsupported state payload: {type(raw_state)!r}")


def _transition_payload(
    state: PairState,
    event: Event,
    cfg: EngineConfig,
    order_size_usd: float,
    order_sizes: dict[str, float] | None,
) -> dict[str, Any]:
    return {
        "state": _state_payload(state),
        "event": _event_payload(event),
        "config": _cfg_payload(cfg),
        "order_size_usd": float(order_size_usd),
        "order_sizes": dict(order_sizes) if order_sizes is not None else None,
    }


def _actions_payload(actions: list[Action]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for action in actions:
        if is_dataclass(action):
            payload.append(cast(dict[str, Any], asdict(action)))
        else:
            payload.append({"_repr": repr(action)})
    return payload


def _shadow_state_focus_payload(state: PairState) -> dict[str, Any]:
    return {
        "total_settled_usd": float(state.total_settled_usd),
        "orders": [
            {
                "local_id": int(o.local_id),
                "trade_id": str(o.trade_id),
                "cycle": int(o.cycle),
                "regime_at_entry": o.regime_at_entry,
            }
            for o in state.orders
        ],
        "recovery_orders": [
            {
                "recovery_id": int(r.recovery_id),
                "trade_id": str(r.trade_id),
                "cycle": int(r.cycle),
                "regime_at_entry": r.regime_at_entry,
            }
            for r in state.recovery_orders
        ],
        "completed_cycles": [
            {
                "trade_id": str(c.trade_id),
                "cycle": int(c.cycle),
                "entry_fee": float(c.entry_fee),
                "exit_fee": float(c.exit_fee),
                "quote_fee": float(c.quote_fee),
                "settled_usd": float(c.settled_usd),
                "regime_at_entry": c.regime_at_entry,
            }
            for c in state.completed_cycles
        ],
    }


def _shadow_actions_focus_payload(actions: list[Action]) -> list[dict[str, Any]]:
    focus: list[dict[str, Any]] = []
    for action in actions:
        if isinstance(action, BookCycleAction):
            focus.append(
                {
                    "kind": "BookCycleAction",
                    "trade_id": str(action.trade_id),
                    "cycle": int(action.cycle),
                    "fees": float(action.fees),
                    "settled_usd": float(action.settled_usd),
                    "from_recovery": bool(action.from_recovery),
                }
            )
    return focus


def transition(
    state: PairState,
    event: Event,
    cfg: EngineConfig,
    order_size_usd: float,
    order_sizes: dict[str, float] | None = None,
) -> tuple[PairState, list[Action]]:
    """
    Drop-in replacement for state_machine.transition().
    """
    payload = _transition_payload(state, event, cfg, order_size_usd, order_sizes)

    shadow_mode = _shadow_enabled() and _haskell_executable_available()
    if shadow_mode:
        _shadow_metric_inc("transition_checks")
        py_state, py_actions = _sm.transition(state, event, cfg, order_size_usd, order_sizes)
        try:
            response = _call_haskell("transition", payload)
            hs_state = _parse_state(response["state"])
            hs_actions = _parse_actions(response.get("actions", []))
            py_focus_state = _shadow_state_focus_payload(py_state)
            hs_focus_state = _shadow_state_focus_payload(hs_state)
            py_focus_actions = _shadow_actions_focus_payload(py_actions)
            hs_focus_actions = _shadow_actions_focus_payload(hs_actions)
            if (
                py_state != hs_state
                or py_actions != hs_actions
                or py_focus_state != hs_focus_state
                or py_focus_actions != hs_focus_actions
            ):
                _record_shadow_divergence("transition", type(event).__name__)
                logger.warning(
                    "Shadow divergence in transition for event=%s",
                    type(event).__name__,
                )
                logger.debug("shadow python state=%s", to_dict(py_state))
                logger.debug("shadow haskell state=%s", to_dict(hs_state))
                logger.debug("shadow python actions=%s", _actions_payload(py_actions))
                logger.debug("shadow haskell actions=%s", _actions_payload(hs_actions))
                logger.debug("shadow python focus state=%s", py_focus_state)
                logger.debug("shadow haskell focus state=%s", hs_focus_state)
                logger.debug("shadow python focus actions=%s", py_focus_actions)
                logger.debug("shadow haskell focus actions=%s", hs_focus_actions)
        except Exception as exc:
            _record_shadow_failure(exc)
            logger.warning("Haskell shadow transition failed: %s", exc)
        return py_state, py_actions

    if not _haskell_enabled():
        return _sm.transition(state, event, cfg, order_size_usd, order_sizes)

    try:
        response = _call_haskell("transition", payload)
        next_state = _parse_state(response["state"])
        actions = _parse_actions(response.get("actions", []))
        return next_state, actions
    except Exception as exc:
        logger.warning("Falling back to Python transition backend after Haskell failure: %s", exc)
        return _sm.transition(state, event, cfg, order_size_usd, order_sizes)


def check_invariants(state: PairState) -> list[str]:
    """
    Drop-in replacement for state_machine.check_invariants().
    """
    payload = {"state": _state_payload(state)}
    shadow_mode = _shadow_enabled() and _haskell_executable_available()
    if shadow_mode:
        _shadow_metric_inc("invariant_checks")
        py_violations = _sm.check_invariants(state)
        try:
            response = _call_haskell("check_invariants", payload)
            hs_violations = response.get("violations", [])
            if not isinstance(hs_violations, list):
                raise TypeError("violations payload must be a list")
            hs_rendered = [str(v) for v in hs_violations]
            if py_violations != hs_rendered:
                _record_shadow_divergence("invariant")
                logger.warning("Shadow divergence in check_invariants")
                logger.debug("shadow python violations=%s", py_violations)
                logger.debug("shadow haskell violations=%s", hs_rendered)
        except Exception as exc:
            _record_shadow_failure(exc)
            logger.warning("Haskell shadow invariant check failed: %s", exc)
        return py_violations

    if not _haskell_enabled():
        return _sm.check_invariants(state)

    try:
        response = _call_haskell("check_invariants", payload)
        violations = response.get("violations", [])
        if not isinstance(violations, list):
            raise TypeError("violations payload must be a list")
        return [str(v) for v in violations]
    except Exception as exc:
        logger.warning("Falling back to Python invariant backend after Haskell failure: %s", exc)
        return _sm.check_invariants(state)


def apply_order_regime_at_entry(
    state: PairState,
    local_id: int,
    regime_at_entry: int | None,
) -> PairState:
    """
    Patch helper that sets regime_at_entry for an in-flight order by local_id.
    """
    local_id_value = int(local_id)
    payload = {
        "params": {
            "state": _state_payload(state),
            "local_id": local_id_value,
            "regime_at_entry": regime_at_entry,
        }
    }

    shadow_mode = _shadow_enabled() and _haskell_executable_available()
    if shadow_mode:
        py_state = _sm.apply_order_regime_at_entry(state, local_id_value, regime_at_entry)
        try:
            response = _call_haskell("apply_order_regime_at_entry", payload)
            hs_state = _parse_state(response["state"])
            if py_state != hs_state:
                _record_shadow_divergence("transition", "apply_order_regime_at_entry")
                logger.warning("Shadow divergence in apply_order_regime_at_entry")
                logger.debug("shadow python state=%s", to_dict(py_state))
                logger.debug("shadow haskell state=%s", to_dict(hs_state))
                logger.debug("shadow python focus state=%s", _shadow_state_focus_payload(py_state))
                logger.debug("shadow haskell focus state=%s", _shadow_state_focus_payload(hs_state))
        except Exception as exc:
            _record_shadow_failure(exc)
            logger.warning("Haskell shadow apply_order_regime_at_entry failed: %s", exc)
        return py_state

    if not _haskell_enabled():
        return _sm.apply_order_regime_at_entry(state, local_id_value, regime_at_entry)

    try:
        response = _call_haskell("apply_order_regime_at_entry", payload)
        return _parse_state(response["state"])
    except Exception as exc:
        logger.warning(
            "Falling back to Python apply_order_regime_at_entry backend after Haskell failure: %s",
            exc,
        )
        return _sm.apply_order_regime_at_entry(state, local_id_value, regime_at_entry)


__all__ = [
    "Side",
    "Role",
    "TradeId",
    "PairPhase",
    "EngineConfig",
    "OrderState",
    "RecoveryOrder",
    "CycleRecord",
    "PairState",
    "PriceTick",
    "TimerTick",
    "FillEvent",
    "RecoveryFillEvent",
    "RecoveryCancelEvent",
    "Event",
    "PlaceOrderAction",
    "CancelOrderAction",
    "OrphanOrderAction",
    "BookCycleAction",
    "Action",
    "derive_phase",
    "compute_order_volume",
    "bootstrap_orders",
    "transition",
    "check_invariants",
    "get_shadow_metrics",
    "reset_shadow_metrics",
    "add_entry_order",
    "apply_order_regime_at_entry",
    "apply_order_txid",
    "remove_order",
    "remove_recovery",
    "find_order",
    "to_dict",
    "from_dict",
]
