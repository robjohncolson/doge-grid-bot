from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import os
import unittest
from unittest import mock

import doge_core as dc
import state_machine as sm


class DogeCoreAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        if hasattr(dc, "reset_shadow_metrics"):
            dc.reset_shadow_metrics()

    def _build_book_cycle_input(self) -> tuple[sm.EngineConfig, sm.PairState, sm.FillEvent]:
        cfg = sm.EngineConfig()
        st = sm.PairState(market_price=0.1, now=1000.0)
        st, _ = sm.add_entry_order(st, cfg, side="sell", trade_id="A", cycle=1, order_size_usd=2.0)
        entry_order = st.orders[0]
        entry_fill = sm.FillEvent(
            order_local_id=entry_order.local_id,
            txid="entry-fill",
            side=entry_order.side,
            price=entry_order.price,
            volume=entry_order.volume,
            fee=0.01,
            timestamp=1001.0,
        )
        st_after_entry, _ = sm.transition(st, entry_fill, cfg, order_size_usd=2.0)
        exit_order = next(o for o in st_after_entry.orders if o.role == "exit")
        exit_fill = sm.FillEvent(
            order_local_id=exit_order.local_id,
            txid="exit-fill",
            side=exit_order.side,
            price=exit_order.price,
            volume=exit_order.volume,
            fee=0.02,
            timestamp=1002.0,
        )
        return cfg, st_after_entry, exit_fill

    def test_exports_expected_surface(self) -> None:
        required = [
            "PairState",
            "EngineConfig",
            "transition",
            "check_invariants",
            "derive_phase",
            "compute_order_volume",
            "add_entry_order",
            "find_order",
            "remove_order",
            "remove_recovery",
            "apply_order_txid",
            "apply_order_regime_at_entry",
            "bootstrap_orders",
            "to_dict",
            "from_dict",
            "PriceTick",
            "TimerTick",
            "FillEvent",
            "RecoveryFillEvent",
            "RecoveryCancelEvent",
            "PlaceOrderAction",
            "CancelOrderAction",
            "OrphanOrderAction",
            "BookCycleAction",
        ]
        for name in required:
            self.assertTrue(hasattr(dc, name), name)

    def test_transition_matches_python_backend(self) -> None:
        cfg = sm.EngineConfig()
        st = sm.PairState(market_price=0.1, now=1000.0)
        st, _ = sm.add_entry_order(st, cfg, side="sell", trade_id="A", cycle=1, order_size_usd=2.0)
        st, _ = sm.add_entry_order(st, cfg, side="buy", trade_id="B", cycle=1, order_size_usd=2.0)
        ev = sm.PriceTick(price=0.1, timestamp=1001.0)

        py_state, py_actions = sm.transition(st, ev, cfg, order_size_usd=2.0)
        dc_state, dc_actions = dc.transition(st, ev, cfg, order_size_usd=2.0)

        self.assertEqual(py_state, dc_state)
        self.assertEqual(py_actions, dc_actions)

    def test_apply_order_regime_at_entry_matches_python_backend(self) -> None:
        cfg = sm.EngineConfig()
        st = sm.PairState(market_price=0.1, now=1000.0)
        st, _ = sm.add_entry_order(st, cfg, side="sell", trade_id="A", cycle=1, order_size_usd=2.0)
        local_id = st.orders[0].local_id

        py_state = sm.apply_order_regime_at_entry(st, local_id, 2)
        dc_state = dc.apply_order_regime_at_entry(st, local_id, 2)

        self.assertEqual(py_state, dc_state)
        self.assertEqual(dc_state.orders[0].regime_at_entry, 2)

    def test_apply_order_regime_at_entry_routes_haskell_rpc(self) -> None:
        cfg = sm.EngineConfig()
        st = sm.PairState(market_price=0.1, now=1000.0)
        st, _ = sm.add_entry_order(st, cfg, side="sell", trade_id="A", cycle=1, order_size_usd=2.0)
        local_id = st.orders[0].local_id
        expected = sm.apply_order_regime_at_entry(st, local_id, 1)

        with mock.patch.dict(
            os.environ,
            {"DOGE_CORE_BACKEND": "haskell"},
            clear=False,
        ):
            with mock.patch.object(dc, "_haskell_executable_available", return_value=True):
                with mock.patch.object(dc, "_call_haskell", return_value={"state": sm.to_dict(expected)}) as call_hs:
                    state = dc.apply_order_regime_at_entry(st, local_id, 1)

        self.assertEqual(state, expected)
        call_hs.assert_called_once()
        method = call_hs.call_args.args[0]
        payload = call_hs.call_args.args[1]
        self.assertEqual(method, "apply_order_regime_at_entry")
        self.assertEqual(payload["params"]["state"], sm.to_dict(st))
        self.assertEqual(payload["params"]["local_id"], local_id)
        self.assertEqual(payload["params"]["regime_at_entry"], 1)

    def test_shadow_apply_order_regime_at_entry_returns_python_authoritative_result(self) -> None:
        cfg = sm.EngineConfig()
        st = sm.PairState(market_price=0.1, now=1000.0)
        st, _ = sm.add_entry_order(st, cfg, side="sell", trade_id="A", cycle=1, order_size_usd=2.0)
        local_id = st.orders[0].local_id
        py_state = sm.apply_order_regime_at_entry(st, local_id, 2)
        hs_state = deepcopy(sm.to_dict(py_state))
        hs_state["orders"][0]["regime_at_entry"] = 1

        with mock.patch.dict(
            os.environ,
            {"DOGE_CORE_SHADOW": "1", "DOGE_CORE_BACKEND": "python"},
            clear=False,
        ):
            with mock.patch.object(dc, "_haskell_executable_available", return_value=True):
                with mock.patch.object(dc, "_call_haskell", return_value={"state": hs_state}):
                    with mock.patch.object(dc.logger, "warning") as warn:
                        dc_state = dc.apply_order_regime_at_entry(st, local_id, 2)

        self.assertEqual(py_state, dc_state)
        self.assertTrue(
            any(
                "Shadow divergence in apply_order_regime_at_entry" in str(c.args[0])
                for c in warn.call_args_list
            )
        )
        metrics = dc.get_shadow_metrics()
        self.assertEqual(metrics["transition_divergences"], 1)
        self.assertEqual(metrics["total_divergences"], 1)
        self.assertEqual(metrics["last_divergence_kind"], "transition")
        self.assertEqual(metrics["last_divergence_event"], "apply_order_regime_at_entry")

    def test_check_invariants_matches_python_backend(self) -> None:
        st = sm.PairState(market_price=0.1, now=1000.0)
        self.assertEqual(sm.check_invariants(st), dc.check_invariants(st))

    def test_forced_python_backend_env(self) -> None:
        previous = os.environ.get("DOGE_CORE_BACKEND")
        try:
            os.environ["DOGE_CORE_BACKEND"] = "python"
            cfg = sm.EngineConfig()
            st = sm.PairState(market_price=0.1, now=1000.0)
            ev = sm.TimerTick(timestamp=1001.0)
            state, actions = dc.transition(st, ev, cfg, order_size_usd=2.0)
            self.assertIsInstance(state, sm.PairState)
            self.assertIsInstance(actions, list)
        finally:
            if previous is None:
                os.environ.pop("DOGE_CORE_BACKEND", None)
            else:
                os.environ["DOGE_CORE_BACKEND"] = previous

    def test_shadow_transition_returns_python_authoritative_result(self) -> None:
        cfg = sm.EngineConfig()
        st = sm.PairState(market_price=0.1, now=1000.0)
        ev = sm.TimerTick(timestamp=1001.0)
        py_state, py_actions = sm.transition(st, ev, cfg, order_size_usd=2.0)
        hs_response = {
            "state": sm.to_dict(py_state),
            "actions": [asdict(a) for a in py_actions],
        }

        with mock.patch.dict(
            os.environ,
            {"DOGE_CORE_SHADOW": "1", "DOGE_CORE_BACKEND": "python"},
            clear=False,
        ):
            with mock.patch.object(dc, "_haskell_executable_available", return_value=True):
                with mock.patch.object(dc, "_call_haskell", return_value=hs_response) as call_hs:
                    dc_state, dc_actions = dc.transition(st, ev, cfg, order_size_usd=2.0)

        self.assertEqual(py_state, dc_state)
        self.assertEqual(py_actions, dc_actions)
        call_hs.assert_called_once()

    def test_shadow_transition_logs_divergence(self) -> None:
        cfg = sm.EngineConfig()
        st = sm.PairState(market_price=0.1, now=1000.0)
        ev = sm.TimerTick(timestamp=1001.0)
        py_state, py_actions = sm.transition(st, ev, cfg, order_size_usd=2.0)
        hs_state = sm.to_dict(py_state)
        hs_state["now"] = float(hs_state["now"]) + 1.0
        hs_response = {
            "state": hs_state,
            "actions": [asdict(a) for a in py_actions],
        }

        with mock.patch.dict(
            os.environ,
            {"DOGE_CORE_SHADOW": "1", "DOGE_CORE_BACKEND": "python"},
            clear=False,
        ):
            with mock.patch.object(dc, "_haskell_executable_available", return_value=True):
                with mock.patch.object(dc, "_call_haskell", return_value=hs_response):
                    with mock.patch.object(dc.logger, "warning") as warn:
                        dc_state, dc_actions = dc.transition(st, ev, cfg, order_size_usd=2.0)

        self.assertEqual(py_state, dc_state)
        self.assertEqual(py_actions, dc_actions)
        self.assertTrue(
            any("Shadow divergence in transition" in str(c.args[0]) for c in warn.call_args_list)
        )

    def test_shadow_transition_logs_divergence_for_settled_usd(self) -> None:
        cfg, st, ev = self._build_book_cycle_input()
        py_state, py_actions = sm.transition(st, ev, cfg, order_size_usd=2.0)
        self.assertTrue(any(isinstance(a, sm.BookCycleAction) for a in py_actions))

        hs_actions = [asdict(a) for a in py_actions]
        for action in hs_actions:
            if {"trade_id", "cycle", "net_profit", "gross_profit", "fees"}.issubset(action):
                action["settled_usd"] = float(action.get("settled_usd", 0.0)) + 1.0
        hs_response = {
            "state": sm.to_dict(py_state),
            "actions": hs_actions,
        }

        with mock.patch.dict(
            os.environ,
            {"DOGE_CORE_SHADOW": "1", "DOGE_CORE_BACKEND": "python"},
            clear=False,
        ):
            with mock.patch.object(dc, "_haskell_executable_available", return_value=True):
                with mock.patch.object(dc, "_call_haskell", return_value=hs_response):
                    with mock.patch.object(dc.logger, "warning") as warn:
                        dc_state, dc_actions = dc.transition(st, ev, cfg, order_size_usd=2.0)

        self.assertEqual(py_state, dc_state)
        self.assertEqual(py_actions, dc_actions)
        self.assertTrue(
            any("Shadow divergence in transition" in str(c.args[0]) for c in warn.call_args_list)
        )
        metrics = dc.get_shadow_metrics()
        self.assertEqual(metrics["transition_checks"], 1)
        self.assertEqual(metrics["transition_divergences"], 1)
        self.assertEqual(metrics["total_divergences"], 1)

    def test_shadow_check_invariants_returns_python_result(self) -> None:
        st = sm.PairState(market_price=0.1, now=1000.0)

        with mock.patch.dict(
            os.environ,
            {"DOGE_CORE_SHADOW": "1", "DOGE_CORE_BACKEND": "python"},
            clear=False,
        ):
            with mock.patch.object(dc, "_haskell_executable_available", return_value=True):
                with mock.patch.object(dc._sm, "check_invariants", return_value=["py_violation"]):
                    with mock.patch.object(dc, "_call_haskell", return_value={"violations": ["hs_violation"]}):
                        with mock.patch.object(dc.logger, "warning") as warn:
                            result = dc.check_invariants(st)

        self.assertEqual(result, ["py_violation"])
        self.assertTrue(
            any("Shadow divergence in check_invariants" in str(c.args[0]) for c in warn.call_args_list)
        )

    def test_shadow_metrics_increment_on_divergence(self) -> None:
        cfg = sm.EngineConfig()
        st = sm.PairState(market_price=0.1, now=1000.0)
        ev = sm.TimerTick(timestamp=1001.0)
        py_state, py_actions = sm.transition(st, ev, cfg, order_size_usd=2.0)

        hs_state = sm.to_dict(py_state)
        hs_state["now"] = float(hs_state["now"]) + 1.0
        hs_response = {
            "state": hs_state,
            "actions": [asdict(a) for a in py_actions],
        }

        with mock.patch.dict(
            os.environ,
            {"DOGE_CORE_SHADOW": "1", "DOGE_CORE_BACKEND": "python"},
            clear=False,
        ):
            with mock.patch.object(dc, "_haskell_executable_available", return_value=True):
                with mock.patch.object(dc, "_call_haskell", return_value=hs_response):
                    dc.transition(st, ev, cfg, order_size_usd=2.0)

        metrics = dc.get_shadow_metrics()
        self.assertEqual(metrics["transition_checks"], 1)
        self.assertEqual(metrics["transition_divergences"], 1)
        self.assertEqual(metrics["total_divergences"], 1)
        self.assertEqual(metrics["last_divergence_kind"], "transition")
        self.assertEqual(metrics["last_divergence_event"], "TimerTick")

    def test_from_dict_round_trip_preserves_phase4_fields(self) -> None:
        st = sm.PairState(
            market_price=0.1,
            now=1000.0,
            orders=(
                sm.OrderState(
                    local_id=1,
                    side="sell",
                    role="entry",
                    price=0.101,
                    volume=20.0,
                    trade_id="A",
                    cycle=1,
                    regime_at_entry=2,
                ),
            ),
            recovery_orders=(
                sm.RecoveryOrder(
                    recovery_id=1,
                    side="buy",
                    price=0.099,
                    volume=20.0,
                    trade_id="B",
                    cycle=1,
                    entry_price=0.1,
                    orphaned_at=995.0,
                    regime_at_entry=1,
                ),
            ),
            completed_cycles=(
                sm.CycleRecord(
                    trade_id="A",
                    cycle=1,
                    entry_price=0.102,
                    exit_price=0.101,
                    volume=20.0,
                    gross_profit=0.02,
                    fees=0.03,
                    net_profit=-0.01,
                    entry_fee=0.01,
                    exit_fee=0.02,
                    quote_fee=0.01,
                    settled_usd=0.01,
                    regime_at_entry=2,
                ),
            ),
            total_profit=-0.01,
            total_settled_usd=0.01,
            total_fees=0.03,
            total_round_trips=1,
        )

        restored = dc.from_dict(dc.to_dict(st))
        self.assertEqual(restored, st)

    def test_from_dict_defaults_total_settled_usd_to_total_profit(self) -> None:
        raw = sm.to_dict(sm.PairState(market_price=0.1, now=1000.0, total_profit=12.5))
        raw.pop("total_settled_usd", None)

        restored = dc.from_dict(raw)
        self.assertEqual(restored.total_settled_usd, 12.5)


if __name__ == "__main__":
    unittest.main()
