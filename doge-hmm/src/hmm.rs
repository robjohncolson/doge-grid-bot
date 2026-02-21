const EPS: f64 = 1e-12;
const MIN_VAR: f64 = 1e-6;

#[derive(Clone, Debug)]
pub struct GaussianHmm {
    n_states: usize,
    n_features: usize,
    trained: bool,
    training_depth: usize,
    initial_probs: Vec<f64>,
    transition_matrix: Vec<Vec<f64>>,
    means: Vec<Vec<f64>>,
    covars: Vec<Vec<f64>>,
}

impl GaussianHmm {
    pub fn new(n_states: usize, n_features: usize) -> Self {
        let states = n_states.max(2);
        let features = n_features.max(1);

        Self {
            n_states: states,
            n_features: features,
            trained: false,
            training_depth: 0,
            initial_probs: vec![1.0 / states as f64; states],
            transition_matrix: Self::default_transition(states),
            means: vec![vec![0.0; features]; states],
            covars: vec![vec![1.0; features]; states],
        }
    }

    pub fn fit(&mut self, observations: &[[f64; 4]], n_iter: usize) -> Result<(), String> {
        if observations.len() < 2 {
            return Err("need at least 2 observations".to_string());
        }
        if self.n_features != 4 {
            return Err("model expects 4-feature observations".to_string());
        }

        self.initialize_from_data(observations);
        let iters = n_iter.max(1);

        for _ in 0..iters {
            let emissions = self.emission_likelihoods(observations);
            let (alpha, scales) = self.forward_scaled(&emissions);
            let beta = self.backward_scaled(&emissions, &scales);
            let gamma = Self::compute_gamma(&alpha, &beta);
            let (xi_sum, gamma_sum_trans) = self.compute_xi_sums(&alpha, &beta, &emissions);

            self.initial_probs = gamma[0].clone();
            Self::normalize_probs_in_place(&mut self.initial_probs);

            for i in 0..self.n_states {
                if gamma_sum_trans[i] <= EPS {
                    continue;
                }
                for j in 0..self.n_states {
                    self.transition_matrix[i][j] = xi_sum[i][j] / gamma_sum_trans[i];
                }
                Self::normalize_probs_in_place(&mut self.transition_matrix[i]);
            }

            self.update_emissions(observations, &gamma);
        }

        self.trained = true;
        self.training_depth = observations.len();
        Ok(())
    }

    pub fn predict_last_proba(&self, observations: &[[f64; 4]]) -> Vec<f64> {
        if !self.trained || observations.is_empty() {
            return self.default_probs();
        }

        let emissions = self.emission_likelihoods(observations);
        let (alpha, _scales) = self.forward_scaled(&emissions);
        alpha
            .last()
            .cloned()
            .unwrap_or_else(|| self.default_probs())
    }

    pub fn label_map_by_feature(&self, feature_idx: usize) -> Option<Vec<usize>> {
        if !self.trained || self.n_states != 3 || feature_idx >= self.n_features {
            return None;
        }

        let mut order: Vec<usize> = (0..self.n_states).collect();
        order.sort_by(|a, b| {
            self.means[*a][feature_idx]
                .partial_cmp(&self.means[*b][feature_idx])
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        let mut map = vec![1usize; self.n_states];
        map[order[0]] = 0;
        map[order[1]] = 1;
        map[order[2]] = 2;
        Some(map)
    }

    pub fn is_trained(&self) -> bool {
        self.trained
    }

    pub fn training_depth(&self) -> usize {
        self.training_depth
    }

    fn default_probs(&self) -> Vec<f64> {
        if self.n_states == 3 {
            vec![0.0, 1.0, 0.0]
        } else {
            vec![1.0 / self.n_states as f64; self.n_states]
        }
    }

    fn default_transition(n_states: usize) -> Vec<Vec<f64>> {
        if n_states == 1 {
            return vec![vec![1.0]];
        }

        let mut matrix = vec![vec![0.0; n_states]; n_states];
        let off_diag = 0.20 / (n_states as f64 - 1.0);
        for i in 0..n_states {
            for j in 0..n_states {
                matrix[i][j] = if i == j { 0.80 } else { off_diag };
            }
        }
        matrix
    }

    fn initialize_from_data(&mut self, observations: &[[f64; 4]]) {
        let t_len = observations.len();

        let mut spread_indexed: Vec<(usize, f64)> = observations
            .iter()
            .enumerate()
            .map(|(i, row)| (i, row[1]))
            .collect();
        spread_indexed.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal));

        let mut global_mean = vec![0.0; self.n_features];
        for row in observations {
            for f in 0..self.n_features {
                global_mean[f] += row[f];
            }
        }
        for f in 0..self.n_features {
            global_mean[f] /= t_len as f64;
        }

        let mut global_var = vec![0.0; self.n_features];
        for row in observations {
            for f in 0..self.n_features {
                let d = row[f] - global_mean[f];
                global_var[f] += d * d;
            }
        }
        for f in 0..self.n_features {
            global_var[f] = (global_var[f] / t_len as f64).max(MIN_VAR);
        }

        for s in 0..self.n_states {
            let pos = ((s as f64 + 0.5) * t_len as f64 / self.n_states as f64).floor() as usize;
            let pos_idx = pos.min(t_len.saturating_sub(1));
            let obs_idx = spread_indexed[pos_idx].0;
            let seed = observations[obs_idx];

            for f in 0..self.n_features {
                self.means[s][f] = seed[f];
                self.covars[s][f] = global_var[f];
            }
        }

        self.initial_probs = vec![1.0 / self.n_states as f64; self.n_states];
        self.transition_matrix = Self::default_transition(self.n_states);
    }

