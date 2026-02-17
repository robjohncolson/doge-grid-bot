"""
position_ledger.py -- Local position/journal ledger for self-healing slots.

The ledger is local-first:
- `position_ledger` keeps current position state.
- `position_journal` is append-only event history.

All subsidy balances are derived from journal events.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import time
from typing import Any


_VALID_TRADE_IDS = {"A", "B"}
_VALID_SLOT_MODES = {"legacy", "sticky", "churner"}
_VALID_STATUS = {"open", "closed"}
_VALID_REPRICE_REASONS = {"tighten", "subsidy", "operator"}


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _norm_trade_id(value: Any) -> str:
    trade_id = str(value or "").strip().upper()
    return trade_id if trade_id in _VALID_TRADE_IDS else "A"


def _norm_slot_mode(value: Any) -> str:
    slot_mode = str(value or "").strip().lower()
    return slot_mode if slot_mode in _VALID_SLOT_MODES else "legacy"


def _norm_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    return status if status in _VALID_STATUS else "open"


@dataclass
class PositionRecord:
    # Identity
    position_id: int
    slot_id: int
    trade_id: str
    slot_mode: str
    cycle: int
    # Entry context (immutable)
    entry_price: float
    entry_cost: float
    entry_fee: float
    entry_volume: float
    entry_time: float
    entry_regime: str
    entry_volatility: float
    # Exit intent (mutable)
    current_exit_price: float
    original_exit_price: float
    target_profit_pct: float
    exit_txid: str
    # Outcome (write once on close)
    exit_price: float | None = None
    exit_cost: float | None = None
    exit_fee: float | None = None
    exit_time: float | None = None
    exit_regime: str | None = None
    net_profit: float | None = None
    close_reason: str | None = None
    # Status
    status: str = "open"
    times_repriced: int = 0


@dataclass
class JournalRecord:
    journal_id: int
    position_id: int
    timestamp: float
    event_type: str
    details: dict[str, Any]


class PositionLedger:
    def __init__(
        self,
        *,
        enabled: bool = True,
        journal_local_limit: int = 500,
    ) -> None:
        self.enabled = bool(enabled)
        self.journal_local_limit = max(50, int(journal_local_limit))

        self._positions: dict[int, PositionRecord] = {}
        self._journal: list[JournalRecord] = []

        self._next_position_id: int = 1
        self._next_journal_id: int = 1

        # Watermarks preserve lifetime subsidy totals when local journal rows are trimmed.
        self._subsidy_earned_watermark_by_slot: dict[int, float] = {}
        self._subsidy_consumed_watermark_by_slot: dict[int, float] = {}

    # ------------------ Core API ------------------

    def open_position(
        self,
        slot_id: int,
        trade_id: str,
        slot_mode: str,
        cycle: int,
        entry_data: dict[str, Any],
        exit_data: dict[str, Any],
        *,
        position_id: int | None = None,
    ) -> int:
        if not self.enabled:
            return 0

        pid = int(position_id) if position_id is not None else self._next_position_id
        if pid in self._positions:
            raise ValueError(f"position_id {pid} already exists")

        current_exit = _to_float(exit_data.get("current_exit_price"))
        original_exit = _to_float(exit_data.get("original_exit_price"), current_exit)

        rec = PositionRecord(
            position_id=pid,
            slot_id=max(0, int(slot_id)),
            trade_id=_norm_trade_id(trade_id),
            slot_mode=_norm_slot_mode(slot_mode),
            cycle=max(0, int(cycle)),
            entry_price=_to_float(entry_data.get("entry_price")),
            entry_cost=_to_float(entry_data.get("entry_cost")),
            entry_fee=max(0.0, _to_float(entry_data.get("entry_fee"))),
            entry_volume=max(0.0, _to_float(entry_data.get("entry_volume"))),
            entry_time=_to_float(entry_data.get("entry_time")),
            entry_regime=str(entry_data.get("entry_regime") or ""),
            entry_volatility=max(0.0, _to_float(entry_data.get("entry_volatility"))),
            current_exit_price=current_exit,
            original_exit_price=original_exit,
            target_profit_pct=_to_float(exit_data.get("target_profit_pct")),
            exit_txid=str(exit_data.get("exit_txid") or ""),
            status="open",
            times_repriced=max(0, _to_int(exit_data.get("times_repriced"), 0)),
        )

        self._positions[pid] = rec
        if position_id is None:
            self._next_position_id += 1
        else:
            self._next_position_id = max(self._next_position_id, int(position_id) + 1)
        return pid

    def journal_event(
        self,
        position_id: int,
        event_type: str,
        details: dict[str, Any] | None,
        *,
        timestamp: float | None = None,
        journal_id: int | None = None,
    ) -> int:
        if not self.enabled:
            return 0
        if int(position_id) not in self._positions:
            raise ValueError(f"unknown position_id {position_id}")

        jid = int(journal_id) if journal_id is not None else self._next_journal_id
        row = JournalRecord(
            journal_id=jid,
            position_id=int(position_id),
            timestamp=_to_float(timestamp, time.time()),
            event_type=str(event_type or "").strip(),
            details=dict(details or {}),
        )
        self._journal.append(row)
        if journal_id is None:
            self._next_journal_id += 1
        else:
            self._next_journal_id = max(self._next_journal_id, int(journal_id) + 1)

        self._trim_journal_if_needed()
        return jid

    def close_position(
        self,
        position_id: int,
        outcome_data: dict[str, Any],
    ) -> None:
        if not self.enabled:
            return
        rec = self._positions.get(int(position_id))
        if rec is None:
            raise ValueError(f"unknown position_id {position_id}")
        if rec.status == "closed":
            return

        close_reason = str(outcome_data.get("close_reason") or "filled").strip().lower()
        rec.exit_price = _to_float(outcome_data.get("exit_price"))
        rec.exit_cost = _to_float(outcome_data.get("exit_cost"))
        rec.exit_fee = max(0.0, _to_float(outcome_data.get("exit_fee")))
        rec.exit_time = _to_float(outcome_data.get("exit_time"), time.time())
        rec.exit_regime = str(outcome_data.get("exit_regime") or "")
        rec.net_profit = _to_float(outcome_data.get("net_profit"))
        rec.close_reason = close_reason
        rec.status = "closed"

        if close_reason == "filled":
            self.journal_event(
                rec.position_id,
                "filled",
                {
                    "fill_price": float(rec.exit_price or 0.0),
                    "fill_cost": float(rec.exit_cost or 0.0),
                    "fill_fee": float(rec.exit_fee or 0.0),
                    "net_profit": float(rec.net_profit or 0.0),
                },
                timestamp=float(rec.exit_time or time.time()),
            )
        elif close_reason == "cancelled":
            self.journal_event(
                rec.position_id,
                "cancelled",
                {
                    "reason": str(outcome_data.get("reason") or "cancelled"),
                    "age_seconds": _to_float(outcome_data.get("age_seconds")),
                },
                timestamp=float(rec.exit_time or time.time()),
            )
        else:
            self.journal_event(
                rec.position_id,
                "written_off",
                {
                    "close_price": float(rec.exit_price or 0.0),
                    "realized_loss": max(0.0, -float(rec.net_profit or 0.0)),
                    "reason": str(outcome_data.get("reason") or close_reason),
                },
                timestamp=float(rec.exit_time or time.time()),
            )

    # ------------------ Runtime helpers ------------------

    def bind_exit_txid(self, position_id: int, txid: str) -> None:
        rec = self._positions.get(int(position_id))
        if rec is None or rec.status != "open":
            return
        rec.exit_txid = str(txid or "")

    def reprice_position(
        self,
        position_id: int,
        *,
        new_exit_price: float,
        new_exit_txid: str,
        reason: str,
        subsidy_consumed: float = 0.0,
        timestamp: float | None = None,
        old_txid_override: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        rec = self._positions.get(int(position_id))
        if rec is None:
            raise ValueError(f"unknown position_id {position_id}")
        if rec.status != "open":
            return

        r = str(reason or "").strip().lower()
        if r not in _VALID_REPRICE_REASONS:
            r = "operator"

        old_price = float(rec.current_exit_price)
        old_txid = str(old_txid_override if old_txid_override is not None else rec.exit_txid)
        rec.current_exit_price = float(new_exit_price)
        rec.exit_txid = str(new_exit_txid or "")
        rec.times_repriced += 1

        self.journal_event(
            rec.position_id,
            "repriced",
            {
                "old_price": old_price,
                "new_price": float(new_exit_price),
                "old_txid": old_txid,
                "new_txid": rec.exit_txid,
                "reason": r,
                "subsidy_consumed": max(0.0, float(subsidy_consumed)),
            },
            timestamp=_to_float(timestamp, time.time()),
        )

    # ------------------ Queries ------------------

    def get_position(self, position_id: int) -> dict[str, Any] | None:
        rec = self._positions.get(int(position_id))
        return asdict(rec) if rec is not None else None

    def get_open_positions(self, slot_id: int | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        sid = int(slot_id) if slot_id is not None else None
        for rec in self._positions.values():
            if rec.status != "open":
                continue
            if sid is not None and rec.slot_id != sid:
                continue
            rows.append(asdict(rec))
        rows.sort(key=lambda r: (float(r.get("entry_time", 0.0) or 0.0), int(r.get("position_id", 0))))
        return rows

    def get_position_history(self, slot_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sid = int(slot_id) if slot_id is not None else None
        rows: list[dict[str, Any]] = []
        for rec in self._positions.values():
            if rec.status != "closed":
                continue
            if sid is not None and rec.slot_id != sid:
                continue
            rows.append(asdict(rec))
        rows.sort(
            key=lambda r: (
                float(r.get("exit_time", 0.0) or 0.0),
                int(r.get("position_id", 0)),
            ),
            reverse=True,
        )
        return rows[: max(1, int(limit))]

    def get_journal(self, position_id: int | None = None) -> list[dict[str, Any]]:
        pid = int(position_id) if position_id is not None else None
        rows: list[dict[str, Any]] = []
        for row in self._journal:
            if pid is not None and int(row.position_id) != pid:
                continue
            rows.append(asdict(row))
        return rows

    def get_subsidy_balance(self, slot_id: int) -> float:
        sid = int(slot_id)
        earned, consumed = self._subsidy_totals_for_slot(sid)
        return max(0.0, earned - consumed)

    def get_subsidy_totals(self, slot_id: int | None = None) -> dict[str, float]:
        if slot_id is not None:
            sid = int(slot_id)
            earned, consumed = self._subsidy_totals_for_slot(sid)
            return {
                "earned": float(earned),
                "consumed": float(consumed),
                "balance": max(0.0, float(earned - consumed)),
            }

        earned = 0.0
        consumed = 0.0
        slots = {p.slot_id for p in self._positions.values()}
        slots.update(self._subsidy_earned_watermark_by_slot.keys())
        slots.update(self._subsidy_consumed_watermark_by_slot.keys())
        for sid in slots:
            e, c = self._subsidy_totals_for_slot(int(sid))
            earned += e
            consumed += c
        return {
            "earned": float(earned),
            "consumed": float(consumed),
            "balance": max(0.0, float(earned - consumed)),
        }

    # ------------------ Snapshot ------------------

    def snapshot_state(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "position_ledger": [asdict(r) for r in self._positions.values()],
            "position_journal_recent": [asdict(r) for r in self._journal],
            "position_id_counter": int(self._next_position_id),
            "journal_id_counter": int(self._next_journal_id),
            "subsidy_earned_watermark_by_slot": {
                str(k): float(v) for k, v in self._subsidy_earned_watermark_by_slot.items()
            },
            "subsidy_consumed_watermark_by_slot": {
                str(k): float(v) for k, v in self._subsidy_consumed_watermark_by_slot.items()
            },
        }

    def restore_state(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        self.enabled = bool(payload.get("enabled", self.enabled))

        self._positions = {}
        raw_positions = payload.get("position_ledger", [])
        if isinstance(raw_positions, list):
            for row in raw_positions:
                if not isinstance(row, dict):
                    continue
                try:
                    rec = PositionRecord(
                        position_id=max(1, _to_int(row.get("position_id"), 0)),
                        slot_id=max(0, _to_int(row.get("slot_id"), 0)),
                        trade_id=_norm_trade_id(row.get("trade_id")),
                        slot_mode=_norm_slot_mode(row.get("slot_mode")),
                        cycle=max(0, _to_int(row.get("cycle"), 0)),
                        entry_price=_to_float(row.get("entry_price")),
                        entry_cost=_to_float(row.get("entry_cost")),
                        entry_fee=max(0.0, _to_float(row.get("entry_fee"))),
                        entry_volume=max(0.0, _to_float(row.get("entry_volume"))),
                        entry_time=_to_float(row.get("entry_time")),
                        entry_regime=str(row.get("entry_regime") or ""),
                        entry_volatility=max(0.0, _to_float(row.get("entry_volatility"))),
                        current_exit_price=_to_float(row.get("current_exit_price")),
                        original_exit_price=_to_float(
                            row.get("original_exit_price"),
                            _to_float(row.get("current_exit_price")),
                        ),
                        target_profit_pct=_to_float(row.get("target_profit_pct")),
                        exit_txid=str(row.get("exit_txid") or ""),
                        exit_price=(
                            None
                            if row.get("exit_price", None) is None
                            else _to_float(row.get("exit_price"))
                        ),
                        exit_cost=(
                            None
                            if row.get("exit_cost", None) is None
                            else _to_float(row.get("exit_cost"))
                        ),
                        exit_fee=(
                            None
                            if row.get("exit_fee", None) is None
                            else max(0.0, _to_float(row.get("exit_fee")))
                        ),
                        exit_time=(
                            None
                            if row.get("exit_time", None) is None
                            else _to_float(row.get("exit_time"))
                        ),
                        exit_regime=(
                            None
                            if row.get("exit_regime", None) is None
                            else str(row.get("exit_regime") or "")
                        ),
                        net_profit=(
                            None
                            if row.get("net_profit", None) is None
                            else _to_float(row.get("net_profit"))
                        ),
                        close_reason=(
                            None
                            if row.get("close_reason", None) is None
                            else str(row.get("close_reason") or "")
                        ),
                        status=_norm_status(row.get("status")),
                        times_repriced=max(0, _to_int(row.get("times_repriced"), 0)),
                    )
                except Exception:
                    continue
                self._positions[rec.position_id] = rec

        self._journal = []
        raw_journal = payload.get("position_journal_recent", [])
        if isinstance(raw_journal, list):
            for row in raw_journal:
                if not isinstance(row, dict):
                    continue
                try:
                    rec = JournalRecord(
                        journal_id=max(1, _to_int(row.get("journal_id"), 0)),
                        position_id=max(1, _to_int(row.get("position_id"), 0)),
                        timestamp=_to_float(row.get("timestamp")),
                        event_type=str(row.get("event_type") or ""),
                        details=dict(row.get("details") or {}),
                    )
                except Exception:
                    continue
                if rec.position_id not in self._positions:
                    continue
                self._journal.append(rec)

        raw_pid = payload.get("position_id_counter", 1)
        raw_jid = payload.get("journal_id_counter", 1)
        self._next_position_id = max(max(self._positions.keys(), default=0) + 1, _to_int(raw_pid, 1))
        self._next_journal_id = max(max((j.journal_id for j in self._journal), default=0) + 1, _to_int(raw_jid, 1))

        self._subsidy_earned_watermark_by_slot = {}
        raw_earned = payload.get("subsidy_earned_watermark_by_slot", {})
        if isinstance(raw_earned, dict):
            for k, v in raw_earned.items():
                sid = _to_int(k, -1)
                if sid >= 0:
                    self._subsidy_earned_watermark_by_slot[sid] = max(0.0, _to_float(v))

        self._subsidy_consumed_watermark_by_slot = {}
        raw_consumed = payload.get("subsidy_consumed_watermark_by_slot", {})
        if isinstance(raw_consumed, dict):
            for k, v in raw_consumed.items():
                sid = _to_int(k, -1)
                if sid >= 0:
                    self._subsidy_consumed_watermark_by_slot[sid] = max(0.0, _to_float(v))

        self._trim_journal_if_needed()

    # ------------------ Internals ------------------

    def _subsidy_totals_for_slot(self, slot_id: int) -> tuple[float, float]:
        sid = int(slot_id)
        earned = float(self._subsidy_earned_watermark_by_slot.get(sid, 0.0))
        consumed = float(self._subsidy_consumed_watermark_by_slot.get(sid, 0.0))

        for row in self._journal:
            pos = self._positions.get(int(row.position_id))
            if pos is None or int(pos.slot_id) != sid:
                continue
            if row.event_type == "churner_profit":
                earned += max(0.0, _to_float(row.details.get("net_profit")))
            elif row.event_type == "over_performance":
                if "excess" in row.details:
                    earned += max(0.0, _to_float(row.details.get("excess")))
                else:
                    earned += max(0.0, _to_float(row.details.get("net_profit")))
            elif row.event_type == "repriced":
                reason = str(row.details.get("reason") or "").strip().lower()
                if reason == "subsidy":
                    consumed += max(0.0, _to_float(row.details.get("subsidy_consumed")))

        return float(earned), float(consumed)

    def _trim_journal_if_needed(self) -> None:
        limit = max(50, int(self.journal_local_limit))
        if len(self._journal) <= limit:
            return

        trim_n = len(self._journal) - limit
        removed = self._journal[:trim_n]
        self._journal = self._journal[trim_n:]

        for row in removed:
            pos = self._positions.get(int(row.position_id))
            if pos is None:
                continue
            sid = int(pos.slot_id)
            if row.event_type == "churner_profit":
                earned = max(0.0, _to_float(row.details.get("net_profit")))
                self._subsidy_earned_watermark_by_slot[sid] = (
                    float(self._subsidy_earned_watermark_by_slot.get(sid, 0.0)) + earned
                )
            elif row.event_type == "over_performance":
                if "excess" in row.details:
                    earned = max(0.0, _to_float(row.details.get("excess")))
                else:
                    earned = max(0.0, _to_float(row.details.get("net_profit")))
                self._subsidy_earned_watermark_by_slot[sid] = (
                    float(self._subsidy_earned_watermark_by_slot.get(sid, 0.0)) + earned
                )
            elif row.event_type == "repriced":
                reason = str(row.details.get("reason") or "").strip().lower()
                if reason == "subsidy":
                    consumed = max(0.0, _to_float(row.details.get("subsidy_consumed")))
                    self._subsidy_consumed_watermark_by_slot[sid] = (
                        float(self._subsidy_consumed_watermark_by_slot.get(sid, 0.0)) + consumed
                    )

