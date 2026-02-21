use crate::features::FeatureExtractor;
use crate::hmm::GaussianHmm;
use crate::math::baum_welch::normalize_probs;
use crate::math::ema::clamp;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyAny;
use pyo3::types::PyDict;
use std::time::{SystemTime, UNIX_EPOCH};

const TIER_SHALLOW_MAX: i32 = 999;
const TIER_BASELINE_MAX: i32 = 2499;
const TIER_DEEP_MAX: i32 = 3999;

#[allow(non_camel_case_types)]
#[pyclass(eq, eq_int)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Regime {
    BEARISH = 0,
    RANGING = 1,
    BULLISH = 2,
}

#[pyclass]
#[derive(Clone, Debug)]
pub struct RegimeState {
    #[pyo3(get, set)]
    pub regime: i32,
    #[pyo3(get, set)]
    pub probabilities: Vec<f64>,
    #[pyo3(get, set)]
    pub confidence: f64,
    #[pyo3(get, set)]
    pub bias_signal: f64,
    #[pyo3(get, set)]
    pub last_update_ts: f64,
    #[pyo3(get, set)]
    pub observation_count: usize,
}

#[pymethods]
impl RegimeState {
    #[new]
    #[pyo3(signature = (
        regime=Regime::RANGING as i32,
        probabilities=None,
        confidence=0.0,
        bias_signal=0.0,
        last_update_ts=0.0,
        observation_count=0,
    ))]
    fn new(
        regime: i32,
        probabilities: Option<Vec<f64>>,
        confidence: f64,
        bias_signal: f64,
        last_update_ts: f64,
        observation_count: usize,
    ) -> Self {
        Self {
            regime,
            probabilities: probabilities.unwrap_or_else(|| vec![0.0, 1.0, 0.0]),
            confidence,
            bias_signal,
            last_update_ts,
            observation_count,
        }
    }

    fn to_dict(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let d = PyDict::new_bound(py);
        d.set_item("regime", self.regime)?;
        d.set_item("probabilities", self.probabilities.clone())?;
        d.set_item("confidence", self.confidence)?;
        d.set_item("bias_signal", self.bias_signal)?;
        d.set_item("last_update_ts", self.last_update_ts)?;
        d.set_item("observation_count", self.observation_count)?;
        Ok(d.unbind())
    }

    #[staticmethod]
    fn from_dict(d: &Bound<'_, PyDict>) -> PyResult<Self> {
        Ok(Self {
            regime: dict_i32(d, "regime", Regime::RANGING as i32),
            probabilities: dict_vec_f64(d, "probabilities", vec![0.0, 1.0, 0.0]),
            confidence: dict_f64(d, "confidence", 0.0),
            bias_signal: dict_f64(d, "bias_signal", 0.0),
            last_update_ts: dict_f64(d, "last_update_ts", 0.0),
            observation_count: dict_usize(d, "observation_count", 0),
        })
    }
}

#[pyclass]
#[derive(Clone, Debug)]
pub struct TertiaryTransition {
    #[pyo3(get, set)]
    pub from_regime: i32,
    #[pyo3(get, set)]
    pub to_regime: i32,
    #[pyo3(get, set)]
    pub confirmation_count: i32,
    #[pyo3(get, set)]
    pub confirmed: bool,
    #[pyo3(get, set)]
    pub changed_at: f64,
    #[pyo3(get, set)]
    pub transition_age_sec: f64,
}

impl Default for TertiaryTransition {
    fn default() -> Self {
        Self {
            from_regime: Regime::RANGING as i32,
            to_regime: Regime::RANGING as i32,
            confirmation_count: 0,
            confirmed: false,
            changed_at: 0.0,
            transition_age_sec: 0.0,
        }
    }
}