    fn emission_likelihoods(&self, observations: &[[f64; 4]]) -> Vec<Vec<f64>> {
        let mut out = vec![vec![0.0; self.n_states]; observations.len()];

        for (t, row) in observations.iter().enumerate() {
            let mut log_probs = vec![0.0; self.n_states];
            let mut max_log = f64::NEG_INFINITY;
            for s in 0..self.n_states {
                let lp = self.gaussian_logpdf_diag(row, s);
                log_probs[s] = lp;
                if lp > max_log {
                    max_log = lp;
                }
            }
            for s in 0..self.n_states {
                out[t][s] = (log_probs[s] - max_log).exp().max(EPS);
            }
        }

        out
    }

    fn gaussian_logpdf_diag(&self, row: &[f64; 4], state: usize) -> f64 {
        let mut acc = 0.0;
        for f in 0..self.n_features {
            let var = self.covars[state][f].max(MIN_VAR);
            let diff = row[f] - self.means[state][f];
            acc += -0.5 * ((2.0 * std::f64::consts::PI * var).ln() + (diff * diff) / var);
        }
        acc
    }

    fn forward_scaled(&self, emissions: &[Vec<f64>]) -> (Vec<Vec<f64>>, Vec<f64>) {
        let t_len = emissions.len();
        let mut alpha = vec![vec![0.0; self.n_states]; t_len];
        let mut scales = vec![1.0; t_len];

        for s in 0..self.n_states {
            alpha[0][s] = self.initial_probs[s] * emissions[0][s];
        }
        let mut scale0 = alpha[0].iter().sum::<f64>();
        if scale0 <= EPS {
            scale0 = EPS;
        }
        scales[0] = scale0;
        for s in 0..self.n_states {
            alpha[0][s] /= scale0;
        }

        for t in 1..t_len {
            for j in 0..self.n_states {
                let mut sum_prev = 0.0;
                for i in 0..self.n_states {
                    sum_prev += alpha[t - 1][i] * self.transition_matrix[i][j];
                }
                alpha[t][j] = sum_prev * emissions[t][j];
            }
            let mut scale = alpha[t].iter().sum::<f64>();
            if scale <= EPS {
                scale = EPS;
            }
            scales[t] = scale;
            for j in 0..self.n_states {
                alpha[t][j] /= scale;
            }
        }

        (alpha, scales)
    }

    fn backward_scaled(&self, emissions: &[Vec<f64>], scales: &[f64]) -> Vec<Vec<f64>> {
        let t_len = emissions.len();
        let mut beta = vec![vec![0.0; self.n_states]; t_len];

        for s in 0..self.n_states {
            beta[t_len - 1][s] = 1.0;
        }

        for t in (0..(t_len - 1)).rev() {
            let scale_next = scales[t + 1].max(EPS);
            for i in 0..self.n_states {
                let mut sum_next = 0.0;
                for j in 0..self.n_states {
                    sum_next += self.transition_matrix[i][j] * emissions[t + 1][j] * beta[t + 1][j];
                }
                beta[t][i] = sum_next / scale_next;
            }
        }

        beta
    }

    fn compute_gamma(alpha: &[Vec<f64>], beta: &[Vec<f64>]) -> Vec<Vec<f64>> {
        let t_len = alpha.len();
        let n_states = alpha[0].len();
        let mut gamma = vec![vec![0.0; n_states]; t_len];

        for t in 0..t_len {
            let mut sum = 0.0;
            for s in 0..n_states {
                gamma[t][s] = alpha[t][s] * beta[t][s];
                sum += gamma[t][s];
            }
            if sum <= EPS {
                sum = EPS;
            }
            for s in 0..n_states {
                gamma[t][s] /= sum;
            }
        }

        gamma
    }

