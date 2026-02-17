import unittest
from collections import deque
from unittest import mock

import bot
import config
import state_machine as sm


class BalanceIntelligenceTests(unittest.TestCase):
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
        return rt

    def test_poll_external_flows_detects_and_adjusts_baseline(self):
        rt = self._runtime()
        ledgers = {
            "count": 2,
            "ledger": {
                "LUSD-1": {
                    "type": "deposit",
                    "asset": "ZUSD",
                    "amount": "50.0",
                    "fee": "0.0",
                    "time": 1100.0,
                },
                "LDOGE-1": {
                    "type": "withdrawal",
                    "asset": "XXDG",
                    "amount": "100.0",
                    "fee": "0.0",
                    "time": 1110.0,
                },
            },
        }

        with mock.patch("bot.kraken_client.get_ledgers", return_value=ledgers):
            with mock.patch.object(config, "FLOW_BASELINE_AUTO_ADJUST", True):
                rt._poll_external_flows(1200.0)

        self.assertEqual(rt._flow_total_count, 2)
        self.assertEqual(len(rt._external_flows), 2)
        self.assertAlmostEqual(rt._flow_total_deposits_doge_eq, 500.0, places=8)
        self.assertAlmostEqual(rt._flow_total_withdrawals_doge_eq, -100.0, places=8)
        # USD flow adjusts baseline usd; DOGE flow adjusts baseline doge.
        self.assertAlmostEqual(rt._recon_baseline["usd"], 150.0, places=8)
        self.assertAlmostEqual(rt._recon_baseline["doge"], 900.0, places=8)
        self.assertEqual(len(rt._baseline_adjustments), 2)

    def test_poll_external_flows_dedups_ledger_id(self):
        rt = self._runtime()
        ledgers = {
            "count": 1,
            "ledger": {
                "L1": {
                    "type": "deposit",
                    "asset": "XXDG",
                    "amount": "250.0",
                    "fee": "0.0",
                    "time": 1100.0,
                }
            },
        }

        with mock.patch("bot.kraken_client.get_ledgers", return_value=ledgers):
            rt._poll_external_flows(1200.0)
            rt._poll_external_flows(1300.0)

        self.assertEqual(rt._flow_total_count, 1)
        self.assertEqual(len(rt._external_flows), 1)
        self.assertAlmostEqual(rt._flow_total_deposits_doge_eq, 250.0, places=8)

    def test_poll_external_flows_cursor_bootstrap_avoids_backfill(self):
        rt = self._runtime()
        rt._flow_ledger_cursor = 0.0
        rt._recon_baseline["ts"] = 500.0

        with mock.patch("bot.kraken_client.get_ledgers", return_value={"count": 0, "ledger": {}}) as mocked:
            rt._poll_external_flows(1300.0)

        self.assertTrue(mocked.called)
        self.assertEqual(mocked.call_args.kwargs.get("start"), 1000.0)

    def test_poll_external_flows_permission_error_disables_detection(self):
        rt = self._runtime()
        err = "Kraken API error: ['EGeneral:Permission denied']"

        with mock.patch("bot.kraken_client.get_ledgers", side_effect=Exception(err)):
            rt._poll_external_flows(1200.0)

        self.assertFalse(rt._flow_detection_active)
        self.assertFalse(rt._flow_last_ok)
        self.assertIn("permission", rt._flow_disabled_reason.lower())

    def test_compute_balance_recon_adjusted_drift_auto_adjust_enabled(self):
        rt = self._runtime()
        rt._recon_baseline = {"usd": 0.0, "doge": 1000.0, "ts": 900.0}
        rt._last_balance_snapshot = {"ZUSD": "0.0", "XXDG": "1100.0"}
        rt._flow_total_deposits_doge_eq = 500.0
        rt._flow_total_withdrawals_doge_eq = 0.0
        with mock.patch.object(config, "FLOW_BASELINE_AUTO_ADJUST", True):
            recon = rt._compute_balance_recon(0.0, 0.0)
        self.assertAlmostEqual(float(recon["drift_doge"]), 100.0, places=8)
        self.assertAlmostEqual(float(recon["adjusted_drift_doge"]), 100.0, places=8)

    def test_compute_balance_recon_adjusted_drift_observe_mode(self):
        rt = self._runtime()
        rt._recon_baseline = {"usd": 0.0, "doge": 1000.0, "ts": 900.0}
        rt._last_balance_snapshot = {"ZUSD": "0.0", "XXDG": "1100.0"}
        rt._flow_total_deposits_doge_eq = 500.0
        rt._flow_total_withdrawals_doge_eq = 0.0
        with mock.patch.object(config, "FLOW_BASELINE_AUTO_ADJUST", False):
            recon = rt._compute_balance_recon(0.0, 0.0)
        self.assertAlmostEqual(float(recon["drift_doge"]), 100.0, places=8)
        self.assertAlmostEqual(float(recon["adjusted_drift_doge"]), -400.0, places=8)

    def test_snapshot_roundtrip_restores_flow_state(self):
        rt = self._runtime()
        rt._external_flows = [
            bot.ExternalFlow(
                ledger_id="L1",
                flow_type="deposit",
                asset="XXDG",
                amount=250.0,
                fee=0.0,
                timestamp=1111.0,
                doge_eq=250.0,
                price_at_detect=0.1,
            )
        ]
        rt._flow_seen_ids = {"L1"}
        rt._flow_ledger_cursor = 1112.0
        rt._baseline_adjustments = [{"ledger_id": "L1"}]
        rt._flow_total_deposits_doge_eq = 250.0
        rt._flow_total_withdrawals_doge_eq = 0.0
        rt._flow_total_count = 1
        snap = rt._global_snapshot()

        with mock.patch("bot.supabase_store.load_state", return_value=snap):
            with mock.patch("bot.supabase_store.load_max_event_id", return_value=0):
                restored = bot.BotRuntime()
                restored._load_snapshot()

        self.assertEqual(restored._flow_total_count, 1)
        self.assertEqual(restored._flow_seen_ids, {"L1"})
        self.assertAlmostEqual(restored._flow_ledger_cursor, 1112.0, places=8)
        self.assertEqual(len(restored._external_flows), 1)

    def test_equity_history_payload_and_trim(self):
        rt = self._runtime()
        now = 10.0 * 86400.0
        rt._equity_ts_enabled = True
        rt._equity_ts_retention_days = 7
        rt._equity_ts_sparkline_7d_step = 3
        rt._equity_ts_records = [
            {"ts": now - 9.0 * 86400.0, "doge_eq": 900.0},
            {"ts": now - 2.0 * 86400.0, "doge_eq": 1000.0},
            {"ts": now - 1.0 * 86400.0, "doge_eq": 1020.0},
            {"ts": now - 1000.0, "doge_eq": 1030.0},
        ]
        rt._doge_eq_snapshots = deque([
            (now - 3600.0, 1028.0),
            (now - 300.0, 1030.0),
        ])

        rt._trim_equity_ts_records(now)
        payload = rt._equity_history_status_payload(now)
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["snapshots_persisted"], 3)
        self.assertEqual(payload["snapshots_in_memory"], 2)
        self.assertGreaterEqual(len(payload["sparkline_7d"]), 1)

    def test_status_payload_includes_external_flow_and_equity_blocks(self):
        rt = self._runtime()
        payload = rt.status_payload()
        self.assertIn("external_flows", payload)
        self.assertIn("equity_history", payload)


if __name__ == "__main__":
    unittest.main()