#[pymethods]
impl TertiaryTransition {
    #[new]
    #[pyo3(signature = (
        from_regime=Regime::RANGING as i32,
        to_regime=Regime::RANGING as i32,
        confirmation_count=0,
        confirmed=false,
        changed_at=0.0,
        transition_age_sec=0.0,
    ))]
    fn new(
        from_regime: i32,
        to_regime: i32,
        confirmation_count: i32,
        confirmed: bool,
        changed_at: f64,
        transition_age_sec: f64,
    ) -> Self {
        Self {
            from_regime,
            to_regime,
            confirmation_count,
            confirmed,
            changed_at,
            transition_age_sec,
        }
    }

    fn to_dict(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let d = PyDict::new_bound(py);
        d.set_item("from_regime", self.from_regime)?;
        d.set_item("to_regime", self.to_regime)?;
        d.set_item("confirmation_count", self.confirmation_count)?;
        d.set_item("confirmed", self.confirmed)?;
        d.set_item("changed_at", self.changed_at)?;
        d.set_item("transition_age_sec", self.transition_age_sec)?;
        Ok(d.unbind())
    }

    #[staticmethod]
    fn from_dict(d: &Bound<'_, PyDict>) -> PyResult<Self> {
        Ok(Self {
            from_regime: dict_regime_i32(d, "from_regime", Regime::RANGING as i32),
            to_regime: dict_regime_i32(d, "to_regime", Regime::RANGING as i32),
            confirmation_count: dict_i32(d, "confirmation_count", 0).max(0),
            confirmed: dict_bool(d, "confirmed", false),
            changed_at: dict_f64(d, "changed_at", 0.0),
            transition_age_sec: dict_f64(d, "transition_age_sec", 0.0).max(0.0),
        })
    }
}

#[derive(Clone, Debug)]
struct HmmConfig {
    n_states: usize,
    n_iter: usize,
    inference_window: usize,
    confidence_threshold: f64,
    retrain_interval_sec: f64,
    min_train_samples: usize,
    bias_gain: f64,
    blend_with_trend: f64,
}

impl Default for HmmConfig {
    fn default() -> Self {
        Self {
            n_states: 3,
            n_iter: 100,
            inference_window: 50,
            confidence_threshold: 0.15,
            retrain_interval_sec: 86400.0,
            min_train_samples: 500,
            bias_gain: 1.0,
            blend_with_trend: 0.5,
        }
    }
}

#[pyclass]
pub struct RegimeDetector {
    #[pyo3(get)]
    pub state: RegimeState,
    #[pyo3(get)]
    pub _trained: bool,
    #[pyo3(get)]
    pub _last_train_ts: f64,
    #[pyo3(get)]
    pub training_depth: i32,
    tertiary_transition: TertiaryTransition,

    cfg: HmmConfig,
    extractor: FeatureExtractor,
    model: Option<GaussianHmm>,
    label_map: Vec<usize>,
}

#[pymethods]
impl RegimeDetector {
    #[new]
    #[pyo3(signature = (config=None))]
    fn new(config: Option<&Bound<'_, PyDict>>) -> Self {
        let mut cfg = HmmConfig::default();
        if let Some(d) = config {
            let _requested_states = dict_usize(d, "HMM_N_STATES", cfg.n_states);
            cfg.n_states = 3;
            cfg.n_iter = dict_usize(d, "HMM_N_ITER", cfg.n_iter).max(10);
            cfg.inference_window = dict_usize(d, "HMM_INFERENCE_WINDOW", cfg.inference_window).max(5);
            cfg.confidence_threshold = dict_f64(d, "HMM_CONFIDENCE_THRESHOLD", cfg.confidence_threshold).max(0.0);
            cfg.retrain_interval_sec = dict_f64(d, "HMM_RETRAIN_INTERVAL_SEC", cfg.retrain_interval_sec).max(1.0);
            cfg.min_train_samples = dict_usize(d, "HMM_MIN_TRAIN_SAMPLES", cfg.min_train_samples).max(5);
            cfg.bias_gain = dict_f64(d, "HMM_BIAS_GAIN", cfg.bias_gain).max(0.0);
            cfg.blend_with_trend = clamp(
                dict_f64(d, "HMM_BLEND_WITH_TREND", cfg.blend_with_trend),
                0.0,
                1.0,
            );
        } else {
            cfg.n_states = 3;
        }

        Self {
            state: RegimeState {
                regime: Regime::RANGING as i32,
                probabilities: vec![0.0, 1.0, 0.0],
                confidence: 0.0,
                bias_signal: 0.0,
                last_update_ts: 0.0,
                observation_count: 0,
            },
            _trained: false,
            _last_train_ts: 0.0,
            training_depth: 0,
            tertiary_transition: TertiaryTransition::default(),
            cfg,
            extractor: FeatureExtractor::default(),
            model: None,
            label_map: vec![0, 1, 2],
        }
    }

