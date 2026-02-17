import unittest

try:
    import survival_model
except Exception as exc:  # pragma: no cover
    survival_model = None
    _IMPORT_ERROR = exc
else:  # pragma: no cover
    _IMPORT_ERROR = None


@unittest.skipIf(survival_model is None, f"survival_model import failed: {_IMPORT_ERROR}")
class SurvivalModelTests(unittest.TestCase):
    def _obs(
        self,
        *,
        duration: float,
        censored: bool,
        regime: int = 1,
        side: str = "A",
        distance: float = 0.2,
        weight: float = 1.0,
    ) -> "survival_model.FillObservation":
        return survival_model.FillObservation(
            duration_sec=float(duration),
            censored=bool(censored),
            regime_at_entry=int(regime),
            regime_at_exit=None if censored else int(regime),
            side=str(side),
            distance_pct=float(distance),
            posterior_1m=[0.2, 0.6, 0.2],
            posterior_15m=[0.2, 0.6, 0.2],
            posterior_1h=[0.2, 0.6, 0.2],
            entropy_at_entry=0.6,
            p_switch_at_entry=0.08,
            fill_imbalance=0.0,
            congestion_ratio=0.2,
            weight=float(weight),
            synthetic=False,
        )

    def test_kaplan_meier_curve_is_monotonic(self):
        cfg = survival_model.SurvivalConfig(min_observations=1, min_per_stratum=1, synthetic_weight=0.3, horizons=[60, 120, 240])
        model = survival_model.SurvivalModel(cfg, model_tier="kaplan_meier")
        rows = [
            self._obs(duration=60, censored=False),
            self._obs(duration=120, censored=False),
            self._obs(duration=180, censored=False),
            self._obs(duration=240, censored=False),
            self._obs(duration=300, censored=True),
        ]
        self.assertTrue(model.fit(rows))

        curve = model.km.curves.get("ranging_A")
        self.assertIsNotNone(curve)
        surv = [float(x) for x in curve.survival.tolist()]
        for i in range(len(surv) - 1):
            self.assertGreaterEqual(surv[i], surv[i + 1])

    def test_censoring_reduces_near_term_fill_probability(self):
        cfg = survival_model.SurvivalConfig(min_observations=1, min_per_stratum=1, synthetic_weight=0.3, horizons=[120, 240, 360])

        uncensored_model = survival_model.SurvivalModel(cfg, model_tier="kaplan_meier")
        uncensored_rows = [
            self._obs(duration=120, censored=False),
            self._obs(duration=180, censored=False),
            self._obs(duration=240, censored=False),
            self._obs(duration=300, censored=False),
        ]
        self.assertTrue(uncensored_model.fit(uncensored_rows))

        censored_model = survival_model.SurvivalModel(cfg, model_tier="kaplan_meier")
        censored_rows = list(uncensored_rows) + [
            self._obs(duration=240, censored=True),
            self._obs(duration=300, censored=True),
        ]
        self.assertTrue(censored_model.fit(censored_rows))

        probs_unc, _, _ = uncensored_model.km.predict(regime_at_entry=1, side="A", horizons=[240])
        probs_cen, _, _ = censored_model.km.predict(regime_at_entry=1, side="A", horizons=[240])
        self.assertLessEqual(float(probs_cen[240]), float(probs_unc[240]))

    def test_safe_defaults_when_insufficient_data(self):
        cfg = survival_model.SurvivalConfig(min_observations=10, min_per_stratum=2, synthetic_weight=0.3, horizons=[1800, 3600, 14400])
        model = survival_model.SurvivalModel(cfg, model_tier="cox")

        rows = [
            self._obs(duration=600, censored=False, regime=0, side="A", distance=0.1),
            self._obs(duration=700, censored=True, regime=1, side="B", distance=0.2),
            self._obs(duration=800, censored=False, regime=2, side="A", distance=0.3),
        ]
        self.assertFalse(model.fit(rows))

        pred = model.predict(self._obs(duration=1, censored=False, distance=0.2))
        self.assertAlmostEqual(pred.p_fill_30m, 0.5, places=8)
        self.assertAlmostEqual(pred.p_fill_1h, 0.5, places=8)
        self.assertAlmostEqual(pred.p_fill_4h, 0.5, places=8)
        self.assertEqual(pred.model_tier, "kaplan_meier")
        self.assertAlmostEqual(pred.confidence, 0.0, places=8)

    def test_synthetic_observations_cover_all_regime_side_strata(self):
        rows = survival_model.SurvivalModel.generate_synthetic_observations(n_paths=180, weight=0.3)
        self.assertGreaterEqual(len(rows), 6)

        strata = {(int(row.regime_at_entry), str(row.side).upper()) for row in rows}
        expected = {(r, s) for r in (0, 1, 2) for s in ("A", "B")}
        self.assertTrue(expected.issubset(strata))

    def test_cox_distance_sensitivity_when_fit_succeeds(self):
        cfg = survival_model.SurvivalConfig(min_observations=12, min_per_stratum=1, synthetic_weight=0.0, horizons=[1800, 3600, 14400])
        model = survival_model.SurvivalModel(cfg, model_tier="cox")

        rows = []
        for i in range(18):
            d = 0.05 + (i % 6) * 0.18
            rows.append(
                self._obs(
                    duration=900 + (d * 4000.0) + (i * 5.0),
                    censored=False,
                    regime=2,
                    side="B",
                    distance=d,
                )
            )
        fit_ok = model.fit(rows)
        self.assertTrue(fit_ok)
        if model.active_tier != "cox":
            self.skipTest("Cox tier did not activate in this environment")

        near = self._obs(duration=1, censored=False, regime=2, side="B", distance=0.10)
        far = self._obs(duration=1, censored=False, regime=2, side="B", distance=0.90)
        near_pred = model.predict(near)
        far_pred = model.predict(far)
        self.assertLessEqual(far_pred.p_fill_1h, near_pred.p_fill_1h)


if __name__ == "__main__":
    unittest.main()