    fn compute_xi_sums(
        &self,
        alpha: &[Vec<f64>],
        beta: &[Vec<f64>],
        emissions: &[Vec<f64>],
    ) -> (Vec<Vec<f64>>, Vec<f64>) {
        let t_len = alpha.len();
        let mut xi_sum = vec![vec![0.0; self.n_states]; self.n_states];
        let mut gamma_sum_trans = vec![0.0; self.n_states];

        if t_len < 2 {
            return (xi_sum, gamma_sum_trans);
        }

        for t in 0..(t_len - 1) {
            let mut denom = 0.0;
            for i in 0..self.n_states {
                for j in 0..self.n_states {
                    denom += alpha[t][i]
                        * self.transition_matrix[i][j]
                        * emissions[t + 1][j]
                        * beta[t + 1][j];
                }
            }
            if denom <= EPS {
                continue;
            }

            for i in 0..self.n_states {
                let mut gamma_ti = 0.0;
                for j in 0..self.n_states {
                    let val = alpha[t][i]
                        * self.transition_matrix[i][j]
                        * emissions[t + 1][j]
                        * beta[t + 1][j]
                        / denom;
                    xi_sum[i][j] += val;
                    gamma_ti += val;
                }
                gamma_sum_trans[i] += gamma_ti;
            }
        }

        (xi_sum, gamma_sum_trans)
    }

    fn update_emissions(&mut self, observations: &[[f64; 4]], gamma: &[Vec<f64>]) {
        let t_len = observations.len();

        for s in 0..self.n_states {
            let mut gamma_sum = 0.0;
            for t in 0..t_len {
                gamma_sum += gamma[t][s];
            }
            if gamma_sum <= EPS {
                continue;
            }

            for f in 0..self.n_features {
                let mut num = 0.0;
                for t in 0..t_len {
                    num += gamma[t][s] * observations[t][f];
                }
                self.means[s][f] = num / gamma_sum;
            }

            for f in 0..self.n_features {
                let mut var_num = 0.0;
                let mean = self.means[s][f];
                for t in 0..t_len {
                    let d = observations[t][f] - mean;
                    var_num += gamma[t][s] * d * d;
                }
                self.covars[s][f] = (var_num / gamma_sum).max(MIN_VAR);
            }
        }
    }

    fn normalize_probs_in_place(values: &mut [f64]) {
        let mut sum = 0.0;
        for v in values.iter_mut() {
            if !v.is_finite() || *v < 0.0 {
                *v = 0.0;
            }
            sum += *v;
        }
        if sum <= EPS {
            let uniform = 1.0 / values.len().max(1) as f64;
            for v in values.iter_mut() {
                *v = uniform;
            }
            return;
        }
        for v in values.iter_mut() {
            *v /= sum;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::GaussianHmm;

    #[test]
    fn fit_and_predict_returns_normalized_probs() {
        let mut obs = Vec::new();
        for i in 0..40 {
            let x = i as f64;
            obs.push([0.0 + x * 0.001, -0.50 + x * 0.0005, -0.2, 1.0]);
        }
        for i in 0..40 {
            let x = i as f64;
            obs.push([0.0 + x * 0.001, 0.00 + x * 0.0002, 0.0, 1.0]);
        }
        for i in 0..40 {
            let x = i as f64;
            obs.push([0.0 + x * 0.001, 0.50 + x * 0.0004, 0.2, 1.0]);
        }

        let mut hmm = GaussianHmm::new(3, 4);
        assert!(hmm.fit(&obs, 12).is_ok());
        assert!(hmm.is_trained());

        let p = hmm.predict_last_proba(&obs);
        assert_eq!(p.len(), 3);
        let sum = p.iter().sum::<f64>();
        assert!((sum - 1.0).abs() < 1e-6);
        assert!(p.iter().all(|v| *v >= 0.0));
    }

    #[test]
    fn label_map_is_available_for_trained_3state_model() {
        let mut obs = Vec::new();
        for i in 0..120 {
            let x = i as f64;
            obs.push([0.0, -0.2 + x * 0.001, 0.0, 1.0]);
        }

        let mut hmm = GaussianHmm::new(3, 4);
        assert!(hmm.fit(&obs, 8).is_ok());
        let map = hmm.label_map_by_feature(1);
        assert!(map.is_some());
        let m = map.unwrap_or_default();
        assert_eq!(m.len(), 3);
    }
}
