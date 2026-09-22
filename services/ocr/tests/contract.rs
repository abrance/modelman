//! Contract tests.
//!
//! These are the gate that makes "swap the model" safe: a fixed corpus of
//! screenshots with a recorded baseline. A rebuild that changes recognition
//! output, drops lines, or degrades latency fails here instead of in
//! production.
//!
//! Regenerate baselines deliberately with:
//!   cargo run --release --bin gen-fixtures
//! and review the resulting diff.

use std::fs;
use std::path::{Path, PathBuf};

use ocr_service::config::{Backend, ServerConfig, DEFAULT_CONF_THRESHOLD, DEFAULT_MAX_IMAGE_BYTES};
use ocr_service::AppState;

fn fixtures_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")
}

fn models_dir() -> PathBuf {
    std::env::var("MODELS_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| Path::new(env!("CARGO_MANIFEST_DIR")).join("models"))
}

fn contract_model() -> String {
    std::env::var("CONTRACT_MODEL").unwrap_or_else(|_| "v6small".to_string())
}

async fn state_with(model: &str, models_dir: PathBuf) -> std::sync::Arc<AppState> {
    let config = ServerConfig {
        models_dir,
        listen_addr: "127.0.0.1:0".to_string(),
        preload: vec![model.to_string()],
        default_model: model.to_string(),
        conf_threshold: DEFAULT_CONF_THRESHOLD,
        max_concurrency: 2,
        max_side: 0,
        max_image_bytes: DEFAULT_MAX_IMAGE_BYTES,
        queue_timeout: std::time::Duration::from_secs(30),
        auth_token: None,
    };
    AppState::new(config).await
}

struct Case {
    name: String,
    image: PathBuf,
    expected_lines: Vec<String>,
    raw_line_count: usize,
    min_similarity: f64,
}

fn load_cases(dir: &Path) -> Vec<Case> {
    let mut cases = Vec::new();
    let mut images: Vec<PathBuf> = fs::read_dir(dir)
        .unwrap_or_else(|e| panic!("cannot read {}: {e}", dir.display()))
        .filter_map(|entry| entry.ok().map(|e| e.path()))
        .filter(|path| {
            path.extension()
                .map(|ext| ext.eq_ignore_ascii_case("png") || ext.eq_ignore_ascii_case("jpg"))
                .unwrap_or(false)
        })
        .collect();
    images.sort();

    for image in images {
        let name = image
            .file_stem()
            .map(|s| s.to_string_lossy().to_string())
            .unwrap_or_default();
        let expected_path = dir.join(format!("{name}.expected.json"));
        let raw = fs::read_to_string(&expected_path).unwrap_or_else(|e| {
            panic!(
                "missing baseline {}: {} (run `cargo run --release --bin gen-fixtures`)",
                expected_path.display(),
                e
            )
        });
        let parsed: serde_json::Value =
            serde_json::from_str(&raw).expect("baseline is not valid JSON");

        let expected_lines = parsed["lines"]
            .as_array()
            .expect("baseline.lines must be an array")
            .iter()
            .map(|line| {
                line["text"]
                    .as_str()
                    .expect("baseline line.text must be a string")
                    .to_string()
            })
            .collect::<Vec<_>>();

        cases.push(Case {
            name,
            image,
            raw_line_count: parsed["raw_line_count"].as_u64().unwrap_or(0) as usize,
            min_similarity: parsed["min_similarity"].as_f64().unwrap_or(0.95),
            expected_lines,
        });
    }

    assert!(
        !cases.is_empty(),
        "no fixtures found in {}; the contract test would pass vacuously",
        dir.display()
    );
    cases
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn recognition_matches_recorded_baseline() {
    let dir = fixtures_dir();
    let cases = load_cases(&dir);
    let model = contract_model();
    let state = state_with(&model, models_dir()).await;

    assert!(
        state.is_ready().await,
        "default model {model} failed to preload from {}",
        models_dir().display()
    );

    let mut failures: Vec<String> = Vec::new();
    let mut durations_ms: Vec<f64> = Vec::new();

    for case in &cases {
        let bytes = fs::read(&case.image)
            .unwrap_or_else(|e| panic!("cannot read {}: {e}", case.image.display()));
        let recognition = state
            .recognize_bytes(&bytes, &model, Backend::Cpu)
            .await
            .unwrap_or_else(|err| panic!("OCR failed for {}: {}", case.name, err.message()));

        durations_ms.push(recognition.inference_ms);

        let actual: Vec<String> = recognition
            .lines
            .iter()
            .map(|line| line.text.clone())
            .collect();
        let expected_text = case.expected_lines.join("\n");
        let actual_text = actual.join("\n");
        let similarity = similarity_ratio(&expected_text, &actual_text);

        if actual.len() != case.expected_lines.len() {
            failures.push(format!(
                "{}: line count {} != baseline {}\n  baseline: {:?}\n  actual:   {:?}",
                case.name,
                actual.len(),
                case.expected_lines.len(),
                case.expected_lines,
                actual
            ));
            continue;
        }

        if similarity < case.min_similarity {
            failures.push(format!(
                "{}: similarity {:.3} < required {:.3}\n  baseline: {:?}\n  actual:   {:?}",
                case.name, similarity, case.min_similarity, case.expected_lines, actual
            ));
        }

        if recognition.raw_line_count != case.raw_line_count {
            failures.push(format!(
                "{}: raw line count {} != baseline {} (detection stage changed)",
                case.name, recognition.raw_line_count, case.raw_line_count
            ));
        }
    }

    assert!(
        failures.is_empty(),
        "OCR output drifted from the recorded baseline for {}/{} cases:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );

    assert_latency_budget(&mut durations_ms, &dir, &model);
}

fn assert_latency_budget(durations_ms: &mut [f64], dir: &Path, model: &str) {
    let baseline_path = dir.join("baseline.json");
    let Ok(raw) = fs::read_to_string(&baseline_path) else {
        eprintln!(
            "no {}; skipping latency budget check",
            baseline_path.display()
        );
        return;
    };
    let parsed: serde_json::Value = serde_json::from_str(&raw).expect("baseline is not valid JSON");
    if parsed["model"].as_str() != Some(model) {
        eprintln!(
            "baseline was recorded for model {:?}, running {model}; skipping latency budget",
            parsed["model"]
        );
        return;
    }
    let Some(baseline_p95) = parsed["inference_ms_p95"].as_f64() else {
        return;
    };
    if baseline_p95 <= 0.0 {
        return;
    }

    durations_ms.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let index = ((durations_ms.len() as f64 - 1.0) * 0.95).round() as usize;
    let p95 = durations_ms[index.min(durations_ms.len() - 1)];

    // CI runners are shared and noisy, so the gate is deliberately generous:
    // it catches order-of-magnitude regressions, not jitter.
    let ceiling = baseline_p95 * 6.0;
    assert!(
        p95 <= ceiling,
        "p95 inference {p95:.1} ms exceeds {ceiling:.1} ms (baseline {baseline_p95:.1} ms x6)"
    );
    println!("contract latency p95 {p95:.1} ms (baseline {baseline_p95:.1} ms)");
}

/// Character-level similarity in `0.0..=1.0`, used instead of exact equality so
/// a single glyph flip is reported as a degraded score rather than a crash.
fn similarity_ratio(a: &str, b: &str) -> f64 {
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    if a.is_empty() && b.is_empty() {
        return 1.0;
    }
    let distance = levenshtein(&a, &b);
    let longest = a.len().max(b.len()) as f64;
    1.0 - (distance as f64 / longest)
}

fn levenshtein(a: &[char], b: &[char]) -> usize {
    let mut previous: Vec<usize> = (0..=b.len()).collect();
    let mut current = vec![0usize; b.len() + 1];
    for (i, ca) in a.iter().enumerate() {
        current[0] = i + 1;
        for (j, cb) in b.iter().enumerate() {
            let cost = usize::from(ca != cb);
            current[j + 1] = (previous[j + 1] + 1)
                .min(current[j] + 1)
                .min(previous[j] + cost);
        }
        std::mem::swap(&mut previous, &mut current);
    }
    previous[b.len()]
}

#[test]
fn similarity_ratio_is_sane() {
    assert_eq!(similarity_ratio("abc", "abc"), 1.0);
    assert_eq!(similarity_ratio("", ""), 1.0);
    assert!(similarity_ratio("abc", "abd") > 0.6);
    assert!(similarity_ratio("abc", "xyz") < 0.1);
}

#[test]
fn levenshtein_matches_known_values() {
    let a: Vec<char> = "kitten".chars().collect();
    let b: Vec<char> = "sitting".chars().collect();
    assert_eq!(levenshtein(&a, &b), 3);
}
