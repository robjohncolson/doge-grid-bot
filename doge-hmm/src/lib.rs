mod features;
mod hmm;
pub mod math;
mod regime;

use pyo3::prelude::*;
use pyo3::types::PyDict;

use regime::{RegimeDetector, RegimeState};

#[pyfunction]
fn compute_blended_idle_target(
    trend_score: f64,
    hmm_bias: f64,
    blend_factor: f64,
    base_target: f64,
    sensitivity: f64,
    floor: f64,
    ceiling: f64,
) -> f64 {
    let blend = blend_factor.clamp(0.0, 1.0);
    let blended = blend * trend_score + (1.0 - blend) * hmm_bias;
    (base_target - sensitivity * blended).clamp(floor, ceiling)
}

#[pyfunction]
#[pyo3(signature = (regime_state, confidence_threshold=0.15))]
fn compute_grid_bias(
    py: Python<'_>,
    regime_state: &RegimeState,
    confidence_threshold: f64,
) -> PyResult<Py<PyDict>> {
    let out = PyDict::new_bound(py);

    if regime_state.confidence < confidence_threshold {
        out.set_item("mode", "symmetric")?;
        out.set_item("entry_spacing_mult_a", 1.0)?;
        out.set_item("entry_spacing_mult_b", 1.0)?;
        out.set_item("size_skew_override", py.None())?;
        return Ok(out.unbind());
    }

    let bias = regime_state.bias_signal;
    if bias > 0.0 {
        out.set_item("mode", "long_bias")?;
        out.set_item("entry_spacing_mult_a", 1.0 + bias.abs() * 0.5)?;
        out.set_item("entry_spacing_mult_b", (1.0 - bias.abs() * 0.3).max(0.6))?;
        out.set_item("size_skew_override", (bias.abs() * 0.3).min(0.30))?;
        return Ok(out.unbind());
    }

    out.set_item("mode", "short_bias")?;
    out.set_item("entry_spacing_mult_a", (1.0 - bias.abs() * 0.3).max(0.6))?;
    out.set_item("entry_spacing_mult_b", 1.0 + bias.abs() * 0.5)?;
    out.set_item("size_skew_override", (-bias.abs() * 0.3).max(-0.30))?;
    Ok(out.unbind())
}

#[pyfunction]
fn serialize_for_snapshot(py: Python<'_>, detector: &RegimeDetector) -> PyResult<Py<PyDict>> {
    detector.snapshot(py)
}

#[pyfunction]
fn restore_from_snapshot(detector: &mut RegimeDetector, snapshot: &Bound<'_, PyDict>) -> PyResult<()> {
    detector.restore_snapshot(snapshot)
}

#[pymodule]
fn doge_hmm(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<regime::Regime>()?;
    m.add_class::<regime::RegimeState>()?;
    m.add_class::<features::FeatureExtractor>()?;
    m.add_class::<regime::RegimeDetector>()?;

    m.add_function(wrap_pyfunction!(compute_blended_idle_target, m)?)?;
    m.add_function(wrap_pyfunction!(compute_grid_bias, m)?)?;
    m.add_function(wrap_pyfunction!(serialize_for_snapshot, m)?)?;
    m.add_function(wrap_pyfunction!(restore_from_snapshot, m)?)?;

    Ok(())
}
