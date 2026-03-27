pub fn normalize_probs(mut probs: [f64; 3]) -> [f64; 3] {
    for p in &mut probs {
        if !p.is_finite() || *p < 0.0 {
            *p = 0.0;
        }
    }

    let sum = probs[0] + probs[1] + probs[2];
    if sum <= 0.0 {
        return [0.0, 1.0, 0.0];
    }

    [probs[0] / sum, probs[1] / sum, probs[2] / sum]
}
