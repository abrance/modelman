//! Regenerates the contract-test baselines from the currently built model.
//!
//! Run this only when a baseline change is intentional (new tier, new model
//! revision, changed preprocessing) and review the diff before committing:
//! `<case>.expected.json` is the reference the CI gate compares against.
//!
//! Usage:
//!   cargo run --release --bin gen-fixtures -- [fixtures_dir] [model]

use std::fs;
use std::path::PathBuf;
use std::time::Instant;

use ocr_service::config::{Backend, ServerConfig};
use ocr_service::AppState;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "ocr_service=warn".into()),
        )
        .init();

    let mut args = std::env::args().skip(1);
    let fixtures_dir: PathBuf = args
        .next()
        .unwrap_or_else(|| "tests/fixtures".to_string())
        .into();
    let model = args.next().unwrap_or_else(|| "v6small".to_string());

    let models_dir = std::env::var("MODELS_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("models"));

    if !fixtures_dir.is_dir() {
        eprintln!("fixtures directory not found: {}", fixtures_dir.display());
        std::process::exit(2);
    }

    let config = ServerConfig {
        models_dir,
        listen_addr: "127.0.0.1:0".to_string(),
        preload: vec![model.clone()],
        default_model: model.clone(),
        conf_threshold: ocr_service::config::DEFAULT_CONF_THRESHOLD,
        max_concurrency: 1,
        max_side: 0,
        max_image_bytes: ocr_service::config::DEFAULT_MAX_IMAGE_BYTES,
        queue_timeout: std::time::Duration::from_secs(30),
        auth_token: None,
    };

    println!(
        "generating baselines: fixtures={} model={} models_dir={}",
        fixtures_dir.display(),
        model,
        config.models_dir.display()
    );

    let state = AppState::new(config).await;

    let mut cases: Vec<PathBuf> = fs::read_dir(&fixtures_dir)
        .unwrap_or_else(|e| panic!("cannot read {}: {e}", fixtures_dir.display()))
        .filter_map(|entry| entry.ok().map(|e| e.path()))
        .filter(|path| {
            path.extension()
                .map(|ext| ext.eq_ignore_ascii_case("png") || ext.eq_ignore_ascii_case("jpg"))
                .unwrap_or(false)
        })
        .collect();
    cases.sort();

    if cases.is_empty() {
        eprintln!("no images found in {}", fixtures_dir.display());
        std::process::exit(2);
    }

    let mut durations_ms: Vec<f64> = Vec::new();
    let mut total_lines = 0usize;

    for path in &cases {
        let bytes =
            fs::read(path).unwrap_or_else(|e| panic!("cannot read {}: {e}", path.display()));
        let started = Instant::now();
        let recognition = state
            .recognize_bytes(&bytes, &model, Backend::Cpu)
            .await
            .unwrap_or_else(|err| panic!("OCR failed for {}: {}", path.display(), err.message()));
        let wall_ms = started.elapsed().as_secs_f64() * 1000.0;

        let case_name = path
            .file_stem()
            .map(|s| s.to_string_lossy().to_string())
            .unwrap_or_default();

        let payload = serde_json::json!({
            "model": model,
            "source_image": path.file_name().map(|s| s.to_string_lossy().to_string()),
            "conf_threshold": ocr_service::config::DEFAULT_CONF_THRESHOLD,
            "min_similarity": 0.95,
            "raw_line_count": recognition.raw_line_count,
            "inference_ms": recognition.inference_ms,
            "lines": recognition
                .lines
                .iter()
                .map(|line| {
                    serde_json::json!({
                        "text": line.text,
                        "confidence": line.confidence,
                    })
                })
                .collect::<Vec<_>>(),
        });

        let target = fixtures_dir.join(format!("{case_name}.expected.json"));
        fs::write(
            &target,
            format!("{}\n", serde_json::to_string_pretty(&payload).unwrap()),
        )
        .unwrap_or_else(|e| panic!("cannot write {}: {e}", target.display()));

        println!(
            "  {case_name}: {} lines kept, {} raw, {:.1} ms (wall {:.1} ms)",
            recognition.lines.len(),
            recognition.raw_line_count,
            recognition.inference_ms,
            wall_ms
        );

        durations_ms.push(recognition.inference_ms);
        total_lines += recognition.lines.len();
    }

    durations_ms.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let p50 = percentile(&durations_ms, 0.50);
    let p95 = percentile(&durations_ms, 0.95);

    let baseline = serde_json::json!({
        "model": model,
        "cases": cases.len(),
        "total_lines": total_lines,
        "inference_ms_p50": p50,
        "inference_ms_p95": p95,
        "peak_rss_kb": peak_rss_kb(),
        "note": "Regenerate with: cargo run --release --bin gen-fixtures",
    });

    let target = fixtures_dir.join("baseline.json");
    fs::write(
        &target,
        format!("{}\n", serde_json::to_string_pretty(&baseline).unwrap()),
    )
    .unwrap_or_else(|e| panic!("cannot write {}: {e}", target.display()));

    println!(
        "baseline written to {}: cases={} lines={} p50={:.1}ms p95={:.1}ms peak_rss={}kB",
        target.display(),
        cases.len(),
        total_lines,
        p50,
        p95,
        baseline["peak_rss_kb"]
    );
}

fn percentile(sorted: &[f64], fraction: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let index = ((sorted.len() as f64 - 1.0) * fraction).round() as usize;
    sorted[index.min(sorted.len() - 1)]
}

/// Peak resident set size, read from `/proc` so no allocator hook is needed.
pub fn peak_rss_kb() -> u64 {
    rss_field("VmHWM")
}

fn rss_field(field: &str) -> u64 {
    let Ok(status) = fs::read_to_string("/proc/self/status") else {
        return 0;
    };
    for line in status.lines() {
        if let Some(rest) = line.strip_prefix(field) {
            return rest
                .trim_start_matches(':')
                .split_whitespace()
                .next()
                .and_then(|v| v.parse().ok())
                .unwrap_or(0);
        }
    }
    0
}
