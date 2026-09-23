//! HTTP-level tests: routing, status codes, auth gate and multipart parsing.
//!
//! Recognition accuracy itself is covered by `contract.rs`; here the goal is
//! the wire contract that clients and the deployment pipeline depend on.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use ocr_service::config::{Backend, ServerConfig, DEFAULT_CONF_THRESHOLD, DEFAULT_MAX_IMAGE_BYTES};
use ocr_service::{build_router, AppState};
use tower::ServiceExt as _;

fn models_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("models")
}

fn fixtures_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")
}

fn sample_image() -> Vec<u8> {
    let dir = fixtures_dir();
    let mut images: Vec<PathBuf> = fs::read_dir(&dir)
        .expect("fixtures directory missing")
        .filter_map(|entry| entry.ok().map(|e| e.path()))
        .filter(|path| path.extension().is_some_and(|ext| ext == "png"))
        .collect();
    images.sort();
    let first = images.first().expect("no fixture png available");
    fs::read(first).expect("cannot read fixture")
}

async fn app(auth_token: Option<&str>) -> axum::Router {
    let config = ServerConfig {
        models_dir: models_dir(),
        listen_addr: "127.0.0.1:0".to_string(),
        preload: vec!["v6small".to_string()],
        default_model: "v6small".to_string(),
        conf_threshold: DEFAULT_CONF_THRESHOLD,
        max_concurrency: 2,
        max_side: 0,
        max_image_bytes: DEFAULT_MAX_IMAGE_BYTES,
        queue_timeout: std::time::Duration::from_secs(30),
        auth_token: auth_token.map(str::to_string),
    };
    let state: Arc<AppState> = AppState::new(config).await;
    build_router(state)
}

fn multipart_body(images: &[(&str, &[u8])]) -> (String, Vec<u8>) {
    let boundary = "----modelman-test-boundary";
    let mut body = Vec::new();
    for (name, bytes) in images {
        body.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
        body.extend_from_slice(
            format!("Content-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n")
                .as_bytes(),
        );
        body.extend_from_slice(b"Content-Type: image/png\r\n\r\n");
        body.extend_from_slice(bytes);
        body.extend_from_slice(b"\r\n");
    }
    body.extend_from_slice(format!("--{boundary}--\r\n").as_bytes());
    (boundary.to_string(), body)
}

