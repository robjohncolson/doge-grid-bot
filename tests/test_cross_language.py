from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import doge_core as dc
import state_machine as sm


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "cross_language"
ROOT_DIR = Path(__file__).resolve().parent.parent
STUB_EXE = ROOT_DIR / "doge-core" / "stub" / ("doge-core-exe.cmd" if sys.platform == "win32" else "doge-core-exe")

EVENT_BUILDERS = {
    "PriceTick": sm.PriceTick,
    "TimerTick": sm.TimerTick,
    "FillEvent": sm.FillEvent,
    "RecoveryFillEvent": sm.RecoveryFillEvent,
    "RecoveryCancelEvent": sm.RecoveryCancelEvent,
}


def _load_fixture(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _build_event(raw: dict) -> sm.Event:
    event_type = str(raw["type"])
    payload = raw["payload"]
    builder = EVENT_BUILDERS.get(event_type)
    if builder is None:
        raise ValueError(f"Unknown event type: {event_type}")
    return builder(**payload)


def _fixture_paths() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.json"))


class CrossLanguageParityTests(unittest.TestCase):
    def test_fixtures_exist(self) -> None:
        paths = _fixture_paths()
        self.assertGreaterEqual(len(paths), 12)

    def _run_parity_with_fixtures(self) -> None:
        for path in _fixture_paths():
            fixture = _load_fixture(path)
            name = fixture.get("name", path.stem)
            with self.subTest(fixture=name):
                cfg = sm.EngineConfig(**fixture["config"])
                py_state = sm.from_dict(fixture["initial_state"])
                dc_state = sm.from_dict(fixture["initial_state"])
                order_size_usd = float(fixture["order_size_usd"])
                raw_order_sizes = fixture.get("order_sizes")
                order_sizes = None
                if isinstance(raw_order_sizes, dict):
                    order_sizes = {str(k): float(v) for k, v in raw_order_sizes.items()}

                events = [_build_event(raw) for raw in fixture["events"]]
                for idx, event in enumerate(events):
                    py_state, py_actions = sm.transition(
                        py_state,
                        event,
                        cfg,
                        order_size_usd=order_size_usd,
                        order_sizes=order_sizes,
                    )
                    dc_state, dc_actions = dc.transition(
                        dc_state,
                        event,
                        cfg,
                        order_size_usd=order_size_usd,
                        order_sizes=order_sizes,
                    )

                    self.assertEqual(
                        py_state,
                        dc_state,
                        f"{name}: state diverged at event index {idx}",
                    )
                    self.assertEqual(
                        py_actions,
                        dc_actions,
                        f"{name}: actions diverged at event index {idx}",
                    )
                    self.assertEqual(
                        sm.check_invariants(py_state),
                        dc.check_invariants(dc_state),
                        f"{name}: invariant results diverged at event index {idx}",
                    )

                expected = fixture.get("expected", {})
                if "phase" in expected:
                    self.assertEqual(sm.derive_phase(py_state), expected["phase"], name)
                if "open_orders" in expected:
                    self.assertEqual(len(py_state.orders), int(expected["open_orders"]), name)
                if "recovery_orders" in expected:
                    self.assertEqual(len(py_state.recovery_orders), int(expected["recovery_orders"]), name)
                if "round_trips" in expected:
                    self.assertEqual(py_state.total_round_trips, int(expected["round_trips"]), name)
                if "completed_cycles" in expected:
                    self.assertEqual(len(py_state.completed_cycles), int(expected["completed_cycles"]), name)
                if "cycle_a" in expected:
                    self.assertEqual(py_state.cycle_a, int(expected["cycle_a"]), name)
                if "cycle_b" in expected:
                    self.assertEqual(py_state.cycle_b, int(expected["cycle_b"]), name)
                if "s2_entered_at" in expected:
                    exp_s2 = expected["s2_entered_at"]
                    if exp_s2 is None:
                        self.assertIsNone(py_state.s2_entered_at, name)
                    else:
                        self.assertIsNotNone(py_state.s2_entered_at, name)
                        self.assertAlmostEqual(float(py_state.s2_entered_at), float(exp_s2), places=9, msg=name)
                if "cooldown_until_a" in expected:
                    self.assertAlmostEqual(
                        float(py_state.cooldown_until_a),
                        float(expected["cooldown_until_a"]),
                        places=9,
                        msg=name,
                    )
                if "cooldown_until_b" in expected:
                    self.assertAlmostEqual(
                        float(py_state.cooldown_until_b),
                        float(expected["cooldown_until_b"]),
                        places=9,
                        msg=name,
                    )
                if "consecutive_refreshes_a" in expected:
                    self.assertEqual(py_state.consecutive_refreshes_a, int(expected["consecutive_refreshes_a"]), name)
                if "consecutive_refreshes_b" in expected:
                    self.assertEqual(py_state.consecutive_refreshes_b, int(expected["consecutive_refreshes_b"]), name)
                if "refresh_cooldown_until_a" in expected:
                    self.assertAlmostEqual(
                        float(py_state.refresh_cooldown_until_a),
                        float(expected["refresh_cooldown_until_a"]),
                        places=9,
                        msg=name,
                    )
                if "refresh_cooldown_until_b" in expected:
                    self.assertAlmostEqual(
                        float(py_state.refresh_cooldown_until_b),
                        float(expected["refresh_cooldown_until_b"]),
                        places=9,
                        msg=name,
                    )
                if "last_refresh_direction_a" in expected:
                    self.assertEqual(py_state.last_refresh_direction_a, expected["last_refresh_direction_a"], name)
                if "last_refresh_direction_b" in expected:
                    self.assertEqual(py_state.last_refresh_direction_b, expected["last_refresh_direction_b"], name)
                if "invariants" in expected:
                    self.assertEqual(sm.check_invariants(py_state), list(expected["invariants"]), name)

    def test_transition_parity_with_fixtures(self) -> None:
        self._run_parity_with_fixtures()

    def test_transition_parity_with_haskell_stub_backend(self) -> None:
        self.assertTrue(STUB_EXE.exists(), str(STUB_EXE))
        health_req = {
            "method": "check_invariants",
            "state": sm.to_dict(sm.PairState(market_price=0.1, now=0.0)),
        }
        health = subprocess.run(
            [str(STUB_EXE)],
            input=json.dumps(health_req),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(health.returncode, 0, health.stderr)
        health_payload = json.loads(health.stdout)
        self.assertIn("violations", health_payload)

        previous_backend = os.environ.get("DOGE_CORE_BACKEND")
        previous_exe = os.environ.get("DOGE_CORE_EXE")
        try:
            os.environ["DOGE_CORE_BACKEND"] = "haskell"
            os.environ["DOGE_CORE_EXE"] = str(STUB_EXE)
            self._run_parity_with_fixtures()
        finally:
            if previous_backend is None:
                os.environ.pop("DOGE_CORE_BACKEND", None)
            else:
                os.environ["DOGE_CORE_BACKEND"] = previous_backend
            if previous_exe is None:
                os.environ.pop("DOGE_CORE_EXE", None)
            else:
                os.environ["DOGE_CORE_EXE"] = previous_exe


if __name__ == "__main__":
    unittest.main()
