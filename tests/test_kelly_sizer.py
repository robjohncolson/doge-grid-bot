import math
import unittest

from kelly_sizer import (
    KellyConfig,
    KellyResult,
    KellySizer,
    _recency_weights,
    compute_kelly_fraction,
    partition_cycles_by_regime,
)


class KellySizerTests(unittest.TestCase):
    def test_recency_weighting_uses_exit_time_rank_decay(self):
        cycles = [
            {"net_profit": 0.01, "exit_time": 10},   # rank 2
            {"net_profit": -0.01, "exit_time": 20},  # rank 1
            {"net_profit": 0.02, "exit_time": 30},   # rank 0
        ]
        w_wins, w_losses = _recency_weights(cycles, halflife=2)

        self.assertEqual(len(w_wins), 2)
        self.assertEqual(len(w_losses), 1)
        self.assertAlmostEqual(w_wins[0], 0.5, places=8)
        self.assertAlmostEqual(w_wins[1], 1.0, places=8)
        self.assertAlmostEqual(w_losses[0], math.sqrt(0.5), places=8)

    def test_all_wins_caps_f_star_and_multiplier(self):
        out = compute_kelly_fraction(
            wins=[0.02, 0.03, 0.01],
            losses=[],
            fraction=0.25,
        )
        self.assertEqual(out.reason, "all_wins")
        self.assertEqual(out.f_star, 1.0)
        self.assertEqual(out.f_fractional, 0.25)
        self.assertEqual(out.multiplier, 1.25)
        self.assertTrue(math.isinf(out.payoff_ratio))

    def test_partition_cycles_by_regime_normalizes_legacy_text(self):
        cycles = [
            {"net_profit": 0.01, "regime_at_entry": "BULLISH", "exit_time": 3},
            {"net_profit": -0.01, "regime_at_entry": "RANGING", "exit_time": 2},
            {"net_profit": 0.02, "regime_at_entry": "0", "exit_time": 1},
            {"net_profit": 0.03, "regime_at_entry": 2, "exit_time": 0},
            {"net_profit": -0.02, "regime_at_entry": "UNKNOWN", "exit_time": -1},
            {"net_profit": 0.04, "regime_at_entry": None, "exit_time": -2},
        ]

        buckets = partition_cycles_by_regime(
            cycles,
            {0: "bearish", 1: "ranging", 2: "bullish"},
        )

        self.assertEqual(len(buckets["aggregate"]), 6)
        self.assertEqual(len(buckets["bullish"]), 2)
        self.assertEqual(len(buckets["ranging"]), 1)
        self.assertEqual(len(buckets["bearish"]), 1)
        self.assertEqual(len(buckets["unknown"]), 2)

    def test_no_edge_multiplier_is_clamped_to_floor(self):
        cfg = KellyConfig(
            kelly_floor_mult=0.7,
            kelly_ceiling_mult=1.5,
            negative_edge_mult=0.2,
            log_kelly_updates=False,
        )
        sizer = KellySizer(cfg)
        sizer._results = {
            "aggregate": KellyResult(
                f_star=-0.1,
                f_fractional=0.0,
                multiplier=1.0,
                win_rate=0.4,
                avg_win=0.01,
                avg_loss=0.02,
                payoff_ratio=0.5,
                n_total=40,
                n_wins=16,
                n_losses=24,
                edge=-0.2,
                sufficient_data=True,
                reason="no_edge",
            )
        }

        adjusted, reason = sizer.size_for_slot(100.0, regime_label="bearish")
        self.assertAlmostEqual(adjusted, 70.0)
        self.assertIn("kelly_no_edge(aggregate,m=0.700)", reason)

    def test_no_edge_multiplier_is_clamped_to_ceiling(self):
        cfg = KellyConfig(
            kelly_floor_mult=0.5,
            kelly_ceiling_mult=1.2,
            negative_edge_mult=2.0,
            log_kelly_updates=False,
        )
        sizer = KellySizer(cfg)
        sizer._results = {
            "aggregate": KellyResult(
                f_star=-0.1,
                f_fractional=0.0,
                multiplier=1.0,
                win_rate=0.4,
                avg_win=0.01,
                avg_loss=0.02,
                payoff_ratio=0.5,
                n_total=40,
                n_wins=16,
                n_losses=24,
                edge=-0.2,
                sufficient_data=True,
                reason="no_edge",
            )
        }

        adjusted, reason = sizer.size_for_slot(100.0)
        self.assertAlmostEqual(adjusted, 120.0)
        self.assertIn("m=1.200", reason)

    def test_update_works_without_regime_tags(self):
        cfg = KellyConfig(
            min_samples_total=3,
            min_samples_per_regime=2,
            use_recency_weighting=False,
            log_kelly_updates=False,
        )
        sizer = KellySizer(cfg)
        results = sizer.update(
            [
                {"net_profit": 0.02, "exit_time": 3},
                {"net_profit": -0.01, "exit_time": 2},
                {"net_profit": 0.01, "exit_time": 1},
            ],
            regime_label="ranging",
        )

        self.assertTrue(results["aggregate"].sufficient_data)
        self.assertFalse(results["bullish"].sufficient_data)
        self.assertFalse(results["ranging"].sufficient_data)
        self.assertFalse(results["bearish"].sufficient_data)

    def test_snapshot_restore_preserves_regime_and_count(self):
        cfg = KellyConfig(
            min_samples_total=2,
            min_samples_per_regime=1,
            use_recency_weighting=False,
            log_kelly_updates=False,
        )
        sizer = KellySizer(cfg)
        sizer.update(
            [
                {"net_profit": 0.02, "regime_at_entry": 2, "exit_time": 2},
                {"net_profit": -0.01, "regime_at_entry": 2, "exit_time": 1},
            ],
            regime_label="bullish",
        )
        snap = sizer.snapshot_state()

        restored = KellySizer(cfg)
        restored.restore_state(snap)
        payload = restored.status_payload()
        self.assertEqual(payload["active_regime"], "bullish")
        self.assertEqual(payload["last_update_n"], 2)
        # Results intentionally recompute on next update().
        self.assertNotIn("aggregate", payload)


if __name__ == "__main__":
    unittest.main()
