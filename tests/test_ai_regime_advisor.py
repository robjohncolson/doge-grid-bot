import unittest
import json
import time
from unittest import mock

import ai_advisor

try:
    import hmm_regime_detector as hrd
except ModuleNotFoundError:
    hrd = None


class AIRegimeAdvisorP0Tests(unittest.TestCase):
    def setUp(self):
        ai_advisor._panelist_consecutive_fails.clear()
        ai_advisor._panelist_skip_until.clear()

    def test_sambanova_panelists_defined(self):
        self.assertEqual(
            ai_advisor.SAMBANOVA_PANELISTS,
            [
                ("DeepSeek-R1", "DeepSeek-R1-0528", True),
                ("DeepSeek-V3.1", "DeepSeek-V3.1", False),
            ],
        )

    def test_cerebras_panelists_defined(self):
        self.assertEqual(
            ai_advisor.CEREBRAS_PANELISTS,
            [
                ("Qwen3-235B", "qwen-3-235b-a22b-instruct-2507", False),
                ("GPT-OSS-120B", "gpt-oss-120b", False),
            ],
        )

    def test_groq_panelists_excludes_gpt_oss(self):
        self.assertEqual(
            ai_advisor.GROQ_PANELISTS,
            [
                ("Llama-70B", "llama-3.3-70b-versatile", False),
                ("Llama-8B", "llama-3.1-8b-instant", False),
            ],
        )

    def test_build_panel_all_providers(self):
        with mock.patch.object(ai_advisor.config, "SAMBANOVA_API_KEY", "s-key"):
            with mock.patch.object(ai_advisor.config, "CEREBRAS_API_KEY", "c-key"):
                with mock.patch.object(ai_advisor.config, "GROQ_API_KEY", "g-key"):
                    with mock.patch.object(ai_advisor.config, "NVIDIA_API_KEY", "n-key"):
                        with mock.patch.object(ai_advisor.config, "AI_API_KEY", ""):
                            panel = ai_advisor._build_panel()

        self.assertEqual(len(panel), 7)
        names = [p["name"] for p in panel]
        self.assertIn("DeepSeek-R1", names)
        self.assertIn("DeepSeek-V3.1", names)
        self.assertIn("Qwen3-235B", names)
        self.assertIn("GPT-OSS-120B", names)
        self.assertIn("Llama-70B", names)
        self.assertIn("Llama-8B", names)
        self.assertIn("Kimi-K2.5", names)
        self.assertTrue(all(bool(p.get("panelist_id")) for p in panel))

    def test_build_panel_partial_keys(self):
        with mock.patch.object(ai_advisor.config, "SAMBANOVA_API_KEY", ""):
            with mock.patch.object(ai_advisor.config, "CEREBRAS_API_KEY", ""):
                with mock.patch.object(ai_advisor.config, "GROQ_API_KEY", "g-key"):
                    with mock.patch.object(ai_advisor.config, "NVIDIA_API_KEY", ""):
                        with mock.patch.object(ai_advisor.config, "AI_API_KEY", ""):
                            panel = ai_advisor._build_panel()
        self.assertEqual(len(panel), 2)
        self.assertEqual([p["name"] for p in panel], ["Llama-70B", "Llama-8B"])

    def test_ordered_panel_full_reasoning_chain(self):
        panel = [
            {"name": "DeepSeek-V3.1"},
            {"name": "Llama-8B"},
            {"name": "Qwen3-235B"},
            {"name": "DeepSeek-R1"},
            {"name": "Llama-70B"},
            {"name": "GPT-OSS-120B"},
            {"name": "Kimi-K2.5"},
        ]
        with mock.patch.object(ai_advisor.config, "AI_REGIME_PREFER_REASONING", True):
            ordered = ai_advisor._ordered_regime_panel(panel)
        self.assertEqual(
            [p["name"] for p in ordered],
            [
                "DeepSeek-R1",
                "Kimi-K2.5",
                "DeepSeek-V3.1",
                "Qwen3-235B",
                "GPT-OSS-120B",
                "Llama-70B",
                "Llama-8B",
            ],
        )

    def test_ordered_panel_full_instruct_chain(self):
        panel = [
            {"name": "DeepSeek-R1"},
            {"name": "Llama-70B"},
            {"name": "Llama-8B"},
            {"name": "DeepSeek-V3.1"},
            {"name": "GPT-OSS-120B"},
            {"name": "Kimi-K2.5"},
            {"name": "Qwen3-235B"},
        ]
        with mock.patch.object(ai_advisor.config, "AI_REGIME_PREFER_REASONING", False):
            ordered = ai_advisor._ordered_regime_panel(panel)
        self.assertEqual(
            [p["name"] for p in ordered],
            [
                "DeepSeek-V3.1",
                "Qwen3-235B",
                "GPT-OSS-120B",
                "Llama-70B",
                "Llama-8B",
                "DeepSeek-R1",
                "Kimi-K2.5",
            ],
        )

    def test_instruct_max_tokens_400(self):
        self.assertEqual(ai_advisor._INSTRUCT_MAX_TOKENS, 400)

    def test_reasoning_token_cap_not_clipped(self):
        captured_payload = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"{}"}}]}'

        def _fake_urlopen(req, timeout=0):
            captured_payload["payload"] = json.loads(req.data.decode("utf-8"))
            return _Resp()

        panelist = {
            "name": "DeepSeek-R1",
            "url": "https://api.sambanova.ai/v1/chat/completions",
            "model": "DeepSeek-R1-0528",
            "key": "k",
            "reasoning": True,
            "max_tokens": 2048,
        }
        with mock.patch("ai_advisor.urllib.request.urlopen", side_effect=_fake_urlopen):
            response, err = ai_advisor._call_panelist_messages([{"role": "user", "content": "x"}], panelist)

        self.assertEqual(err, "")
        self.assertEqual(response, "{}")
        self.assertEqual(int(captured_payload["payload"]["max_tokens"]), 2048)

    def test_instruct_token_cap_preserved(self):
        captured_payload = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"{}"}}]}'

        def _fake_urlopen(req, timeout=0):
            captured_payload["payload"] = json.loads(req.data.decode("utf-8"))
            return _Resp()

        panelist = {
            "name": "Llama-70B",
            "url": "https://api.groq.com/openai/v1/chat/completions",
            "model": "llama-3.3-70b-versatile",
            "key": "k",
            "reasoning": False,
            "max_tokens": 9999,
        }
        with mock.patch("ai_advisor.urllib.request.urlopen", side_effect=_fake_urlopen):
            response, err = ai_advisor._call_panelist_messages([{"role": "user", "content": "x"}], panelist)

        self.assertEqual(err, "")
        self.assertEqual(response, "{}")
        self.assertEqual(int(captured_payload["payload"]["max_tokens"]), 512)

    def test_regime_prompt_conviction_definition(self):
        prompt = ai_advisor._REGIME_SYSTEM_PROMPT
        self.assertIn("confidence in the ASSESSMENT", prompt)
        self.assertIn("Even Tier 0 can have high conviction", prompt)
        self.assertIn("Return ONLY a JSON object", prompt)

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
                "consensus_probabilities": {
                    "bearish": 0.013,
                    "ranging": 0.984,
                    "bullish": 0.003,
                },
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
        self.assertEqual(payload["hmm"]["consensus"]["consensus_probabilities"], [0.013, 0.984, 0.003])
        self.assertEqual(len(payload["regime_history_30m"]), 2)
        self.assertEqual(payload["mechanical_tier"]["current"], 0)
        self.assertEqual(payload["mechanical_tier"]["direction"], "symmetric")

    def test_consensus_probs_missing_defaults(self):
        payload = ai_advisor._build_regime_context({
            "hmm_consensus": {
                "agreement": "full",
                "effective_regime": "RANGING",
                "effective_confidence": 0.9,
                "effective_bias": 0.0,
            },
        })
        self.assertEqual(payload["hmm"]["consensus"]["consensus_probabilities"], [0.0, 1.0, 0.0])

    def test_consensus_probs_dict_format_supported(self):
        out = ai_advisor._sanitize_probabilities({
            "bearish": "0.12",
            "ranging": 0.83,
            "bullish": 0.05,
        })
        self.assertEqual(out, [0.12, 0.83, 0.05])

    def test_think_tag_stripping(self):
        response = (
            "<think>Let me reason about {json: true} and fields.</think>\n"
            '{"recommended_tier": 1, "recommended_direction": "long_bias", '
            '"conviction": 76, "rationale": "Trend is strengthening.", '
            '"watch_for": "15m confidence decay."}'
        )
        parsed, err = ai_advisor._parse_regime_opinion(response)
        self.assertEqual(err, "")
        self.assertEqual(parsed["recommended_tier"], 1)
        self.assertEqual(parsed["recommended_direction"], "long_bias")
        self.assertEqual(parsed["conviction"], 76)

    def test_think_tag_absent(self):
        response = (
            '{"recommended_tier": 0, "recommended_direction": "symmetric", '
            '"conviction": 61, "rationale": "Market is balanced.", '
            '"watch_for": "Bias expansion."}'
        )
        parsed, err = ai_advisor._parse_regime_opinion(response)
        self.assertEqual(err, "")
        self.assertEqual(parsed["recommended_tier"], 0)
        self.assertEqual(parsed["recommended_direction"], "symmetric")
        self.assertEqual(parsed["conviction"], 61)

    def test_reasoning_content_fallback(self):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return (
                    b'{"choices":[{"message":{"content":"","reasoning_content":"'
                    b'{\\"recommended_tier\\":0,\\"recommended_direction\\":\\"symmetric\\",'
                    b'\\"conviction\\":55,\\"rationale\\":\\"ok\\",\\"watch_for\\":\\"x\\"}"}}]}'
                )

        panelist = {
            "name": "DeepSeek-R1",
            "url": "https://api.sambanova.ai/v1/chat/completions",
            "model": "DeepSeek-R1-0528",
            "key": "k",
            "reasoning": True,
            "max_tokens": 2048,
        }
        with mock.patch("ai_advisor.urllib.request.urlopen", return_value=_Resp()):
            response, err = ai_advisor._call_panelist_messages([{"role": "user", "content": "x"}], panelist)

        self.assertEqual(err, "")
        self.assertIn('"recommended_tier":0', response.replace(" ", ""))

    def test_panelist_skip_tracking_uses_unique_identity_key(self):
        panel = [
            {
                "name": "GPT-OSS-120B",
                "url": "https://api.groq.com/openai/v1/chat/completions",
                "model": "openai/gpt-oss-120b",
                "key": "k",
                "reasoning": False,
                "max_tokens": 400,
                "panelist_id": "https://api.groq.com/openai/v1/chat/completions|openai/gpt-oss-120b",
            },
            {
                "name": "GPT-OSS-120B",
                "url": "https://api.cerebras.ai/v1/chat/completions",
                "model": "gpt-oss-120b",
                "key": "k",
                "reasoning": False,
                "max_tokens": 400,
                "panelist_id": "https://api.cerebras.ai/v1/chat/completions|gpt-oss-120b",
            },
        ]

        def _fake_call(messages, panelist):
            if panelist["panelist_id"].startswith("https://api.groq.com"):
                return ("", "http_429")
            return (
                '{"recommended_tier": 0, "recommended_direction": "symmetric", '
                '"conviction": 64, "rationale": "Range is stable.", '
                '"watch_for": "Bias expansion."}',
                "",
            )

        with mock.patch.object(ai_advisor.config, "AI_REGIME_ADVISOR_ENABLED", True):
            with mock.patch("ai_advisor._build_panel", return_value=panel):
                with mock.patch("ai_advisor._call_panelist_messages", side_effect=_fake_call):
                    result = ai_advisor.get_regime_opinion({})

        self.assertEqual(result["panelist"], "GPT-OSS-120B")
        self.assertEqual(
            ai_advisor._panelist_consecutive_fails.get(
                "https://api.groq.com/openai/v1/chat/completions|openai/gpt-oss-120b",
            ),
            1,
        )
        self.assertNotIn("GPT-OSS-120B", ai_advisor._panelist_consecutive_fails)

    def test_get_regime_opinion_honors_panelist_cooldown(self):
        panel = [
            {
                "name": "DeepSeek-R1",
                "url": "https://api.sambanova.ai/v1/chat/completions",
                "model": "DeepSeek-R1-0528",
                "key": "k",
                "reasoning": True,
                "max_tokens": 2048,
                "panelist_id": "https://api.sambanova.ai/v1/chat/completions|DeepSeek-R1-0528",
            },
            {
                "name": "DeepSeek-V3.1",
                "url": "https://api.sambanova.ai/v1/chat/completions",
                "model": "DeepSeek-V3.1",
                "key": "k",
                "reasoning": False,
                "max_tokens": 400,
                "panelist_id": "https://api.sambanova.ai/v1/chat/completions|DeepSeek-V3.1",
            },
        ]
        now = time.time()
        ai_advisor._panelist_skip_until["https://api.sambanova.ai/v1/chat/completions|DeepSeek-R1-0528"] = now + 60.0
        calls = []

        def _fake_call(messages, panelist):
            calls.append(panelist["panelist_id"])
            return (
                '{"recommended_tier": 0, "recommended_direction": "symmetric", '
                '"conviction": 70, "rationale": "Range is stable.", '
                '"watch_for": "Bias expansion."}',
                "",
            )

        with mock.patch.object(ai_advisor.config, "AI_REGIME_ADVISOR_ENABLED", True):
            with mock.patch("ai_advisor._build_panel", return_value=panel):
                with mock.patch("ai_advisor._call_panelist_messages", side_effect=_fake_call):
                    with mock.patch("ai_advisor.time.time", return_value=now):
                        result = ai_advisor.get_regime_opinion({})

        self.assertEqual(result["panelist"], "DeepSeek-V3.1")
        self.assertEqual(
            calls,
            ["https://api.sambanova.ai/v1/chat/completions|DeepSeek-V3.1"],
        )

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

    def test_reasoning_model_omits_temperature(self):
        """DeepSeek-R1 rejects the temperature parameter (HTTP 400)."""
        captured = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"{}"}}]}'

        def _fake_urlopen(req, timeout=0):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return _Resp()

        panelist = {
            "name": "DeepSeek-R1",
            "url": "https://api.sambanova.ai/v1/chat/completions",
            "model": "DeepSeek-R1-0528",
            "key": "k",
            "reasoning": True,
            "max_tokens": 2048,
        }
        with mock.patch("ai_advisor.urllib.request.urlopen", side_effect=_fake_urlopen):
            ai_advisor._call_panelist_messages([{"role": "user", "content": "x"}], panelist)

        self.assertNotIn("temperature", captured["payload"])

    def test_instruct_model_includes_temperature(self):
        """Instruct models should still get temperature=0.2."""
        captured = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"{}"}}]}'

        def _fake_urlopen(req, timeout=0):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return _Resp()

        panelist = {
            "name": "Llama-70B",
            "url": "https://api.groq.com/openai/v1/chat/completions",
            "model": "llama-3.3-70b-versatile",
            "key": "k",
            "reasoning": False,
            "max_tokens": 400,
        }
        with mock.patch("ai_advisor.urllib.request.urlopen", side_effect=_fake_urlopen):
            ai_advisor._call_panelist_messages([{"role": "user", "content": "x"}], panelist)

        self.assertIn("temperature", captured["payload"])
        self.assertAlmostEqual(captured["payload"]["temperature"], 0.2)

    def test_council_call_omits_temperature_for_reasoning(self):
        """_call_panelist (council path) also omits temperature for reasoning models."""
        captured = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"ok"}}]}'

        def _fake_urlopen(req, timeout=0):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return _Resp()

        panelist = {
            "name": "DeepSeek-R1",
            "url": "https://api.sambanova.ai/v1/chat/completions",
            "model": "DeepSeek-R1-0528",
            "key": "k",
            "reasoning": True,
            "max_tokens": 2048,
        }
        with mock.patch("ai_advisor.urllib.request.urlopen", side_effect=_fake_urlopen):
            ai_advisor._call_panelist("test prompt", panelist)

        self.assertNotIn("temperature", captured["payload"])

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


    def test_parse_regime_opinion_suggested_ttl_present(self):
        response = (
            '{"recommended_tier": 1, "recommended_direction": "long_bias", '
            '"conviction": 72, "rationale": "Trend.", "watch_for": "Decay.", '
            '"suggested_ttl_minutes": 25}'
        )
        parsed, err = ai_advisor._parse_regime_opinion(response)
        self.assertEqual(err, "")
        self.assertEqual(parsed["suggested_ttl_minutes"], 25)

    def test_parse_regime_opinion_suggested_ttl_missing(self):
        response = (
            '{"recommended_tier": 0, "recommended_direction": "symmetric", '
            '"conviction": 61, "rationale": "Balanced.", "watch_for": "Bias."}'
        )
        parsed, err = ai_advisor._parse_regime_opinion(response)
        self.assertEqual(err, "")
        self.assertEqual(parsed["suggested_ttl_minutes"], 0)

    def test_parse_regime_opinion_suggested_ttl_clamped(self):
        response = (
            '{"recommended_tier": 0, "recommended_direction": "symmetric", '
            '"conviction": 50, "rationale": "ok", "watch_for": "x", '
            '"suggested_ttl_minutes": 120}'
        )
        parsed, err = ai_advisor._parse_regime_opinion(response)
        self.assertEqual(err, "")
        self.assertEqual(parsed["suggested_ttl_minutes"], 60)

        response_neg = (
            '{"recommended_tier": 0, "recommended_direction": "symmetric", '
            '"conviction": 50, "rationale": "ok", "watch_for": "x", '
            '"suggested_ttl_minutes": -5}'
        )
        parsed_neg, err_neg = ai_advisor._parse_regime_opinion(response_neg)
        self.assertEqual(err_neg, "")
        self.assertEqual(parsed_neg["suggested_ttl_minutes"], 0)

    def test_default_regime_opinion_has_suggested_ttl(self):
        default = ai_advisor._default_regime_opinion()
        self.assertIn("suggested_ttl_minutes", default)
        self.assertEqual(default["suggested_ttl_minutes"], 0)


if __name__ == "__main__":
    unittest.main()
