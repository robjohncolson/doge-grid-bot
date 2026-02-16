import unittest
from unittest import mock

import ai_advisor

try:
    import hmm_regime_detector as hrd
except ModuleNotFoundError:
    hrd = None


class AIRegimeAdvisorP0Tests(unittest.TestCase):
    def test_parse_regime_opinion_validates_defaults_and_clamps(self):
        response = (
            "```json\n"
            "{\"recommended_tier\": 9, \"recommended_direction\": \"UP\", "
            "\"conviction\": 140, \"rationale\": \""
            + ("x" * 600)
            + "\", \"watch_for\": \""
            + ("y" * 300)
            + "\"}\n"
            "```"
        )

        parsed, err = ai_advisor._parse_regime_opinion(response)
        self.assertEqual(err, "")
        self.assertEqual(parsed["recommended_tier"], 0)
        self.assertEqual(parsed["recommended_direction"], "symmetric")
        self.assertEqual(parsed["conviction"], 100)
        self.assertEqual(len(parsed["rationale"]), 500)
        self.assertEqual(len(parsed["watch_for"]), 200)

    def test_build_regime_context_packs_required_schema(self):
        raw = {
            "hmm_primary": {
                "regime": "bullish",
                "confidence": "0.42",
                "bias_signal": "0.35",
                "probabilities": [0.08, 0.21, 0.71],
            },
            "hmm_secondary": {
                "regime": "RANGING",
                "confidence": 0.15,
                "bias_signal": 0.05,
                "probabilities": [0.28, 0.44, 0.28],
            },
            "hmm_consensus": {
                "agreement": "1m_cooling",
                "effective_regime": "RANGING",
                "effective_confidence": 0.08,
                "effective_bias": 0.03,
            },
            "transition_matrix_1m": [
                [0.95, 0.03, 0.02],
                [0.04, 0.91, 0.05],
                [0.02, 0.03, 0.95],
            ],
            "training_quality": "deep",
            "confidence_modifier": "0.95",
            "regime_history_30m": [
                {"ts": 1739616000, "regime": "RANGING", "conf": "0.12"},
                {"ts": 1739616300, "regime": "BULLISH", "conf": 0.42},
            ],
            "mechanical_tier": {
                "current": 0,
                "direction": "symmetric",
                "since": 1739615700,
            },
            "operational": {
                "directional_trend": "bullish",
                "trend_detected_at": 1739615000,
                "fill_rate_1h": 12,
                "recovery_order_count": 2,
                "capacity_headroom": 45,
                "capacity_band": "normal",
                "kelly_edge_bullish": 0.032,
                "kelly_edge_bearish": -0.008,
                "kelly_edge_ranging": 0.015,
            },
        }

        payload = ai_advisor._build_regime_context(raw)
        self.assertEqual(payload["hmm"]["primary_1m"]["regime"], "BULLISH")
        self.assertEqual(payload["hmm"]["secondary_15m"]["regime"], "RANGING")
        self.assertEqual(payload["hmm"]["training_quality"], "deep")
        self.assertAlmostEqual(float(payload["hmm"]["confidence_modifier"]), 0.95)
        self.assertEqual(len(payload["hmm"]["transition_matrix_1m"]), 3)
        self.assertEqual(len(payload["regime_history_30m"]), 2)
        self.assertEqual(payload["mechanical_tier"]["current"], 0)
        self.assertEqual(payload["mechanical_tier"]["direction"], "symmetric")

    def test_get_regime_opinion_prefers_reasoning_then_fallback(self):
        panel = [
            {"name": "Llama-8B", "url": "", "model": "", "key": "", "reasoning": False, "max_tokens": 200},
            {"name": "Kimi-K2.5", "url": "", "model": "", "key": "", "reasoning": True, "max_tokens": 2048},
            {"name": "Llama-70B", "url": "", "model": "", "key": "", "reasoning": False, "max_tokens": 200},
        ]
        call_order = []

        def _fake_call(messages, panelist):
            call_order.append(panelist["name"])
            if panelist["name"] == "Kimi-K2.5":
                return ("not-json", "")
            if panelist["name"] == "Llama-70B":
                return (
                    '{"recommended_tier": 1, "recommended_direction": "long_bias", '
                    '"conviction": 72, "rationale": "Momentum is improving.", '
                    '"watch_for": "15m confidence > 0.20."}',
                    "",
                )
            return ("", "not_used")

        with mock.patch.object(ai_advisor.config, "AI_REGIME_ADVISOR_ENABLED", True):
            with mock.patch.object(ai_advisor.config, "AI_REGIME_PREFER_REASONING", True):
                with mock.patch("ai_advisor._build_panel", return_value=panel):
                    with mock.patch("ai_advisor._call_panelist_messages", side_effect=_fake_call):
                        result = ai_advisor.get_regime_opinion({})

        self.assertEqual(call_order, ["Kimi-K2.5", "Llama-70B"])
        self.assertEqual(result["panelist"], "Llama-70B")
        self.assertEqual(result["recommended_tier"], 1)
        self.assertEqual(result["recommended_direction"], "long_bias")
        self.assertEqual(result["conviction"], 72)
        self.assertEqual(result["error"], "")

    def test_get_regime_opinion_never_raises(self):
        panel = [{"name": "Kimi-K2.5", "url": "", "model": "", "key": "", "reasoning": True, "max_tokens": 2048}]

        with mock.patch.object(ai_advisor.config, "AI_REGIME_ADVISOR_ENABLED", True):
            with mock.patch("ai_advisor._build_panel", return_value=panel):
                with mock.patch("ai_advisor._build_regime_context", side_effect=RuntimeError("boom")):
                    result = ai_advisor.get_regime_opinion({})

        self.assertEqual(result["recommended_tier"], 0)
        self.assertEqual(result["recommended_direction"], "symmetric")
        self.assertEqual(result["conviction"], 0)
        self.assertIn("boom", result["error"])

    def test_regime_detector_transmat_getter(self):
        if hrd is None:
            self.skipTest("numpy/hmm dependencies not available in test env")

        detector = object.__new__(hrd.RegimeDetector)
        detector._trained = False
        detector.model = None
        self.assertIsNone(detector.transmat)

        class _DummyModel:
            transmat_ = [
                [0.95, 0.03, 0.02],
                [0.04, 0.91, 0.05],
                [0.02, 0.03, 0.95],
            ]

        detector._trained = True
        detector.model = _DummyModel()
        self.assertEqual(
            detector.transmat,
            [
                [0.95, 0.03, 0.02],
                [0.04, 0.91, 0.05],
                [0.02, 0.03, 0.95],
            ],
        )


if __name__ == "__main__":
    unittest.main()
