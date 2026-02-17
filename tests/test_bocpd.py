import math
import unittest

try:
    import bocpd
except Exception as exc:  # pragma: no cover
    bocpd = None
    _IMPORT_ERROR = exc
else:  # pragma: no cover
    _IMPORT_ERROR = None


@unittest.skipIf(bocpd is None, f"bocpd import failed: {_IMPORT_ERROR}")
class BOCPDTests(unittest.TestCase):
    def test_change_prob_stays_low_on_stationary_series(self):
        detector = bocpd.BOCPD(
            expected_run_length=120,
            max_run_length=300,
            alert_threshold=0.30,
            urgent_threshold=0.50,
        )

        probs = []
        for i in range(240):
            x = 0.03 * math.sin(i / 7.0)
            state = detector.update(x)
            probs.append(float(state.change_prob))

        tail = probs[-60:]
        self.assertTrue(tail)
        self.assertLess(sum(tail) / len(tail), 0.15)

    def test_detects_mean_shift_change_point(self):
        detector = bocpd.BOCPD(
            expected_run_length=100,
            max_run_length=300,
            alert_threshold=0.15,
            urgent_threshold=0.35,
        )

        probs = []
        for _ in range(140):
            probs.append(float(detector.update(0.0).change_prob))
        for _ in range(120):
            probs.append(float(detector.update(1.5).change_prob))

        # Skip warmup: first ~30 steps have high change_prob because
        # all mass starts on young run lengths before settling.
        before = max(probs[30:130])
        after = max(probs[140:200])
        self.assertGreater(after, before + 0.05)
        self.assertGreater(after, 0.12)

    def test_alert_flag_activates_on_strong_shift(self):
        detector = bocpd.BOCPD(
            expected_run_length=80,
            max_run_length=240,
            alert_threshold=0.05,
            urgent_threshold=0.20,
        )

        for _ in range(100):
            detector.update(0.0)
        alerts = []
        for _ in range(40):
            state = detector.update(2.0)
            alerts.append(bool(state.alert_active))

        self.assertTrue(any(alerts))

    def test_snapshot_restore_round_trip(self):
        detector = bocpd.BOCPD(
            expected_run_length=90,
            max_run_length=260,
            alert_threshold=0.20,
            urgent_threshold=0.40,
        )
        for i in range(180):
            detector.update(0.02 * math.sin(i / 5.0) + (0.8 if i > 110 else 0.0))

        snapshot = detector.snapshot_state()

        restored = bocpd.BOCPD(
            expected_run_length=90,
            max_run_length=260,
            alert_threshold=0.20,
            urgent_threshold=0.40,
        )
        restored.restore_state(snapshot)

        self.assertEqual(restored.state.observation_count, detector.state.observation_count)
        self.assertEqual(restored.state.run_length_mode, detector.state.run_length_mode)
        self.assertAlmostEqual(restored.state.change_prob, detector.state.change_prob, places=6)

        next_a = detector.update(0.25)
        next_b = restored.update(0.25)
        self.assertAlmostEqual(next_a.change_prob, next_b.change_prob, places=6)
        self.assertEqual(next_a.run_length_mode, next_b.run_length_mode)


if __name__ == "__main__":
    unittest.main()
