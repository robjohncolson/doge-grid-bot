pub fn clamp(value: f64, lo: f64, hi: f64) -> f64 {
    if value < lo {
        lo
    } else if value > hi {
        hi
    } else {
        value
    }
}

pub fn ema_series(series: &[f64], span: usize) -> Vec<f64> {
    if series.is_empty() {
        return Vec::new();
    }

    let use_span = span.max(1) as f64;
    let alpha = 2.0 / (use_span + 1.0);
    let mut out = Vec::with_capacity(series.len());
    out.push(series[0]);

    for i in 1..series.len() {
        let prev = out[i - 1];
        out.push(alpha * series[i] + (1.0 - alpha) * prev);
    }

    out
}

pub fn diff(series: &[f64]) -> Vec<f64> {
    if series.is_empty() {
        return Vec::new();
    }

    let mut out = Vec::with_capacity(series.len());
    out.push(0.0);

    for i in 1..series.len() {
        out.push(series[i] - series[i - 1]);
    }

    out
}

pub fn rsi_series(closes: &[f64], period: usize) -> Vec<f64> {
    if closes.is_empty() {
        return Vec::new();
    }

    let n = closes.len();
    let p = period.max(1);
    if n <= p {
        return vec![f64::NAN; n];
    }

    let mut deltas = Vec::with_capacity(n - 1);
    for i in 1..n {
        deltas.push(closes[i] - closes[i - 1]);
    }

    let mut gains = vec![0.0; n - 1];
    let mut losses = vec![0.0; n - 1];
    for (i, d) in deltas.iter().enumerate() {
        if *d > 0.0 {
            gains[i] = *d;
        } else {
            losses[i] = -*d;
        }
    }

    let mut rsi = vec![f64::NAN; n];

    let mut avg_gain = gains[0..p].iter().sum::<f64>() / p as f64;
    let mut avg_loss = losses[0..p].iter().sum::<f64>() / p as f64;

    let rs0 = avg_gain / avg_loss.max(1e-10);
    rsi[p] = 100.0 - 100.0 / (1.0 + rs0);

    for i in (p + 1)..n {
        let gain = gains[i - 1];
        let loss = losses[i - 1];
        avg_gain = (avg_gain * (p as f64 - 1.0) + gain) / p as f64;
        avg_loss = (avg_loss * (p as f64 - 1.0) + loss) / p as f64;
        let rs = avg_gain / avg_loss.max(1e-10);
        rsi[i] = 100.0 - 100.0 / (1.0 + rs);
    }

    rsi
}