    fn train(&mut self, closes: Vec<f64>, volumes: Vec<f64>) -> PyResult<bool> {
        let obs = self.extractor.extract_rows(&closes, &volumes)?;
        self.training_depth = i32::try_from(obs.len()).unwrap_or(i32::MAX);
        if obs.len() < self.cfg.min_train_samples {
            self._trained = false;
            return Ok(false);
        }

        let mut hmm = GaussianHmm::new(self.cfg.n_states, 4);
        if let Err(err) = hmm.fit(&obs, self.cfg.n_iter) {
            return Err(PyValueError::new_err(err));
        }

        self.label_map = hmm.label_map_by_feature(1).unwrap_or_else(|| vec![0, 1, 2]);
        self.training_depth = i32::try_from(hmm.training_depth()).unwrap_or(i32::MAX);
        self.model = Some(hmm);
        self._trained = true;
        self._last_train_ts = now_ts();
        self.state.observation_count = obs.len();
        Ok(true)
    }

    fn update(&mut self, closes: Vec<f64>, volumes: Vec<f64>) -> PyResult<RegimeState> {
        if !self._trained {
            return Ok(self.state.clone());
        }

        let obs = self.extractor.extract_rows(&closes, &volumes)?;
        if obs.is_empty() {
            return Ok(self.state.clone());
        }

        let start = obs.len().saturating_sub(self.cfg.inference_window);
        let tail = &obs[start..];

        let raw_probs = if let Some(model) = &self.model {
            if model.is_trained() {
                model.predict_last_proba(tail)
            } else {
                vec![0.0, 1.0, 0.0]
            }
        } else {
            vec![0.0, 1.0, 0.0]
        };

        let labeled_probs = self.remap_probs(&raw_probs);
        let p = normalize_probs([labeled_probs[0], labeled_probs[1], labeled_probs[2]]);
        let regime = argmax3(p) as i32;

        let mut sorted = [p[0], p[1], p[2]];
        sorted.sort_by(|a, b| b.partial_cmp(a).unwrap_or(std::cmp::Ordering::Equal));
        let confidence = sorted[0] - sorted[1];

        let bias_signal = if confidence < self.cfg.confidence_threshold {
            0.0
        } else {
            clamp((p[2] - p[0]) * self.cfg.bias_gain, -1.0, 1.0)
        };
        let updated_at = now_ts();

        self.state = RegimeState {
            regime,
            probabilities: vec![p[0], p[1], p[2]],
            confidence: round4(confidence),
            bias_signal: round4(bias_signal),
            last_update_ts: updated_at,
            observation_count: tail.len(),
        };
        self.advance_tertiary_transition(regime, updated_at);

        Ok(self.state.clone())
    }

    fn needs_retrain(&self) -> bool {
        if !self._trained {
            return true;
        }
        (now_ts() - self._last_train_ts) >= self.cfg.retrain_interval_sec
    }

    fn quality_tier(&self) -> String {
        quality_tier_for_depth(self.training_depth).to_string()
    }

    fn confidence_modifier(&self) -> f64 {
        confidence_modifier_for_depth(self.training_depth)
    }

    fn get_tertiary_transition(&self) -> TertiaryTransition {
        self.tertiary_transition.clone()
    }

    fn set_tertiary_transition(&mut self, transition: TertiaryTransition) {
        self.tertiary_transition = transition;
    }
}

pub(crate) fn quality_tier_for_depth(depth: i32) -> &'static str {
    let d = depth.max(0);
    if d <= TIER_SHALLOW_MAX {
        "shallow"
    } else if d <= TIER_BASELINE_MAX {
        "baseline"
    } else if d <= TIER_DEEP_MAX {
        "deep"
    } else {
        "full"
    }
}

pub(crate) fn confidence_modifier_for_depth(depth: i32) -> f64 {
    match quality_tier_for_depth(depth) {
        "shallow" => 0.70,
        "baseline" => 0.85,
        "deep" => 0.95,
        _ => 1.00,
    }
}

pub(crate) fn confidence_modifier_for_source(
    source: &str,
    primary_depth: i32,
    secondary_depth: i32,
    tertiary_depth: i32,
) -> f64 {
    let normalized = source.trim().to_lowercase();
    match normalized.as_str() {
        "secondary" | "15m" => confidence_modifier_for_depth(secondary_depth),
        "tertiary" | "1h" => confidence_modifier_for_depth(tertiary_depth),
        "consensus" | "consensus_min" => {
            confidence_modifier_for_depth(primary_depth).min(confidence_modifier_for_depth(secondary_depth))
        }
        _ => confidence_modifier_for_depth(primary_depth),
    }
}

impl RegimeDetector {
    fn remap_probs(&self, raw_probs: &[f64]) -> [f64; 3] {
        let mut labeled = [0.0, 0.0, 0.0];
        for (raw_idx, raw_prob) in raw_probs.iter().enumerate() {
            let label = self.label_map.get(raw_idx).copied().unwrap_or(1);
            if label < 3 {
                labeled[label] += *raw_prob;
            }
        }
        labeled
    }

