import unittest
from types import SimpleNamespace
from unittest import mock

import bot
import config
import state_machine as sm


class SignalDigestRuntimeTests(unittest.TestCase):
    def _runtime(self) -> bot.BotRuntime:
        rt = bot.BotRuntime()
        rt.mode = "RUNNING"
        rt.last_price = 0.1
        rt.last_price_ts = 1000.0
        rt._last_balance_snapshot = {"ZUSD": "100.0", "XXDG": "1000.0"}
        rt._recon_baseline = {"usd": 100.0, "doge": 1000.0, "ts": 900.0}
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(market_price=0.1, now=1000.0),
            )
        }
        rt._hmm_detector = SimpleNamespace(
            last_observation=SimpleNamespace(
                macd_hist_slope=0.0002,
                ema_spread_pct=0.001,
                rsi_zone=0.1,
                volume_ratio=1.1,
            )
        )
        rt._hmm_state.update({"regime": "RANGING", "confidence": 0.91, "available": True, "trained": True})
        rt._hmm_state_secondary.update({"regime": "RANGING", "confidence": 0.82, "available": True, "trained": True})
        rt._hmm_state_tertiary.update({"regime": "RANGING", "confidence": 0.75, "available": True, "trained": True})
        rt._hmm_consensus.update(
            {
                "regime": "RANGING",
                "effective_regime": "RANGING",
                "effective_confidence": 0.89,
                "effective_bias": 0.0,
                "agreement": "all_align",
            }
        )
        return rt

    def test_run_signal_digest_populates_runtime_fields(self):
        rt = self._runtime()
        with mock.patch.object(config, "DIGEST_ENABLED", True):
            rt._run_signal_digest(1000.0)
        self.assertEqual(rt._digest_last_run_ts, 1000.0)
        self.assertEqual(rt._digest_light in {"green", "amber", "red"}, True)
        self.assertEqual(len(rt._digest_checks), 12)
        self.assertTrue(bool(rt._digest_top_concern))

    def test_status_payload_includes_signal_digest_block(self):
        rt = self._runtime()
        with mock.patch.object(config, "DIGEST_ENABLED", True):
            rt._run_signal_digest(1000.0)
            payload = rt.status_payload()
        self.assertIn("signal_digest", payload)
        digest = payload["signal_digest"]
        self.assertIn("light", digest)
        self.assertIn("checks", digest)
        self.assertIn("interpretation", digest)
        self.assertIn("interpretation_stale", digest)

    def test_snapshot_roundtrip_restores_signal_digest_fields(self):
        rt = self._runtime()
        with mock.patch.object(config, "DIGEST_ENABLED", True):
            rt._run_signal_digest(1000.0)
        rt._digest_interpretation = {
            "narrative": "range stable",
            "key_insight": "entries okay",
            "watch_for": "macd turn",
            "config_assessment": "well-suited",
            "config_suggestion": "",
            "panelist": "DeepSeek-Chat",
            "ts": 995.0,
        }
        rt._digest_interpretation_last_trigger = "periodic"
        rt._digest_interpretation_requested_at = 996.0
        snap = rt._global_snapshot()

        with mock.patch("bot.supabase_store.load_state", return_value=snap):
            with mock.patch("bot.supabase_store.load_max_event_id", return_value=0):
                restored = bot.BotRuntime()
                restored._load_snapshot()

        self.assertEqual(restored._digest_light, rt._digest_light)
        self.assertEqual(len(restored._digest_checks), len(rt._digest_checks))
        self.assertEqual(
            str(restored._digest_interpretation.get("narrative", "")),
            "range stable",
        )
        self.assertEqual(restored._digest_interpretation_last_trigger, "periodic")

    def test_manual_trigger_updates_digest_interpretation_request_state(self):
        rt = self._runtime()
        with mock.patch.object(config, "DIGEST_ENABLED", True):
            with mock.patch.object(config, "DIGEST_INTERPRETATION_ENABLED", True):
                ok, msg = rt.trigger_signal_digest_interpretation()
        self.assertTrue(ok)
        self.assertIn("triggered", msg.lower())
        self.assertEqual(rt._digest_interpretation_last_trigger, "manual")
        self.assertGreater(rt._digest_interpretation_requested_at, 0.0)

    def test_maybe_schedule_periodic_trigger(self):
        """Scheduler fires periodic trigger when interval elapsed."""
        rt = self._runtime()
        with mock.patch.object(config, "DIGEST_ENABLED", True):
            with mock.patch.object(config, "DIGEST_INTERPRETATION_ENABLED", True):
                with mock.patch.object(config, "DIGEST_INTERPRETATION_INTERVAL_SEC", 600.0):
                    with mock.patch.object(config, "DIGEST_INTERPRETATION_DEBOUNCE_SEC", 120.0):
                        rt._run_signal_digest(1000.0)
                        # Simulate time passing beyond interval
                        rt._digest_interpretation_last_attempt_ts = 0.0
                        with mock.patch.object(rt, "_start_digest_interpretation") as mock_start:
                            rt._maybe_schedule_digest_interpretation(1000.0)
                            mock_start.assert_called_once()
                            args = mock_start.call_args[0]
                            self.assertEqual(args[1], "periodic")

    def test_maybe_schedule_debounce_blocks(self):
        """Scheduler respects debounce — no trigger if too recent."""
        rt = self._runtime()
        with mock.patch.object(config, "DIGEST_ENABLED", True):
            with mock.patch.object(config, "DIGEST_INTERPRETATION_ENABLED", True):
                with mock.patch.object(config, "DIGEST_INTERPRETATION_DEBOUNCE_SEC", 120.0):
                    rt._run_signal_digest(1000.0)
                    rt._digest_interpretation_last_attempt_ts = 999.0  # 1 second ago
                    with mock.patch.object(rt, "_start_digest_interpretation") as mock_start:
                        rt._maybe_schedule_digest_interpretation(1000.0)
                        mock_start.assert_not_called()

    def test_maybe_schedule_light_change_trigger(self):
        """Scheduler fires light_change trigger when light changed after last interpretation."""
        rt = self._runtime()
        with mock.patch.object(config, "DIGEST_ENABLED", True):
            with mock.patch.object(config, "DIGEST_INTERPRETATION_ENABLED", True):
                with mock.patch.object(config, "DIGEST_INTERPRETATION_DEBOUNCE_SEC", 1.0):
                    with mock.patch.object(config, "DIGEST_INTERPRETATION_INTERVAL_SEC", 99999.0):
                        rt._run_signal_digest(1000.0)
                        rt._digest_light_changed_at = 999.0
                        rt._digest_interpretation = {"ts": 500.0}
                        rt._digest_interpretation_last_attempt_ts = 500.0
                        with mock.patch.object(rt, "_start_digest_interpretation") as mock_start:
                            rt._maybe_schedule_digest_interpretation(1000.0)
                            mock_start.assert_called_once()
                            args = mock_start.call_args[0]
                            self.assertEqual(args[1], "light_change")

    def test_process_pending_success_updates_interpretation(self):
        """Successful pending result updates interpretation fields."""
        rt = self._runtime()
        rt._digest_interpretation_pending = {
            "result": {
                "narrative": "Ranging stable.",
                "key_insight": "Grid optimal.",
                "watch_for": "RSI drop.",
                "config_assessment": "well-suited",
                "config_suggestion": "",
                "panelist": "DeepSeek-Chat",
                "provider": "deepseek",
                "model": "deepseek-chat",
                "error": "",
            },
            "trigger": "periodic",
            "requested_at": 990.0,
            "completed_at": 995.0,
        }
        rt._process_digest_interpretation_pending(1000.0)
        self.assertEqual(rt._digest_interpretation.get("narrative"), "Ranging stable.")
        self.assertEqual(rt._digest_interpretation.get("panelist"), "DeepSeek-Chat")
        self.assertEqual(rt._digest_interpretation_last_error, "")

    def test_process_pending_error_preserves_prior(self):
        """Failed pending result keeps prior interpretation intact."""
        rt = self._runtime()
        rt._digest_interpretation = {"narrative": "Prior.", "ts": 800.0}
        rt._digest_interpretation_pending = {
            "result": {"error": "http_429"},
            "trigger": "periodic",
            "requested_at": 990.0,
            "completed_at": 995.0,
        }
        rt._process_digest_interpretation_pending(1000.0)
        self.assertEqual(rt._digest_interpretation.get("narrative"), "Prior.")
        self.assertEqual(rt._digest_interpretation_last_error, "http_429")

    def test_trigger_manual_starts_worker(self):
        """Manual trigger now dispatches actual worker instead of placeholder."""
        rt = self._runtime()
        with mock.patch.object(config, "DIGEST_ENABLED", True):
            with mock.patch.object(config, "DIGEST_INTERPRETATION_ENABLED", True):
                with mock.patch.object(rt, "_start_digest_interpretation") as mock_start:
                    ok, msg = rt.trigger_signal_digest_interpretation()
                    self.assertTrue(ok)
                    self.assertIn("triggered", msg.lower())
                    mock_start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
