use crate::math::ema::{clamp, diff, ema_series, rsi_series};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

#[pyclass]
#[derive(Clone, Debug)]
pub struct FeatureExtractor {
    fast_ema_periods: usize,
    slow_ema_periods: usize,
    macd_fast: usize,
    macd_slow: usize,
    macd_signal: usize,
    rsi_period: usize,
    volume_avg_period: usize,
}

#[pymethods]
impl FeatureExtractor {
    #[new]
    #[pyo3(signature = (
        fast_ema_periods=9,
        slow_ema_periods=21,
        macd_fast=12,
        macd_slow=26,
        macd_signal=9,
        rsi_period=14,
        volume_avg_period=20,
    ))]
    pub fn new(
        fast_ema_periods: usize,
        slow_ema_periods: usize,
        macd_fast: usize,
        macd_slow: usize,
        macd_signal: usize,
        rsi_period: usize,
        volume_avg_period: usize,
    ) -> Self {
        Self {
            fast_ema_periods,
            slow_ema_periods,
            macd_fast,
            macd_slow,
            macd_signal,
            rsi_period,
            volume_avg_period,
        }
    }

    pub fn extract(&self, closes: Vec<f64>, volumes: Vec<f64>) -> PyResult<Vec<Vec<f64>>> {
        let rows = self.extract_rows(&closes, &volumes)?;
        Ok(rows.into_iter().map(|r| vec![r[0], r[1], r[2], r[3]]).collect())
    }
}

impl Default for FeatureExtractor {
    fn default() -> Self {
        Self::new(9, 21, 12, 26, 9, 14, 20)
    }
}

impl FeatureExtractor {
    pub(crate) fn extract_rows(&self, closes: &[f64], volumes: &[f64]) -> PyResult<Vec<[f64; 4]>> {
        if closes.len() != volumes.len() {
            return Err(PyValueError::new_err("closes and volumes must be same length"));
        }
        if closes.len() < 2 {
            return Ok(Vec::new());
        }

        let fast_ema = ema_series(closes, self.fast_ema_periods.max(1));
        let slow_ema = ema_series(closes, self.slow_ema_periods.max(1));

        let macd_fast_ema = ema_series(closes, self.macd_fast.max(1));
        let macd_slow_ema = ema_series(closes, self.macd_slow.max(1));
        let mut macd_line = Vec::with_capacity(closes.len());
        for i in 0..closes.len() {
            macd_line.push(macd_fast_ema[i] - macd_slow_ema[i]);
        }
        let macd_signal = ema_series(&macd_line, self.macd_signal.max(1));
        let mut macd_hist = Vec::with_capacity(closes.len());
        for i in 0..closes.len() {
            macd_hist.push(macd_line[i] - macd_signal[i]);
        }
        let macd_hist_slope = diff(&macd_hist);

        let rsi_raw = rsi_series(closes, self.rsi_period.max(1));

        let vol_avg = ema_series(volumes, self.volume_avg_period.max(1));

        let mut out = Vec::with_capacity(closes.len());
        for i in 0..closes.len() {
            let slow = slow_ema[i];
            let ema_spread_pct = if slow.abs() <= 1e-10 {
                0.0
            } else {
                (fast_ema[i] - slow) / slow
            };

            let rsi_zone = if rsi_raw[i].is_finite() {
                clamp((rsi_raw[i] - 50.0) / 50.0, -1.0, 1.0)
            } else {
                f64::NAN
            };

            let denom = vol_avg[i].abs().max(1e-10);
            let volume_ratio = volumes[i] / denom;

            let row = [macd_hist_slope[i], ema_spread_pct, rsi_zone, volume_ratio];
            if row.iter().all(|v| v.is_finite()) {
                out.push(row);
            }
        }

        Ok(out)
    }
}
