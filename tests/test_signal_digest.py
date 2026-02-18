import unittest

import signal_digest

try:
    import hmm_regime_detector as hrd
except Exception as exc:  # pragma: no cover
    hrd = None
    _HMM_IMPORT_ERROR = exc
else:  # pragma: no cover
    _HMM_IMPORT_ERROR = None


def _check_by_signal(checks, signal):
    for check in checks:
        if check.signal == signal:
            return check
    raise AssertionError(f"missing check: {signal}")


class SignalDigestRuleTests(unittest.TestCase):
    def test_evaluate_rules_returns_all_12_checks(self):
        checks = signal_digest.evaluate_rules(snapshot={})
        self.assertEqual(len(checks), 12)
        self.assertEqual({c.signal for c in checks}, set(signal_digest.RULE_PRIORITY.keys()))

    def test_ema_trend_threshold_boundaries(self):
        green = _check_by_signal(signal_digest.evaluate_rules({"trend_score": 0.0}), "ema_trend")
        amber = _check_by_signal(signal_digest.evaluate_rules({"trend_score": 0.003}), "ema_trend")
        red = _check_by_signal(signal_digest.evaluate_rules({"trend_score": -0.01}), "ema_trend")

        self.assertEqual(green.severity, "green")
        self.assertEqual(amber.severity, "amber")
        self.assertEqual(red.severity, "red")

    def test_rsi_zone_threshold_boundaries(self):
        green = _check_by_signal(signal_digest.evaluate_rules({"rsi_zone": 0.2}), "rsi_zone")
        amber = _check_by_signal(signal_digest.evaluate_rules({"rsi_zone": 0.21}), "rsi_zone")
        red = _check_by_signal(signal_digest.evaluate_rules({"rsi_zone": -0.41}), "rsi_zone")

        self.assertEqual(green.severity, "green")
        self.assertEqual(amber.severity, "amber")
        self.assertEqual(red.severity, "red")

    def test_top_concern_prefers_higher_priority_with_same_severity(self):
        digest = signal_digest.evaluate_signal_digest(
            snapshot={
                "capacity_fill_health": {"open_order_headroom": 5},
                "hmm_regime": {"confidence_effective": 0.10, "regime": "RANGING"},
            }
        )
        self.assertEqual(digest.light, "red")
        self.assertIn("headroom", digest.top_concern.lower())

    def test_sort_checks_red_first_then_priority(self):
        digest = signal_digest.evaluate_signal_digest(
            snapshot={
                "capacity_fill_health": {"open_order_headroom": 10},  # red
                "hmm_regime": {"confidence_effective": 0.40, "regime": "RANGING"},  # red
                "rsi_zone": 0.25,  # amber
                "trend_score": 0.0,  # green
            }
        )
        self.assertGreaterEqual(len(digest.checks), 3)
        self.assertEqual(digest.checks[0].severity, "red")
        self.assertEqual(digest.checks[0].signal, "headroom")


@unittest.skipIf(hrd is None, f"hmm_regime_detector import failed: {_HMM_IMPORT_ERROR}")
class HMMObservationExposureTests(unittest.TestCase):
    def test_update_captures_last_observation_before_training(self):
        detector = hrd.RegimeDetector.__new__(hrd.RegimeDetector)

        class _ExtractorStub:
            @staticmethod
            def extract(_closes, _volumes):
                return [[0.0002, 0.0015, -0.25, 1.18]]

        detector.extractor = _ExtractorStub()
        detector._trained = False
        detector.model = None
        detector.state = hrd.RegimeState()

        out = detector.update(
            closes=[0.10, 0.11, 0.12],
            volumes=[100.0, 105.0, 99.0],
        )

        self.assertIs(out, detector.state)
        self.assertIsNotNone(detector.last_observation)
        self.assertAlmostEqual(detector.last_observation.macd_hist_slope, 0.0002, places=8)
        self.assertAlmostEqual(detector.last_observation.ema_spread_pct, 0.0015, places=8)
        self.assertAlmostEqual(detector.last_observation.rsi_zone, -0.25, places=8)
        self.assertAlmostEqual(detector.last_observation.volume_ratio, 1.18, places=8)
        self.assertAlmostEqual(detector.last_macd_hist_slope, 0.0002, places=8)
        self.assertAlmostEqual(detector.last_ema_spread_pct, 0.0015, places=8)
        self.assertAlmostEqual(detector.last_rsi_zone, -0.25, places=8)
        self.assertAlmostEqual(detector.last_volume_ratio, 1.18, places=8)


if __name__ == "__main__":
    unittest.main()
