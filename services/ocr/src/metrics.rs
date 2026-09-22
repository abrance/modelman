//! Dependency-free Prometheus text exposition.
//!
//! The registry only needs to answer three questions for this service:
//! how many requests ran, how long they took, and whether the engine held up.
//! A hand-rolled exposition keeps the dependency surface small and the output
//! stable.

use std::collections::{BTreeMap, HashSet};
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::Mutex;
use std::time::Instant;

/// Upper bounds of the request-duration histogram, in seconds.
pub const DURATION_BUCKETS: &[f64] = &[
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0,
];

#[derive(Default)]
struct Histogram {
    buckets: Vec<u64>,
    sum: f64,
    count: u64,
}

impl Histogram {
    /// Buckets are allocated lazily because `Default` cannot size the vector.
    fn sized() -> Self {
        Self {
            buckets: vec![0; DURATION_BUCKETS.len()],
            ..Default::default()
        }
    }
}

impl Histogram {
    fn observe(&mut self, value: f64) {
        for (idx, bound) in DURATION_BUCKETS.iter().enumerate() {
            if value <= *bound {
                self.buckets[idx] += 1;
            }
        }
        self.sum += value;
        self.count += 1;
    }
}

/// Labels render in a deterministic order, so the exposed text is stable and
/// can be diffed between releases.
type LabelSet = Vec<(&'static str, String)>;

fn render_labels(labels: &LabelSet, extra: Option<(&str, &str)>) -> String {
    let mut parts: Vec<String> = labels
        .iter()
        .map(|(k, v)| format!("{k}=\"{}\"", escape(v)))
        .collect();
    if let Some((k, v)) = extra {
        parts.push(format!("{k}=\"{}\"", escape(v)));
    }
    if parts.is_empty() {
        String::new()
    } else {
        format!("{{{}}}", parts.join(","))
    }
}

fn escape(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', "\\n")
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct RequestLabels {
    pub model: String,
    pub backend: String,
    pub status: String,
}

#[derive(Default)]
pub struct Metrics {
    started: Option<Instant>,
    requests: Mutex<BTreeMap<RequestLabels, u64>>,
    durations: Mutex<BTreeMap<(String, String), Histogram>>,
    lines_total: Mutex<BTreeMap<(String, String), u64>>,
    low_confidence_total: Mutex<BTreeMap<(String, String), u64>>,
    concurrency_rejected: Mutex<BTreeMap<(String, String), u64>>,
    loaded_models: Mutex<HashSet<(String, String)>>,
    /// Last load failure per tier, so `/models` can explain why a tier that
    /// ships in the image is not usable.
    load_errors: Mutex<BTreeMap<String, String>>,
    inflight: AtomicI64,
}

impl Metrics {
    pub fn new() -> Self {
        Self {
            started: Some(Instant::now()),
            inflight: AtomicI64::new(0),
            ..Default::default()
        }
    }

    pub fn on_request_start(&self) -> InflightGuard<'_> {
        self.inflight.fetch_add(1, Ordering::Relaxed);
        InflightGuard { metrics: self }
    }

    pub fn record_success(
        &self,
        model: &str,
        backend: &str,
        seconds: f64,
        lines: u64,
        low_conf: u64,
    ) {
        self.bump_request(model, backend, "ok");
        self.observe(model, backend, seconds);
        self.bump_counter(&self.lines_total, model, backend, lines);
        self.bump_counter(&self.low_confidence_total, model, backend, low_conf);
    }

    pub fn record_failure(&self, model: &str, backend: &str, kind: &str) {
        self.bump_request(model, backend, kind);
    }

    pub fn record_concurrency_rejected(&self, model: &str, backend: &str) {
        self.bump_counter(&self.concurrency_rejected, model, backend, 1);
        self.bump_request(model, backend, "overloaded");
    }

    pub fn set_model_loaded(&self, model: &str, backend: &str, loaded: bool) {
        let mut guard = self.loaded_models.lock().unwrap();
        let key = (model.to_string(), backend.to_string());
        if loaded {
            guard.insert(key);
        } else {
            guard.remove(&key);
        }
    }

    pub fn loaded(&self) -> Vec<(String, String)> {
        let mut values: Vec<_> = self.loaded_models.lock().unwrap().iter().cloned().collect();
        values.sort();
        values
    }

    pub fn record_load_error(&self, model: &str, message: &str) {
        self.load_errors
            .lock()
            .unwrap()
            .insert(model.to_string(), message.to_string());
    }

    pub fn clear_load_error(&self, model: &str) {
        self.load_errors.lock().unwrap().remove(model);
    }

    pub fn load_error(&self, model: &str) -> Option<String> {
        self.load_errors.lock().unwrap().get(model).cloned()
    }

    fn bump_request(&self, model: &str, backend: &str, status: &str) {
        let key = RequestLabels {
            model: model.to_string(),
            backend: backend.to_string(),
            status: status.to_string(),
        };
        *self.requests.lock().unwrap().entry(key).or_insert(0) += 1;
    }

    fn observe(&self, model: &str, backend: &str, seconds: f64) {
        self.durations
            .lock()
            .unwrap()
            .entry((model.to_string(), backend.to_string()))
            .or_insert_with(Histogram::sized)
            .observe(seconds);
    }

    fn bump_counter(
        &self,
        slot: &Mutex<BTreeMap<(String, String), u64>>,
        model: &str,
        backend: &str,
        delta: u64,
    ) {
        if delta == 0 {
            return;
        }
        *slot
            .lock()
            .unwrap()
            .entry((model.to_string(), backend.to_string()))
            .or_insert(0) += delta;
    }

    /// Render the registry in Prometheus text exposition format.
    pub fn render(&self) -> String {
        let mut out = String::with_capacity(2048);

        out.push_str("# HELP ocr_up 1 when the process is serving traffic.\n");
        out.push_str("# TYPE ocr_up gauge\n");
        out.push_str("ocr_up 1\n");

        out.push_str("# HELP ocr_uptime_seconds Seconds since process start.\n");
        out.push_str("# TYPE ocr_uptime_seconds gauge\n");
        let uptime = self
            .started
            .map(|s| s.elapsed().as_secs_f64())
            .unwrap_or_default();
        out.push_str(&format!("ocr_uptime_seconds {uptime:.3}\n"));

        out.push_str("# HELP ocr_requests_in_flight Requests currently being processed.\n");
        out.push_str("# TYPE ocr_requests_in_flight gauge\n");
        out.push_str(&format!(
            "ocr_requests_in_flight {}\n",
            self.inflight.load(Ordering::Relaxed)
        ));

        out.push_str("# HELP ocr_models_loaded Model tiers held in memory.\n");
        out.push_str("# TYPE ocr_models_loaded gauge\n");
        for (model, backend) in self.loaded() {
            let labels: LabelSet = vec![("model", model.clone()), ("backend", backend.clone())];
            out.push_str(&format!(
                "ocr_models_loaded{} 1\n",
                render_labels(&labels, None)
            ));
        }

        out.push_str("# HELP ocr_requests_total Completed recognition requests.\n");
        out.push_str("# TYPE ocr_requests_total counter\n");
        for (labels, value) in self.requests.lock().unwrap().iter() {
            let rendered: LabelSet = vec![
                ("model", labels.model.clone()),
                ("backend", labels.backend.clone()),
                ("status", labels.status.clone()),
            ];
            out.push_str(&format!(
                "ocr_requests_total{} {value}\n",
                render_labels(&rendered, None)
            ));
        }

        out.push_str("# HELP ocr_request_duration_seconds Recognition latency.\n");
        out.push_str("# TYPE ocr_request_duration_seconds histogram\n");
        for ((model, backend), hist) in self.durations.lock().unwrap().iter() {
            let labels: LabelSet = vec![("model", model.clone()), ("backend", backend.clone())];
            let mut cumulative = 0u64;
            for (idx, bound) in DURATION_BUCKETS.iter().enumerate() {
                cumulative += hist.buckets[idx];
                out.push_str(&format!(
                    "ocr_request_duration_seconds_bucket{} {cumulative}\n",
                    render_labels(&labels, Some(("le", &bound.to_string())))
                ));
            }
            out.push_str(&format!(
                "ocr_request_duration_seconds_bucket{} {}\n",
                render_labels(&labels, Some(("le", "+Inf"))),
                hist.count
            ));
            out.push_str(&format!(
                "ocr_request_duration_seconds_sum{} {}\n",
                render_labels(&labels, None),
                hist.sum
            ));
            out.push_str(&format!(
                "ocr_request_duration_seconds_count{} {}\n",
                render_labels(&labels, None),
                hist.count
            ));
        }

        out.push_str("# HELP ocr_text_lines_total Recognised text lines.\n");
        out.push_str("# TYPE ocr_text_lines_total counter\n");
        for ((model, backend), value) in self.lines_total.lock().unwrap().iter() {
            let labels: LabelSet = vec![("model", model.clone()), ("backend", backend.clone())];
            out.push_str(&format!(
                "ocr_text_lines_total{} {value}\n",
                render_labels(&labels, None)
            ));
        }

        out.push_str("# HELP ocr_low_confidence_lines_total Lines dropped below CONF_THRESHOLD.\n");
        out.push_str("# TYPE ocr_low_confidence_lines_total counter\n");
        for ((model, backend), value) in self.low_confidence_total.lock().unwrap().iter() {
            let labels: LabelSet = vec![("model", model.clone()), ("backend", backend.clone())];
            out.push_str(&format!(
                "ocr_low_confidence_lines_total{} {value}\n",
                render_labels(&labels, None)
            ));
        }

        out.push_str(
            "# HELP ocr_concurrency_rejected_total Requests rejected by MAX_CONCURRENCY.\n",
        );
        out.push_str("# TYPE ocr_concurrency_rejected_total counter\n");
        for ((model, backend), value) in self.concurrency_rejected.lock().unwrap().iter() {
            let labels: LabelSet = vec![("model", model.clone()), ("backend", backend.clone())];
            out.push_str(&format!(
                "ocr_concurrency_rejected_total{} {value}\n",
                render_labels(&labels, None)
            ));
        }

        out
    }
}

pub struct InflightGuard<'a> {
    metrics: &'a Metrics,
}

impl Drop for InflightGuard<'_> {
    fn drop(&mut self) {
        self.metrics.inflight.fetch_sub(1, Ordering::Relaxed);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn renders_counters_and_histogram_buckets() {
        let metrics = Metrics::new();
        metrics.set_model_loaded("v6small", "cpu", true);
        metrics.record_success("v6small", "cpu", 0.012, 3, 1);
        let text = metrics.render();

        assert!(text.contains("ocr_up 1"));
        assert!(text.contains("ocr_models_loaded{model=\"v6small\",backend=\"cpu\"} 1"));
        assert!(
            text.contains("ocr_requests_total{model=\"v6small\",backend=\"cpu\",status=\"ok\"} 1")
        );
        assert!(text
            .contains("ocr_request_duration_seconds_count{model=\"v6small\",backend=\"cpu\"} 1"));
        assert!(text.contains("le=\"0.025\""));
        assert!(text.contains("le=\"+Inf\""));
        assert!(text.contains("ocr_text_lines_total{model=\"v6small\",backend=\"cpu\"} 3"));
    }

    #[test]
    fn label_values_are_escaped() {
        assert_eq!(escape("a\"b\\c\nd"), "a\\\"b\\\\c\\nd");
    }
}