    fn advance_tertiary_transition(&mut self, regime: i32, now: f64) {
        let norm_regime = normalize_regime_i32(regime).unwrap_or(Regime::RANGING as i32);
        if self.tertiary_transition.changed_at <= 0.0 {
            self.tertiary_transition = TertiaryTransition {
                from_regime: norm_regime,
                to_regime: norm_regime,
                confirmation_count: 1,
                confirmed: false,
                changed_at: now,
                transition_age_sec: 0.0,
            };
            return;
        }

        if norm_regime != self.tertiary_transition.to_regime {
            self.tertiary_transition = TertiaryTransition {
                from_regime: self.tertiary_transition.to_regime,
                to_regime: norm_regime,
                confirmation_count: 1,
                confirmed: false,
                changed_at: now,
                transition_age_sec: 0.0,
            };
            return;
        }

        let next_count = (self.tertiary_transition.confirmation_count + 1).max(1);
        let age_sec = (now - self.tertiary_transition.changed_at).max(0.0);
        self.tertiary_transition.confirmation_count = next_count;
        self.tertiary_transition.transition_age_sec = age_sec;
        self.tertiary_transition.confirmed = self.tertiary_transition.from_regime != self.tertiary_transition.to_regime
            && next_count >= 2;
    }

    pub(crate) fn snapshot(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let state_dict = self.state.to_dict(py)?;
        let tertiary_transition_dict = self.tertiary_transition.to_dict(py)?;
        let d = PyDict::new_bound(py);
        d.set_item("_hmm_regime_state", state_dict)?;
        d.set_item("_hmm_last_train_ts", self._last_train_ts)?;
        d.set_item("_hmm_trained", self._trained)?;
        d.set_item("_hmm_training_depth", self.training_depth)?;
        d.set_item(
            "_hmm_quality_tier",
            quality_tier_for_depth(self.training_depth),
        )?;
        d.set_item(
            "_hmm_confidence_modifier",
            confidence_modifier_for_depth(self.training_depth),
        )?;
        d.set_item("_hmm_tertiary_transition", tertiary_transition_dict)?;
        Ok(d.unbind())
    }

    pub(crate) fn restore_snapshot(&mut self, snapshot: &Bound<'_, PyDict>) -> PyResult<()> {
        if let Ok(Some(state_any)) = snapshot.get_item("_hmm_regime_state") {
            if let Ok(state_dict) = state_any.downcast::<PyDict>() {
                self.state = RegimeState::from_dict(&state_dict)?;
            }
        }

        self._last_train_ts = dict_f64(snapshot, "_hmm_last_train_ts", self._last_train_ts);
        self._trained = dict_bool(snapshot, "_hmm_trained", self._trained);
        self.training_depth = dict_i32(snapshot, "_hmm_training_depth", self.training_depth).max(0);
        if let Ok(Some(transition_any)) = snapshot.get_item("_hmm_tertiary_transition") {
            if let Ok(transition_dict) = transition_any.downcast::<PyDict>() {
                self.tertiary_transition = TertiaryTransition::from_dict(&transition_dict)?;
            }
        }
        Ok(())
    }
}

fn now_ts() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn round4(v: f64) -> f64 {
    (v * 10_000.0).round() / 10_000.0
}

fn argmax3(v: [f64; 3]) -> usize {
    if v[2] >= v[1] && v[2] >= v[0] {
        2
    } else if v[1] >= v[0] {
        1
    } else {
        0
    }
}

fn dict_f64(d: &Bound<'_, PyDict>, key: &str, default: f64) -> f64 {
    match d.get_item(key) {
        Ok(Some(v)) => v.extract::<f64>().unwrap_or(default),
        _ => default,
    }
}

fn dict_i32(d: &Bound<'_, PyDict>, key: &str, default: i32) -> i32 {
    match d.get_item(key) {
        Ok(Some(v)) => v.extract::<i32>().unwrap_or(default),
        _ => default,
    }
}

fn dict_regime_i32(d: &Bound<'_, PyDict>, key: &str, default: i32) -> i32 {
    match d.get_item(key) {
        Ok(Some(v)) => any_to_regime_i32(&v).unwrap_or(default),
        _ => default,
    }
}

fn dict_usize(d: &Bound<'_, PyDict>, key: &str, default: usize) -> usize {
    match d.get_item(key) {
        Ok(Some(v)) => v.extract::<usize>().unwrap_or(default),
        _ => default,
    }
}