async fn json_of(response: axum::response::Response) -> serde_json::Value {
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .expect("cannot read response body");
    serde_json::from_slice(&bytes).expect("response body is not JSON")
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn health_endpoints_are_reachable_without_auth() {
    let app = app(Some("shared-secret")).await;

    // 根路径必须也在内：反代与外部探活器常直接探 `/`，404 会被判成不健康。
    for path in ["/", "/health", "/healthz", "/livez"] {
        let response = app
            .clone()
            .oneshot(Request::builder().uri(path).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK, "{path}");
        let value = json_of(response).await;
        assert_eq!(value["status"], "ok", "{path}");
    }

    // /readyz must be green because the default tier was preloaded.
    let response = app
        .oneshot(
            Request::builder()
                .uri("/readyz")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let value = json_of(response).await;
    assert_eq!(value["status"], "ready");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn version_reports_build_provenance() {
    let app = app(None).await;
    let response = app
        .oneshot(
            Request::builder()
                .uri("/version")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);

    let value = json_of(response).await;
    assert_eq!(value["name"], "modelman-ocr");
    assert_eq!(value["default_model"], "v6small");
    assert!(value["git_commit"].as_str().is_some());
    #[cfg(not(feature = "vulkan"))]
    assert_eq!(value["gpu_supported"], false);
    assert_eq!(value["auth_required"], false);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn models_lists_every_shipped_tier() {
    let app = app(None).await;
    let response = app
        .oneshot(
            Request::builder()
                .uri("/models")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);

    let value = json_of(response).await;
    let items = value.as_array().expect("/models must return an array");
    assert_eq!(items.len(), 3);

    let ids: Vec<&str> = items
        .iter()
        .map(|item| item["id"].as_str().unwrap())
        .collect();
    assert_eq!(ids, vec!["v6tiny", "v6small", "v5"]);

    for item in items {
        assert_eq!(
            item["available"], true,
            "model files must ship in the image"
        );
        assert!(
            item["load_error"].is_null(),
            "no tier should have failed to load"
        );
    }
    assert_eq!(
        items.iter().find(|item| item["id"] == "v6small").unwrap()["loaded"],
        true
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn ocr_accepts_multipart_upload() {
    let app = app(None).await;
    let image = sample_image();
    let (boundary, body) = multipart_body(&[("case.png", &image)]);

    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/ocr")
                .header(
                    "content-type",
                    format!("multipart/form-data; boundary={boundary}"),
                )
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);

    let value = json_of(response).await;
    assert_eq!(value["success"], true, "error: {}", value["error"]);
    assert_eq!(value["model"], "v6small");
    assert_eq!(value["backend"], "cpu");
    assert!(
        !value["results"].as_array().unwrap().is_empty(),
        "expected at least one recognised line"
    );
    assert!(value["time_ms"].as_f64().unwrap() >= 0.0);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn ocr_batch_returns_one_item_per_upload() {
    let app = app(None).await;
    let image = sample_image();
    let (boundary, body) = multipart_body(&[("a.png", &image), ("b.png", &image)]);

    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/ocr/batch")
                .header(
                    "content-type",
                    format!("multipart/form-data; boundary={boundary}"),
                )
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);

    let value = json_of(response).await;
    assert_eq!(value["success"], true);
    let items = value["items"].as_array().unwrap();
    assert_eq!(items.len(), 2);
    assert_eq!(items[0]["filename"], "a.png");
    assert_eq!(items[1]["filename"], "b.png");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn unknown_model_is_a_client_error() {
    let app = app(None).await;
    let image = sample_image();
    let (boundary, body) = multipart_body(&[("case.png", &image)]);

    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/ocr?model=v9")
                .header(
                    "content-type",
                    format!("multipart/form-data; boundary={boundary}"),
                )
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);

    let value = json_of(response).await;
    assert_eq!(value["success"], false);
    assert!(value["error"].as_str().unwrap().contains("unknown model"));
}

#[cfg(not(feature = "vulkan"))]
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn gpu_backend_reports_a_clear_error() {
    let app = app(None).await;
    let image = sample_image();
    let (boundary, body) = multipart_body(&[("case.png", &image)]);

    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/ocr?backend=gpu")
                .header(
                    "content-type",
                    format!("multipart/form-data; boundary={boundary}"),
                )
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);

    let value = json_of(response).await;
    assert!(value["error"].as_str().unwrap().contains("backend 'gpu'"));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn undecodable_payload_is_rejected() {
    let app = app(None).await;
    let (boundary, body) = multipart_body(&[("broken.png", b"definitely not a png")]);

    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/ocr")
                .header(
                    "content-type",
                    format!("multipart/form-data; boundary={boundary}"),
                )
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);

    let value = json_of(response).await;
    assert_eq!(value["success"], false);
    assert!(value["error"]
        .as_str()
        .unwrap()
        .contains("Image decode failed"));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn auth_token_gates_ocr_and_metrics() {
    let app = app(Some("shared-secret")).await;
    let image = sample_image();
    let (boundary, body) = multipart_body(&[("case.png", &image)]);

    // Without a token: rejected.
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/ocr")
                .header(
                    "content-type",
                    format!("multipart/form-data; boundary={boundary}"),
                )
                .body(Body::from(body.clone()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);

    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/metrics")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);

    // With the bearer token: accepted.
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/ocr")
                .header(
                    "content-type",
                    format!("multipart/form-data; boundary={boundary}"),
                )
                .header("authorization", "Bearer shared-secret")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);

    let response = app
        .oneshot(
            Request::builder()
                .uri("/metrics")
                .header("x-auth-token", "shared-secret")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let text = String::from_utf8_lossy(&bytes);
    assert!(text.contains("ocr_requests_total"));
    assert!(text.contains("ocr_request_duration_seconds_bucket"));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn metrics_reflect_completed_requests() {
    let app = app(None).await;
    let image = sample_image();
    let (boundary, body) = multipart_body(&[("case.png", &image)]);

    let _ = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/ocr")
                .header(
                    "content-type",
                    format!("multipart/form-data; boundary={boundary}"),
                )
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();

    let response = app
        .oneshot(
            Request::builder()
                .uri("/metrics")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let text = String::from_utf8_lossy(&bytes);

    assert!(text.contains("status=\"ok\"} 1"), "metrics:\n{text}");
    assert!(text.contains("ocr_low_confidence_lines_total"));
}

#[test]
fn backend_parsing_rejects_unknown_values() {
    assert!(Backend::parse("cpu").is_ok());
    assert!(Backend::parse("gpu").is_ok());
    assert!(Backend::parse("tpu").is_err());
}
