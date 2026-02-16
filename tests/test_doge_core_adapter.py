from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
