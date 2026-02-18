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
        self.assertIn("accepted", msg.lower())
        self.assertEqual(rt._digest_interpretation_last_trigger, "manual")
        self.assertGreater(rt._digest_interpretation_requested_at, 0.0)


if __name__ == "__main__":
    unittest.main()
