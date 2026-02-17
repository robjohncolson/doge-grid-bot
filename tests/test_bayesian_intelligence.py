import unittest

try:
    import bayesian_engine
except Exception as exc:  # pragma: no cover
    bayesian_engine = None
    _IMPORT_ERROR = exc
else:  # pragma: no cover
    _IMPORT_ERROR = None

try:
    import hmm_regime_detector
except Exception as exc:  # pragma: no cover
    hmm_regime_detector = None
    _HMM_IMPORT_ERROR = exc
else:  # pragma: no cover
    _HMM_IMPORT_ERROR = None


@unittest.skipIf(bayesian_engine is None, f"bayesian_engine import failed: {_IMPORT_ERROR}")
class BayesianIntelligenceTests(unittest.TestCase):
    def test_entropy_edges(self):
        self.assertAlmostEqual(bayesian_engine.compute_entropy([1.0, 0.0, 0.0]), 0.0, places=8)
        self.assertAlmostEqual(
            bayesian_engine.compute_entropy([1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0]),
            1.0,
            places=6,
        )

    def test_p_switch_identity_and_uniform(self):
        ident = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
        self.assertAlmostEqual(bayesian_engine.compute_p_switch([0.2, 0.5, 0.3], ident), 0.0, places=8)

        uniform = [
            [1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0],
            [1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0],
            [1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0],
        ]
        self.assertAlmostEqual(
            bayesian_engine.compute_p_switch([0.2, 0.5, 0.3], uniform),
            2.0 / 3.0,
            places=6,
        )

    def test_knob_suppression_floor_and_ceiling(self):
        weak = bayesian_engine.BeliefState(
            enabled=True,
            direction_score=0.10,
            confidence_score=1.0,
            p_switch_consensus=0.0,
            entropy_consensus=0.0,
        )
        weak_knobs = bayesian_engine.compute_action_knobs(
            belief_state=weak,
            volatility_score=1.0,
            congestion_score=0.0,
            capacity_band="normal",
            cfg={},
            enabled=True,
        )
        self.assertAlmostEqual(weak_knobs.suppression_strength, 0.0, places=8)

        strong = bayesian_engine.BeliefState(
            enabled=True,
            direction_score=1.0,
            confidence_score=1.0,
            p_switch_consensus=0.0,
            entropy_consensus=0.0,
        )
        strong_knobs = bayesian_engine.compute_action_knobs(
            belief_state=strong,
            volatility_score=1.0,
            congestion_score=0.0,
            capacity_band="normal",
            cfg={},
            enabled=True,
        )
        self.assertAlmostEqual(strong_knobs.suppression_strength, 1.0, places=8)

    def test_knob_capacity_stop_forces_symmetric_suppression(self):
        strong = bayesian_engine.BeliefState(
            enabled=True,
            direction_score=1.0,
            confidence_score=1.0,
            p_switch_consensus=0.0,
            entropy_consensus=0.0,
        )
        knobs = bayesian_engine.compute_action_knobs(
            belief_state=strong,
            volatility_score=1.0,
            congestion_score=0.0,
            capacity_band="stop",
            cfg={},
            enabled=True,
        )
        self.assertAlmostEqual(knobs.suppression_strength, 0.0, places=8)

    def test_derived_tier_mapping(self):
        self.assertEqual(bayesian_engine.derive_tier_from_knobs(0.81, 1.0)[0], 2)
        self.assertEqual(bayesian_engine.derive_tier_from_knobs(0.30, 1.0)[0], 1)
        self.assertEqual(bayesian_engine.derive_tier_from_knobs(0.0, 1.0)[0], 0)

    def test_regime_agreement_and_expected_value(self):
        entry = [1.0, 0.0, 0.0] * 3
        same = [1.0, 0.0, 0.0] * 3
        opposite = [0.0, 0.0, 1.0] * 3
        self.assertAlmostEqual(bayesian_engine.cosine_similarity(entry, same), 1.0, places=8)
        self.assertLessEqual(bayesian_engine.cosine_similarity(entry, opposite), 0.1)

        ev = bayesian_engine.expected_value(
            p_fill=0.8,
            profit_if_fill=1.0,
            opportunity_cost_per_hour=0.1,
            elapsed_sec=3600.0,
        )
        self.assertGreater(ev, 0.0)

    def test_trade_action_mapping(self):
        action, conf = bayesian_engine.recommend_trade_action(
            regime_agreement=0.2,
            confidence_score=0.8,
            p_fill_30m=0.4,
            p_fill_1h=0.4,
            p_fill_4h=0.4,
            expected_value_usd=0.0,
            ev_trend_label="stable",
            is_s2=False,
            widen_enabled=False,
            immediate_reprice_agreement=0.3,
            immediate_reprice_confidence=0.6,
            tighten_threshold_pfill=0.1,
            tighten_threshold_ev=0.0,
        )
        self.assertEqual(action, "reprice_breakeven")
        self.assertGreaterEqual(conf, 0.7)


@unittest.skipIf(hmm_regime_detector is None, f"hmm_regime_detector import failed: {_HMM_IMPORT_ERROR}")
class RegimeDetectorMathTests(unittest.TestCase):
    def test_detector_entropy_and_pswitch(self):
        self.assertAlmostEqual(hmm_regime_detector.RegimeDetector.compute_entropy([1.0, 0.0, 0.0]), 0.0, places=8)
        self.assertAlmostEqual(
            hmm_regime_detector.RegimeDetector.compute_entropy([1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0]),
            1.0,
            places=6,
        )
        ident = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
        self.assertAlmostEqual(
            hmm_regime_detector.RegimeDetector.compute_p_switch([0.2, 0.5, 0.3], ident),
            0.0,
            places=8,
        )


if __name__ == "__main__":
    unittest.main()
