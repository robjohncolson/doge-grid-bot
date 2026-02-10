import json
import unittest
from unittest import mock

import bot
import config
import grid_strategy


class HardeningRegressionTests(unittest.TestCase):
    def setUp(self):
        self._orig_bot_states = bot._bot_states
        self._orig_current_prices = bot._current_prices
        self._orig_web_pending = bot._web_config_pending

    def tearDown(self):
        bot._bot_states = self._orig_bot_states
        bot._current_prices = self._orig_current_prices
        bot._web_config_pending = self._orig_web_pending

    def test_pair_config_from_dict_restores_persisted_fields(self):
        payload = {
            "pair": "XBTUSD",
            "display": "XBT/USD",
            "entry_pct": 0.35,
            "profit_pct": 1.25,
            "refresh_pct": 0.9,
            "order_size_usd": 7.5,
            "recovery_mode": "liquidate",
            "capital_budget_usd": 42.0,
            "slot_count": 3,
        }
        pc = config.PairConfig.from_dict(payload)
        self.assertEqual(pc.pair, "XBTUSD")
        self.assertEqual(pc.order_size_usd, 7.5)
        self.assertEqual(pc.refresh_pct, 0.9)
        self.assertEqual(pc.recovery_mode, "liquidate")
        self.assertEqual(pc.capital_budget_usd, 42.0)
        self.assertEqual(pc.slot_count, 3)

    def test_build_pairs_parses_recovery_and_slot_fields(self):
        pairs_env = json.dumps([
            {
                "pair": "XBTUSD",
                "display": "XBT/USD",
                "entry_pct": 0.3,
                "profit_pct": 1.2,
                "recovery_mode": "liquidate",
                "capital_budget_usd": 55.5,
                "slot_count": 4,
            }
        ])
        with mock.patch.dict(config.os.environ, {"PAIRS": pairs_env}, clear=False):
            pairs = config._build_pairs()

        self.assertIn("XBTUSD", pairs)
        pc = pairs["XBTUSD"]
        self.assertEqual(pc.recovery_mode, "liquidate")
        self.assertEqual(pc.capital_budget_usd, 55.5)
        self.assertEqual(pc.slot_count, 4)

    def test_state_store_key_is_slot_aware(self):
        state = grid_strategy.GridState(
            pair_config=config.PairConfig("XDGUSD", "DOGE/USD", 0.2, 1.0)
        )
        self.assertEqual(grid_strategy.state_store_key(state), "XDGUSD")
        state.slot_id = 2
        self.assertEqual(grid_strategy.state_store_key(state), "XDGUSD#2")

    def test_restore_state_snapshot_applies_runtime_overrides(self):
        state = grid_strategy.GridState(
            pair_config=config.PairConfig("XDGUSD", "DOGE/USD", 0.2, 1.0)
        )
        snapshot = {
            "center_price": 0.1234,
            "pair_state": "S1b",
            "cycle_a": 5,
            "cycle_b": 6,
            "next_entry_multiplier": 2.0,
            "pair_entry_pct": 0.55,
            "pair_profit_pct": 1.75,
            "consecutive_losses_a": 3,
            "consecutive_losses_b": 1,
            "recovery_orders": [
                {
                    "txid": "TX123",
                    "side": "sell",
                    "price": 0.1300,
                    "volume": 10.0,
                    "trade_id": "B",
                    "cycle": 6,
                    "entry_price": 0.1200,
                    "reason": "timeout",
                }
            ],
            "slot_id": 1,
            "winding_down": True,
        }
        restored = grid_strategy.restore_state_snapshot(
            state, snapshot, source="test"
        )
        self.assertTrue(restored)
        self.assertEqual(state.center_price, 0.1234)
        self.assertEqual(state.pair_state, "S1b")
        self.assertEqual(state.cycle_a, 5)
        self.assertEqual(state.cycle_b, 6)
        self.assertEqual(state.entry_pct, 0.55)
        self.assertEqual(state.profit_pct, 1.75)
        self.assertEqual(state.next_entry_multiplier, 2.0)
        self.assertEqual(state.consecutive_losses_a, 3)
        self.assertEqual(state.consecutive_losses_b, 1)
        self.assertEqual(len(state.recovery_orders), 1)
        self.assertEqual(state.slot_id, 1)
        self.assertTrue(state.winding_down)

    def test_apply_web_config_targets_selected_pair(self):
        st_a = grid_strategy.GridState(
            pair_config=config.PairConfig("XDGUSD", "DOGE/USD", 0.2, 1.0)
        )
        st_b = grid_strategy.GridState(
            pair_config=config.PairConfig("XBTUSD", "XBT/USD", 0.3, 1.2)
        )
        bot._bot_states = {"XDGUSD": st_a, "XBTUSD": st_b}
        bot._current_prices = {"XDGUSD": 0.1, "XBTUSD": 43000.0}
        bot._web_config_pending = {"pair": "XBTUSD", "entry_pct": 0.65}

        with mock.patch.object(config, "STRATEGY_MODE", "pair"):
            with mock.patch.object(
                grid_strategy, "replace_entries_at_distance"
            ) as replace_entries:
                bot._apply_web_config()
                replace_entries.assert_called_once_with(st_b, 43000.0)

        self.assertEqual(st_a.entry_pct, 0.2)
        self.assertEqual(st_b.entry_pct, 0.65)

    def test_capture_rollover_summary_uses_pre_reset_counters(self):
        st_a = grid_strategy.GridState(
            pair_config=config.PairConfig("XDGUSD", "DOGE/USD", 0.2, 1.0)
        )
        st_b = grid_strategy.GridState(
            pair_config=config.PairConfig("XBTUSD", "XBT/USD", 0.3, 1.2)
        )
        st_c = grid_strategy.GridState(
            pair_config=config.PairConfig("SOLUSD", "SOL/USD", 0.3, 1.2)
        )

        st_a.today_date = "2026-02-09"
        st_a.round_trips_today = 2
        st_a.today_profit_usd = 1.25
        st_a.today_fees_usd = 0.10
        st_a.total_profit_usd = 5.0
        st_a.total_round_trips = 20

        st_b.today_date = "2026-02-09"
        st_b.round_trips_today = 1
        st_b.today_profit_usd = 0.75
        st_b.today_fees_usd = 0.05
        st_b.total_profit_usd = 3.0
        st_b.total_round_trips = 10

        st_c.today_date = "2026-02-10"  # already current day
        st_c.round_trips_today = 4
        st_c.today_profit_usd = 0.50
        st_c.today_fees_usd = 0.02
        st_c.total_profit_usd = 2.0
        st_c.total_round_trips = 8

        bot._bot_states = {"XDGUSD": st_a, "XBTUSD": st_b, "SOLUSD": st_c}
        summary = bot._capture_rollover_summary(
            current_utc_date="2026-02-10",
            last_daily_summary_date="",
        )

        self.assertIsNotNone(summary)
        self.assertEqual(summary["date"], "2026-02-09")
        self.assertEqual(summary["trades"], 3)
        self.assertAlmostEqual(summary["profit"], 2.0, places=8)
        self.assertAlmostEqual(summary["fees"], 0.15, places=8)
        self.assertAlmostEqual(summary["total_profit"], 10.0, places=8)
        self.assertEqual(summary["total_trips"], 38)


if __name__ == "__main__":
    unittest.main()
