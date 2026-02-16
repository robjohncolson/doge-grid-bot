import unittest

from throughput_sizer import ThroughputConfig, ThroughputSizer


def _cycle(
    *,
    regime: int,
    trade_id: str,
    entry: float,
    exit_ts: float,
    duration: float,
    profit: float,
    volume: float = 10.0,
) -> dict:
    return {
        "regime_at_entry": regime,
        "trade_id": trade_id,
        "entry_time": entry,
        "exit_time": entry + duration,
        "net_profit": profit,
        "volume": volume,
        "_sort_exit": exit_ts,
    }


class ThroughputSizerTests(unittest.TestCase):
    def _cfg(self, **overrides) -> ThroughputConfig:
        cfg = ThroughputConfig(
            enabled=True,
            lookback_cycles=1000,
            min_samples=3,
            min_samples_per_bucket=2,
            full_confidence_samples=10,
            floor_mult=0.5,
            ceiling_mult=2.0,
            censored_weight=0.5,
            age_pressure_trigger=1.5,
            age_pressure_sensitivity=0.5,
            age_pressure_floor=0.3,
            util_threshold=0.7,
            util_sensitivity=0.8,
            util_floor=0.4,
            recency_halflife=0,
            log_updates=False,
        )
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return cfg

    def test_update_partitions_regime_side_buckets(self):
        sizer = ThroughputSizer(self._cfg(min_samples=1, min_samples_per_bucket=1, full_confidence_samples=1))
        cycles = [
            _cycle(regime=0, trade_id="A", entry=0, exit_ts=100, duration=50, profit=1.0),
            _cycle(regime=0, trade_id="B", entry=0, exit_ts=101, duration=60, profit=1.0),
            _cycle(regime=1, trade_id="A", entry=0, exit_ts=102, duration=40, profit=1.0),
            _cycle(regime=1, trade_id="B", entry=0, exit_ts=103, duration=30, profit=1.0),
            _cycle(regime=2, trade_id="A", entry=0, exit_ts=104, duration=20, profit=1.0),
            _cycle(regime=2, trade_id="B", entry=0, exit_ts=105, duration=10, profit=1.0),
        ]
        stats = sizer.update(cycles, open_exits=[], regime_label="ranging", free_doge=100.0)
        self.assertIn("aggregate", stats)
        self.assertIn("bearish_A", stats)
        self.assertIn("bearish_B", stats)
        self.assertIn("ranging_A", stats)
        self.assertIn("ranging_B", stats)
        self.assertIn("bullish_A", stats)
        self.assertIn("bullish_B", stats)

    def test_fill_time_stats_compute_expected_percentiles(self):
        sizer = ThroughputSizer(self._cfg(min_samples=3, min_samples_per_bucket=1, full_confidence_samples=1))
        cycles = [
            _cycle(regime=1, trade_id="A", entry=0, exit_ts=100, duration=10, profit=1.0),
            _cycle(regime=1, trade_id="A", entry=0, exit_ts=101, duration=20, profit=1.0),
            _cycle(regime=1, trade_id="A", entry=0, exit_ts=102, duration=30, profit=1.0),
        ]
        stats = sizer.update(cycles, open_exits=[], regime_label="ranging", free_doge=100.0)
        agg = stats["aggregate"]
        self.assertAlmostEqual(agg.median_fill_sec, 20.0)
        self.assertAlmostEqual(agg.p75_fill_sec, 30.0)
        self.assertAlmostEqual(agg.p95_fill_sec, 30.0)

    def test_censored_observations_contribute_with_weight(self):
        sizer = ThroughputSizer(
            self._cfg(min_samples=4, min_samples_per_bucket=1, full_confidence_samples=1, censored_weight=0.5)
        )
        cycles = [
            _cycle(regime=1, trade_id="A", entry=0, exit_ts=100 + i, duration=10, profit=1.0)
            for i in range(4)
        ]
        open_exits = [{"regime_at_entry": 1, "trade_id": "A", "age_sec": 100.0, "volume": 5.0} for _ in range(10)]
        stats = sizer.update(cycles, open_exits=open_exits, regime_label="ranging", free_doge=100.0)
        agg = stats["aggregate"]
        self.assertEqual(agg.n_censored, 10)
        self.assertGreaterEqual(agg.median_fill_sec, 100.0)

    def test_faster_bucket_gets_multiplier_above_one(self):
        sizer = ThroughputSizer(self._cfg(min_samples=6, min_samples_per_bucket=2, full_confidence_samples=1))
        cycles = []
        for i in range(2):
            cycles.append(_cycle(regime=1, trade_id="A", entry=0, exit_ts=100 + i, duration=10, profit=1.0))
        for i in range(6):
            cycles.append(_cycle(regime=2, trade_id="B", entry=0, exit_ts=200 + i, duration=40, profit=1.0))
        sizer.update(cycles, open_exits=[], regime_label="ranging", free_doge=100.0)
        sized, _reason = sizer.size_for_slot(100.0, regime_label="ranging", trade_id="A")
        self.assertGreater(sized, 100.0)

    def test_slower_bucket_gets_multiplier_below_one(self):
        sizer = ThroughputSizer(self._cfg(min_samples=6, min_samples_per_bucket=2, full_confidence_samples=1))
        cycles = []
        for i in range(2):
            cycles.append(_cycle(regime=1, trade_id="A", entry=0, exit_ts=100 + i, duration=40, profit=1.0))
        for i in range(6):
            cycles.append(_cycle(regime=2, trade_id="B", entry=0, exit_ts=200 + i, duration=10, profit=1.0))
        sizer.update(cycles, open_exits=[], regime_label="ranging", free_doge=100.0)
        sized, _reason = sizer.size_for_slot(100.0, regime_label="ranging", trade_id="A")
        self.assertLess(sized, 100.0)

    def test_confidence_blend_toward_one_for_small_bucket(self):
        sizer = ThroughputSizer(self._cfg(min_samples=4, min_samples_per_bucket=1, full_confidence_samples=10))
        cycles = [
            _cycle(regime=1, trade_id="A", entry=0, exit_ts=100, duration=10, profit=1.0),
            _cycle(regime=2, trade_id="B", entry=0, exit_ts=101, duration=20, profit=1.0),
            _cycle(regime=2, trade_id="B", entry=0, exit_ts=102, duration=20, profit=1.0),
            _cycle(regime=2, trade_id="B", entry=0, exit_ts=103, duration=20, profit=1.0),
        ]
        sizer.update(cycles, open_exits=[], regime_label="ranging", free_doge=100.0)
        sized, _ = sizer.size_for_slot(100.0, regime_label="ranging", trade_id="A")
        self.assertAlmostEqual(sized, 110.0, delta=2.0)

    def test_age_pressure_throttles_when_oldest_exit_stalls(self):
        sizer = ThroughputSizer(
            self._cfg(
                min_samples=3,
                min_samples_per_bucket=1,
                full_confidence_samples=1,
                floor_mult=0.1,
                age_pressure_trigger=1.0,
                age_pressure_sensitivity=1.0,
                age_pressure_floor=0.3,
            )
        )
        cycles = [
            _cycle(regime=1, trade_id="A", entry=0, exit_ts=100, duration=10, profit=1.0),
            _cycle(regime=1, trade_id="B", entry=0, exit_ts=101, duration=10, profit=1.0),
            _cycle(regime=2, trade_id="A", entry=0, exit_ts=102, duration=10, profit=1.0),
        ]
        open_exits = [{"regime_at_entry": 1, "trade_id": "A", "age_sec": 30.0, "volume": 10.0}]
        sizer.update(cycles, open_exits=open_exits, regime_label="ranging", free_doge=100.0)
        sized, _ = sizer.size_for_slot(100.0, regime_label="ranging", trade_id="A")
        self.assertAlmostEqual(sized, 30.0, delta=1.0)

    def test_age_pressure_uses_p90_reference_ignoring_single_outlier(self):
        sizer = ThroughputSizer(
            self._cfg(
                min_samples=3,
                min_samples_per_bucket=1,
                full_confidence_samples=1,
                floor_mult=0.1,
                age_pressure_trigger=1.0,
                age_pressure_sensitivity=1.0,
                age_pressure_floor=0.3,
            )
        )
        cycles = [
            _cycle(regime=1, trade_id="A", entry=0, exit_ts=100, duration=10, profit=1.0),
            _cycle(regime=1, trade_id="B", entry=0, exit_ts=101, duration=10, profit=1.0),
            _cycle(regime=2, trade_id="A", entry=0, exit_ts=102, duration=10, profit=1.0),
        ]
        open_exits = [{"regime_at_entry": 1, "trade_id": "A", "age_sec": 10.0, "volume": 10.0} for _ in range(10)]
        open_exits.append({"regime_at_entry": 1, "trade_id": "B", "age_sec": 1000.0, "volume": 10.0})
        sizer.update(cycles, open_exits=open_exits, regime_label="ranging", free_doge=100.0)
        payload = sizer.status_payload()

        self.assertEqual(payload["age_pressure_reference"], "p90")
        self.assertAlmostEqual(float(payload["age_pressure_ref_age_sec"]), 10.0, delta=1e-6)
        self.assertAlmostEqual(float(payload["oldest_open_exit_age_sec"]), 1000.0, delta=1e-6)
        self.assertAlmostEqual(float(payload["age_pressure"]), 1.0, delta=1e-6)

    def test_age_pressure_p90_small_open_set_degrades_toward_max(self):
        sizer = ThroughputSizer(
            self._cfg(
                min_samples=3,
                min_samples_per_bucket=1,
                full_confidence_samples=1,
                floor_mult=0.1,
                age_pressure_trigger=1.0,
                age_pressure_sensitivity=1.0,
                age_pressure_floor=0.3,
            )
        )
        cycles = [
            _cycle(regime=1, trade_id="A", entry=0, exit_ts=100, duration=10, profit=1.0),
            _cycle(regime=1, trade_id="B", entry=0, exit_ts=101, duration=10, profit=1.0),
            _cycle(regime=2, trade_id="A", entry=0, exit_ts=102, duration=10, profit=1.0),
        ]
        open_exits = [{"regime_at_entry": 1, "trade_id": "A", "age_sec": 10.0, "volume": 10.0} for _ in range(8)]
        open_exits.append({"regime_at_entry": 1, "trade_id": "B", "age_sec": 1000.0, "volume": 10.0})
        sizer.update(cycles, open_exits=open_exits, regime_label="ranging", free_doge=100.0)
        payload = sizer.status_payload()

        self.assertAlmostEqual(float(payload["age_pressure_ref_age_sec"]), 1000.0, delta=1e-6)
        self.assertAlmostEqual(float(payload["age_pressure"]), 0.3, delta=1e-6)

    def test_utilization_penalty_throttles_when_locked_is_high(self):
        sizer = ThroughputSizer(
            self._cfg(
                min_samples=3,
                min_samples_per_bucket=1,
                full_confidence_samples=1,
                floor_mult=0.1,
                util_threshold=0.5,
                util_sensitivity=1.0,
                util_floor=0.4,
            )
        )
        cycles = [
            _cycle(regime=1, trade_id="A", entry=0, exit_ts=100, duration=10, profit=1.0),
            _cycle(regime=1, trade_id="B", entry=0, exit_ts=101, duration=10, profit=1.0),
            _cycle(regime=2, trade_id="A", entry=0, exit_ts=102, duration=10, profit=1.0),
        ]
        open_exits = [{"regime_at_entry": 1, "trade_id": "A", "age_sec": 15.0, "volume": 90.0}]
        sizer.update(cycles, open_exits=open_exits, regime_label="ranging", free_doge=10.0)
        sized, _ = sizer.size_for_slot(100.0, regime_label="ranging", trade_id="A")
        self.assertAlmostEqual(sized, 40.0, delta=1.0)

    def test_final_multiplier_respects_floor_and_ceiling(self):
        sizer_hi = ThroughputSizer(self._cfg(min_samples=4, min_samples_per_bucket=2, full_confidence_samples=1))
        cycles_hi = [
            _cycle(regime=1, trade_id="A", entry=0, exit_ts=100, duration=1, profit=1.0),
            _cycle(regime=1, trade_id="A", entry=0, exit_ts=101, duration=1, profit=1.0),
            _cycle(regime=2, trade_id="B", entry=0, exit_ts=102, duration=100, profit=1.0),
            _cycle(regime=2, trade_id="B", entry=0, exit_ts=103, duration=100, profit=1.0),
            _cycle(regime=2, trade_id="B", entry=0, exit_ts=104, duration=100, profit=1.0),
            _cycle(regime=2, trade_id="B", entry=0, exit_ts=105, duration=100, profit=1.0),
            _cycle(regime=2, trade_id="B", entry=0, exit_ts=106, duration=100, profit=1.0),
            _cycle(regime=2, trade_id="B", entry=0, exit_ts=107, duration=100, profit=1.0),
        ]
        sizer_hi.update(cycles_hi, open_exits=[], regime_label="ranging", free_doge=100.0)
        sized_hi, _ = sizer_hi.size_for_slot(100.0, regime_label="ranging", trade_id="A")
        self.assertAlmostEqual(sized_hi, 200.0, delta=1.0)

        sizer_lo = ThroughputSizer(self._cfg(min_samples=4, min_samples_per_bucket=2, full_confidence_samples=1))
        cycles_lo = [
            _cycle(regime=1, trade_id="A", entry=0, exit_ts=100, duration=100, profit=1.0),
            _cycle(regime=1, trade_id="A", entry=0, exit_ts=101, duration=100, profit=1.0),
            _cycle(regime=2, trade_id="B", entry=0, exit_ts=102, duration=1, profit=1.0),
            _cycle(regime=2, trade_id="B", entry=0, exit_ts=103, duration=1, profit=1.0),
            _cycle(regime=2, trade_id="B", entry=0, exit_ts=104, duration=1, profit=1.0),
            _cycle(regime=2, trade_id="B", entry=0, exit_ts=105, duration=1, profit=1.0),
            _cycle(regime=2, trade_id="B", entry=0, exit_ts=106, duration=1, profit=1.0),
            _cycle(regime=2, trade_id="B", entry=0, exit_ts=107, duration=1, profit=1.0),
        ]
        sizer_lo.update(cycles_lo, open_exits=[], regime_label="ranging", free_doge=100.0)
        sized_lo, _ = sizer_lo.size_for_slot(100.0, regime_label="ranging", trade_id="A")
        self.assertAlmostEqual(sized_lo, 50.0, delta=1.0)

    def test_insufficient_data_returns_pass_through(self):
        sizer = ThroughputSizer(self._cfg(min_samples=10, min_samples_per_bucket=5))
        cycles = [
            _cycle(regime=1, trade_id="A", entry=0, exit_ts=100, duration=10, profit=1.0),
            _cycle(regime=1, trade_id="A", entry=0, exit_ts=101, duration=10, profit=1.0),
        ]
        sizer.update(cycles, open_exits=[], regime_label="ranging", free_doge=100.0)
        sized, reason = sizer.size_for_slot(100.0, regime_label="ranging", trade_id="A")
        self.assertEqual(sized, 100.0)
        self.assertIn("insufficient_data", reason)

    def test_disabled_returns_pass_through(self):
        cfg = self._cfg()
        cfg.enabled = False
        sizer = ThroughputSizer(cfg)
        sized, reason = sizer.size_for_slot(100.0, regime_label="ranging", trade_id="A")
        self.assertEqual(sized, 100.0)
        self.assertEqual(reason, "tp_disabled")

    def test_snapshot_restore_round_trip(self):
        sizer = ThroughputSizer(self._cfg(min_samples=3, min_samples_per_bucket=1, full_confidence_samples=1))
        cycles = [
            _cycle(regime=1, trade_id="A", entry=0, exit_ts=100, duration=10, profit=2.0),
            _cycle(regime=1, trade_id="B", entry=0, exit_ts=101, duration=20, profit=1.0),
            _cycle(regime=2, trade_id="A", entry=0, exit_ts=102, duration=30, profit=1.0),
        ]
        open_exits = [{"regime_at_entry": 1, "trade_id": "A", "age_sec": 25.0, "volume": 40.0}]
        sizer.update(cycles, open_exits=open_exits, regime_label="bullish", free_doge=60.0)
        snap = sizer.snapshot_state()

        restored = ThroughputSizer(self._cfg(min_samples=3, min_samples_per_bucket=1, full_confidence_samples=1))
        restored.restore_state(snap)
        payload = restored.status_payload()
        self.assertEqual(payload["active_regime"], "bullish")
        self.assertEqual(payload["last_update_n"], 3)
        self.assertEqual(payload["age_pressure_reference"], "p90")
        self.assertIn("age_pressure_ref_age_sec", payload)
        self.assertIn("aggregate", payload)
        self.assertIn("ranging_A", payload)


if __name__ == "__main__":
    unittest.main()