fn dict_bool(d: &Bound<'_, PyDict>, key: &str, default: bool) -> bool {
    match d.get_item(key) {
        Ok(Some(v)) => v.extract::<bool>().unwrap_or(default),
        _ => default,
    }
}

fn dict_vec_f64(d: &Bound<'_, PyDict>, key: &str, default: Vec<f64>) -> Vec<f64> {
    match d.get_item(key) {
        Ok(Some(v)) => v.extract::<Vec<f64>>().unwrap_or(default),
        _ => default,
    }
}

fn normalize_regime_i32(value: i32) -> Option<i32> {
    if (Regime::BEARISH as i32..=Regime::BULLISH as i32).contains(&value) {
        Some(value)
    } else {
        None
    }
}

fn any_to_regime_i32(v: &Bound<'_, PyAny>) -> Option<i32> {
    if let Ok(i) = v.extract::<i32>() {
        return normalize_regime_i32(i);
    }
    if let Ok(f) = v.extract::<f64>() {
        return normalize_regime_i32(f.trunc() as i32);
    }
    if let Ok(s) = v.extract::<String>() {
        let trimmed = s.trim();
        if trimmed.is_empty() {
            return None;
        }
        if let Ok(i) = trimmed.parse::<i32>() {
            return normalize_regime_i32(i);
        }
        return match trimmed.to_ascii_uppercase().as_str() {
            "BEARISH" => Some(Regime::BEARISH as i32),
            "RANGING" => Some(Regime::RANGING as i32),
            "BULLISH" => Some(Regime::BULLISH as i32),
            _ => None,
        };
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quality_tiers_match_depth_ranges() {
        assert_eq!(quality_tier_for_depth(-10), "shallow");
        assert_eq!(quality_tier_for_depth(0), "shallow");
        assert_eq!(quality_tier_for_depth(999), "shallow");
        assert_eq!(quality_tier_for_depth(1000), "baseline");
        assert_eq!(quality_tier_for_depth(2499), "baseline");
        assert_eq!(quality_tier_for_depth(2500), "deep");
        assert_eq!(quality_tier_for_depth(3999), "deep");
        assert_eq!(quality_tier_for_depth(4000), "full");
    }

    #[test]
    fn confidence_modifiers_match_tiers() {
        assert!((confidence_modifier_for_depth(500) - 0.70).abs() < 1e-9);
        assert!((confidence_modifier_for_depth(1500) - 0.85).abs() < 1e-9);
        assert!((confidence_modifier_for_depth(3000) - 0.95).abs() < 1e-9);
        assert!((confidence_modifier_for_depth(5000) - 1.00).abs() < 1e-9);
    }

    #[test]
    fn confidence_modifier_for_source_uses_requested_pipeline() {
        let primary = 900;
        let secondary = 2800;
        let tertiary = 4200;
        assert!((confidence_modifier_for_source("primary", primary, secondary, tertiary) - 0.70).abs() < 1e-9);
        assert!((confidence_modifier_for_source("secondary", primary, secondary, tertiary) - 0.95).abs() < 1e-9);
        assert!((confidence_modifier_for_source("tertiary", primary, secondary, tertiary) - 1.00).abs() < 1e-9);
        assert!((confidence_modifier_for_source("consensus", primary, secondary, tertiary) - 0.70).abs() < 1e-9);
    }

    #[test]
    fn tertiary_transition_tracks_changes_and_confirmation() {
        let mut detector = RegimeDetector::new(None);
        detector.advance_tertiary_transition(Regime::RANGING as i32, 100.0);
        assert_eq!(detector.tertiary_transition.from_regime, Regime::RANGING as i32);
        assert_eq!(detector.tertiary_transition.to_regime, Regime::RANGING as i32);
        assert_eq!(detector.tertiary_transition.confirmation_count, 1);
        assert!(!detector.tertiary_transition.confirmed);

        detector.advance_tertiary_transition(Regime::BULLISH as i32, 160.0);
        assert_eq!(detector.tertiary_transition.from_regime, Regime::RANGING as i32);
        assert_eq!(detector.tertiary_transition.to_regime, Regime::BULLISH as i32);
        assert_eq!(detector.tertiary_transition.confirmation_count, 1);
        assert!(!detector.tertiary_transition.confirmed);

        detector.advance_tertiary_transition(Regime::BULLISH as i32, 220.0);
        assert_eq!(detector.tertiary_transition.confirmation_count, 2);
        assert!(detector.tertiary_transition.confirmed);
        assert!(detector.tertiary_transition.transition_age_sec >= 60.0);
    }
}
