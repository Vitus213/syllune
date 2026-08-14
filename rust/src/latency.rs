//! End-to-end latency gate: real cloud + Wayland injection trials with
//! per-stage timestamps, p50/p95/p99 aggregation and hard thresholds.
//! Without real credentials, audio or an injection target the gate is
//! marked unverified, never passed.

use serde::Serialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum TrialStage {
    Start,
    ConnectReady,
    UtteranceStart,
    FirstPartial,
    Stop,
    TailSent,
    FinishSent,
    FinalReceived,
    InjectionComplete,
}

#[derive(Debug, Clone, Serialize)]
pub struct TrialOutcome {
    pub id: usize,
    pub mode: String,
    pub stages: Vec<(TrialStage, f64)>,
    pub success: bool,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Copy, Serialize)]
pub struct LatencyThresholds {
    pub first_partial_p95_seconds: f64,
    pub stop_to_final_p50_seconds: f64,
    pub stop_to_inject_p99_seconds: f64,
    pub min_trials: usize,
}

impl Default for LatencyThresholds {
    fn default() -> Self {
        Self {
            first_partial_p95_seconds: 1.0,
            stop_to_final_p50_seconds: 0.6,
            stop_to_inject_p99_seconds: 1.0,
            min_trials: 100,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct LatencyGate {
    pub trials: usize,
    pub failed_trials: usize,
    pub excluded_non_quick: usize,
    pub unverified: bool,
    pub passed: bool,
    pub first_partial_p95: Option<f64>,
    pub stop_to_final_p50: Option<f64>,
    pub stop_to_inject_p99: Option<f64>,
    pub thresholds: LatencyThresholds,
    pub samples: Vec<TrialOutcome>,
}

/// Linear-interpolation percentile over a copy of the samples. Returns NaN
/// for empty input.
pub fn percentile(values: &[f64], p: f64) -> f64 {
    if values.is_empty() {
        return f64::NAN;
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(|left, right| left.partial_cmp(right).unwrap_or(std::cmp::Ordering::Equal));
    let rank = (p / 100.0) * (sorted.len() - 1) as f64;
    let lower = rank.floor() as usize;
    let upper = (lower + 1).min(sorted.len() - 1);
    let fraction = rank - lower as f64;
    sorted[lower] + fraction * (sorted[upper] - sorted[lower])
}

/// Aggregate trial outcomes into the latency gate. Only `quick`-mode
/// successful trials feed the percentile thresholds; failed trials and
/// insufficient sample counts keep the gate unpassed.
pub fn evaluate_gate(outcomes: &[TrialOutcome], thresholds: &LatencyThresholds) -> LatencyGate {
    let mut quick_success: Vec<&TrialOutcome> = Vec::new();
    let mut failed_trials = 0;
    let mut excluded_non_quick = 0;
    for outcome in outcomes {
        if outcome.mode != "quick" {
            excluded_non_quick += 1;
            continue;
        }
        if outcome.success {
            quick_success.push(outcome);
        } else {
            failed_trials += 1;
        }
    }

    let first_partials: Vec<f64> = quick_success
        .iter()
        .filter_map(|outcome| {
            stage_duration(outcome, TrialStage::UtteranceStart, TrialStage::FirstPartial)
        })
        .collect();
    let stop_to_finals: Vec<f64> = quick_success
        .iter()
        .filter_map(|outcome| {
            stage_duration(outcome, TrialStage::Stop, TrialStage::FinalReceived)
        })
        .collect();
    let stop_to_injects: Vec<f64> = quick_success
        .iter()
        .filter_map(|outcome| {
            stage_duration(outcome, TrialStage::Stop, TrialStage::InjectionComplete)
        })
        .collect();

    let first_partial_p95 = optional_percentile(&first_partials, 95.0);
    let stop_to_final_p50 = optional_percentile(&stop_to_finals, 50.0);
    let stop_to_inject_p99 = optional_percentile(&stop_to_injects, 99.0);

    let trials = quick_success.len() + failed_trials;
    let unverified = quick_success.len() < thresholds.min_trials;
    let within_budgets = [
        (first_partial_p95, thresholds.first_partial_p95_seconds),
        (stop_to_final_p50, thresholds.stop_to_final_p50_seconds),
        (stop_to_inject_p99, thresholds.stop_to_inject_p99_seconds),
    ]
    .iter()
    .all(|(value, budget)| match value {
        Some(value) => *value <= *budget,
        None => true,
    });
    let passed = !unverified && failed_trials == 0 && within_budgets;

    LatencyGate {
        trials,
        failed_trials,
        excluded_non_quick,
        unverified,
        passed,
        first_partial_p95,
        stop_to_final_p50,
        stop_to_inject_p99,
        thresholds: *thresholds,
        samples: outcomes.to_vec(),
    }
}

fn stage_duration(outcome: &TrialOutcome, from: TrialStage, to: TrialStage) -> Option<f64> {
    let start = outcome.stages.iter().find(|(stage, _)| *stage == from)?.1;
    let end = outcome.stages.iter().find(|(stage, _)| *stage == to)?.1;
    Some(end - start)
}

fn optional_percentile(values: &[f64], p: f64) -> Option<f64> {
    if values.is_empty() {
        None
    } else {
        Some(percentile(values, p))
    }
}

