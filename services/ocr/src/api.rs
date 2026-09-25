//! HTTP surface.
//!
//! The `/ocr` response shape is byte-compatible with the service this
//! replaces, so existing clients keep working; the remaining endpoints are
//! additions used by the deployment pipeline and by operators.

use std::sync::Arc;
use std::time::Instant;

use axum::body::Body;
use axum::extract::{DefaultBodyLimit, Multipart, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};

use crate::config::{Backend, ModelTier, ServerConfig, MODEL_TIERS};
use crate::engine::{decode_image, EngineError, EngineKey, EngineManager, Recognition};
use crate::metrics::Metrics;

pub const NAME: &str = "modelman-ocr";
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
pub const GIT_COMMIT: &str = env!("GIT_COMMIT");
pub const BUILD_TIME: &str = env!("BUILD_TIME");

pub struct AppState {
    pub config: ServerConfig,
    pub engines: EngineManager,
    pub metrics: Metrics,
    started: Instant,
}

impl AppState {
    /// Builds the state. Preloading failures are logged rather than fatal:
    /// a missing optional tier must not stop the process from serving, and
    /// readiness stays false until the default tier is resident.
    pub async fn new(config: ServerConfig) -> Arc<Self> {
        let engines = EngineManager::new(&config);
        let state = Arc::new(Self {
            engines,
            metrics: Metrics::new(),
            config,
            started: Instant::now(),
        });

        for tier in state.config.preload.clone() {
            let key = EngineKey::new(&tier, Backend::Cpu);
            if let Err(err) = state.engines.ensure_loaded(&key, &state.metrics).await {
                tracing::warn!(model = %tier, error = %err, "preload failed");
            }
        }

        state
    }

    pub fn uptime_secs(&self) -> u64 {
        self.started.elapsed().as_secs()
    }

    /// Ready means the configured default tier is resident, so the first real
    /// request will not pay the load cost.
    pub async fn is_ready(&self) -> bool {
        self.engines
            .is_loaded(&EngineKey::new(&self.config.default_model, Backend::Cpu))
            .await
    }

    pub fn loaded_labels(&self) -> Vec<String> {
        self.metrics
            .loaded()
            .into_iter()
            .map(|(model, backend)| format!("{model}/{backend}"))
            .collect()
    }

    /// Core entry point shared by `/ocr`, `/ocr/batch` and the contract tests.
    pub async fn recognize_bytes(
        &self,
        bytes: &[u8],
        model: &str,
        backend: Backend,
    ) -> Result<Recognition, ApiError> {
        if bytes.is_empty() {
            return Err(ApiError::Upload("uploaded image is empty".to_string()));
        }
        if bytes.len() > self.config.max_image_bytes {
            return Err(ApiError::Upload(format!(
                "image is {} bytes which exceeds MAX_IMAGE_BYTES={}",
                bytes.len(),
                self.config.max_image_bytes
            )));
        }

        let _inflight = self.metrics.on_request_start();
        let image = match decode_image(bytes, self.config.max_side) {
            Ok(image) => image,
            Err(reason) => {
                self.metrics
                    .record_failure(model, backend.as_str(), "bad_image");
                return Err(ApiError::Decode(reason));
            }
        };

        let key = EngineKey::new(model, backend);
        match self.engines.recognize(&key, image, &self.metrics).await {
            Ok(recognition) => {
                self.metrics.clear_load_error(model);
                self.metrics.record_success(
                    model,
                    backend.as_str(),
                    recognition.inference_ms / 1000.0,
                    recognition.lines.len() as u64,
                    recognition.lines_dropped(),
                );
                Ok(recognition)
            }
            Err(err) => {
                self.metrics
                    .record_failure(model, backend.as_str(), err.status_label());
                if matches!(
                    err,
                    EngineError::LoadFailed { .. } | EngineError::ModelFileMissing { .. }
                ) {
                    self.metrics.record_load_error(model, &err.to_string());
                }
                Err(ApiError::Engine(err))
            }
        }
    }
}

// ─── Requests and responses ─────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
pub struct OcrQuery {
    pub model: Option<String>,
    pub backend: Option<String>,
}

impl OcrQuery {
    fn resolve(&self, config: &ServerConfig) -> Result<(String, Backend), ApiError> {
        let model = self
            .model
            .clone()
            .unwrap_or_else(|| config.default_model.clone());
        if ModelTier::lookup(&model).is_none() {
            return Err(ApiError::Engine(EngineError::UnknownModel(model)));
        }
        let backend = match self.backend.as_deref() {
            Some(raw) => Backend::parse(raw)?,
            None => Backend::Cpu,
        };
        Ok((model, backend))
    }
}

#[derive(Debug, Serialize, Deserialize)]
pub struct Bbox {
    pub left: i32,
    pub top: i32,
    pub width: u32,
    pub height: u32,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct OcrLine {
    pub text: String,
    pub confidence: f32,
    pub bbox: Option<Bbox>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct OcrResponse {
    pub success: bool,
    pub results: Vec<OcrLine>,
    pub time_ms: f64,
    pub model: String,
    pub backend: String,
    pub error: Option<String>,
}

impl OcrResponse {
    fn success(model: &str, backend: Backend, recognition: Recognition) -> Self {
        Self {
            success: true,
            results: recognition
                .lines
                .into_iter()
                .map(|line| OcrLine {
                    text: line.text,
                    confidence: line.confidence,
                    bbox: line.bbox.map(|b| Bbox {
                        left: b.left,
                        top: b.top,
                        width: b.width,
                        height: b.height,
                    }),
                })
                .collect(),
            time_ms: recognition.inference_ms,
            model: model.to_string(),
            backend: backend.as_str().to_string(),
            error: None,
        }
    }

    fn failure(model: &str, backend: Backend, error: String) -> Self {
        Self {
            success: false,
            results: Vec::new(),
            time_ms: 0.0,
            model: model.to_string(),
            backend: backend.as_str().to_string(),
            error: Some(error),
        }
    }
}

#[derive(Debug, Serialize)]
pub struct HealthResponse {
    pub status: String,
    pub models_loaded: Vec<String>,
    pub uptime_secs: u64,
}

#[derive(Debug, Serialize)]
pub struct VersionResponse {
    pub name: &'static str,
    pub version: &'static str,
    pub git_commit: &'static str,
    pub build_time: &'static str,
    pub default_model: String,
    pub conf_threshold: f32,
    pub max_concurrency: usize,
    pub max_side: u32,
    pub max_image_bytes: usize,
    pub gpu_supported: bool,
    pub auth_required: bool,
    pub models_dir: String,
}

#[derive(Debug, Serialize)]
pub struct ModelInfo {
    pub id: &'static str,
    pub label: &'static str,
    pub det: &'static str,
    pub rec: &'static str,
    pub keys: &'static str,
    pub available: bool,
    pub loaded: bool,
    /// Populated when a load attempt failed, so an operator sees the reason
    /// without reading container logs.
    pub load_error: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct BatchItem {
    pub index: usize,
    pub filename: Option<String>,
    pub success: bool,
    pub results: Vec<OcrLine>,
    pub time_ms: f64,
    pub error: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct BatchResponse {
    pub success: bool,
    pub model: String,
    pub backend: String,
    pub total_time_ms: f64,
    pub items: Vec<BatchItem>,
}

// ─── Errors ─────────────────────────────────────────────────────────────────

#[derive(Debug)]
pub enum ApiError {
    /// Malformed or oversized upload: a client problem.
    Upload(String),
    /// Undecodable image payload: a client problem.
    Decode(String),
    /// Anything raised by the engine layer.
    Engine(EngineError),
    /// A query parameter could not be interpreted.
    Query(String),
    /// Missing or wrong bearer token.
    Unauthorized,
}

impl From<EngineError> for ApiError {
    fn from(value: EngineError) -> Self {
        ApiError::Engine(value)
    }
}

impl From<String> for ApiError {
    fn from(value: String) -> Self {
        ApiError::Query(value)
    }
}

impl ApiError {
    pub fn status(&self) -> StatusCode {
        match self {
            ApiError::Upload(_) | ApiError::Decode(_) | ApiError::Query(_) => {
                StatusCode::BAD_REQUEST
            }
            ApiError::Unauthorized => StatusCode::UNAUTHORIZED,
            ApiError::Engine(EngineError::Overloaded { .. }) => StatusCode::SERVICE_UNAVAILABLE,
            ApiError::Engine(EngineError::UnknownModel(_))
            | ApiError::Engine(EngineError::ModelFileMissing { .. })
            | ApiError::Engine(EngineError::GpuUnavailable) => StatusCode::BAD_REQUEST,
            ApiError::Engine(_) => StatusCode::INTERNAL_SERVER_ERROR,
        }
    }

    pub fn message(&self) -> String {
        match self {
            ApiError::Upload(reason) | ApiError::Decode(reason) | ApiError::Query(reason) => {
                reason.clone()
            }
            ApiError::Unauthorized => "missing or invalid auth token".to_string(),
            ApiError::Engine(err) => err.to_string(),
        }
    }
}

// ─── Handlers ───────────────────────────────────────────────────────────────

async fn health(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    Json(HealthResponse {
        status: "ok".to_string(),
        models_loaded: state.loaded_labels(),
        uptime_secs: state.uptime_secs(),
    })
}

async fn livez() -> impl IntoResponse {
    (StatusCode::OK, Json(serde_json::json!({ "status": "ok" })))
}

async fn readyz(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    if state.is_ready().await {
        (
            StatusCode::OK,
            Json(serde_json::json!({
                "status": "ready",
                "default_model": state.config.default_model,
            })),
        )
    } else {
        (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({
                "status": "starting",
                "default_model": state.config.default_model,
            })),
        )
    }
}

async fn version(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    Json(VersionResponse {
        name: NAME,
        version: VERSION,
        git_commit: GIT_COMMIT,
        build_time: BUILD_TIME,
        default_model: state.config.default_model.clone(),
        conf_threshold: state.config.conf_threshold,
        max_concurrency: state.config.max_concurrency,
        max_side: state.config.max_side,
        max_image_bytes: state.config.max_image_bytes,
        gpu_supported: crate::engine::GPU_SUPPORTED,
        auth_required: state.config.auth_token.is_some(),
        models_dir: state.config.models_dir.display().to_string(),
    })
}

async fn models(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    let loaded = state.metrics.loaded();
    let items: Vec<ModelInfo> = MODEL_TIERS
        .iter()
        .map(|tier| ModelInfo {
            id: tier.id,
            label: tier.label,
            det: tier.det,
            rec: tier.rec,
            keys: tier.keys,
            available: tier.det_path(&state.config.models_dir).exists()
                && tier.rec_path(&state.config.models_dir).exists()
                && tier.keys_path(&state.config.models_dir).exists(),
            loaded: loaded.iter().any(|(model, _)| model == tier.id),
            load_error: state.metrics.load_error(tier.id),
        })
        .collect();
    Json(items)
}

async fn metrics(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    Response::builder()
        .status(StatusCode::OK)
        .header("content-type", "text/plain; version=0.0.4; charset=utf-8")
        .body(Body::from(state.metrics.render()))
        .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response())
}

async fn ocr(
    State(state): State<Arc<AppState>>,
    Query(query): Query<OcrQuery>,
    multipart: Multipart,
) -> Response {
    let (model, backend) = match query.resolve(&state.config) {
        Ok(resolved) => resolved,
        Err(err) => {
            return (
                err.status(),
                Json(OcrResponse::failure(
                    query.model.as_deref().unwrap_or("unknown"),
                    Backend::Cpu,
                    err.message(),
                )),
            )
                .into_response();
        }
    };

    let uploads = match collect_uploads(multipart, state.config.max_image_bytes).await {
        Ok(uploads) => uploads,
        Err(err) => {
            let message = err.message();
            return (
                err.status(),
                Json(OcrResponse::failure(&model, backend, message)),
            )
                .into_response();
        }
    };

    let Some(first) = uploads.into_iter().next() else {
        return (
            StatusCode::BAD_REQUEST,
            Json(OcrResponse::failure(
                &model,
                backend,
                "Missing 'image' field in multipart form".to_string(),
            )),
        )
            .into_response();
    };

    match state.recognize_bytes(&first.bytes, &model, backend).await {
        Ok(recognition) => (
            StatusCode::OK,
            Json(OcrResponse::success(&model, backend, recognition)),
        )
            .into_response(),
        Err(err) => (
            err.status(),
            Json(OcrResponse::failure(&model, backend, err.message())),
        )
            .into_response(),
    }
}

async fn ocr_batch(
    State(state): State<Arc<AppState>>,
    Query(query): Query<OcrQuery>,
    multipart: Multipart,
) -> Response {
    let (model, backend) = match query.resolve(&state.config) {
        Ok(resolved) => resolved,
        Err(err) => {
            return (
                err.status(),
                Json(BatchResponse {
                    success: false,
                    model: err_model(&query),
                    backend: "cpu".to_string(),
                    total_time_ms: 0.0,
                    items: Vec::new(),
                }),
            )
                .into_response();
        }
    };

    let uploads = match collect_uploads(multipart, state.config.max_image_bytes).await {
        Ok(uploads) => uploads,
        Err(err) => {
            return (
                err.status(),
                Json(BatchResponse {
                    success: false,
                    model: model.clone(),
                    backend: backend.as_str().to_string(),
                    total_time_ms: 0.0,
                    items: Vec::new(),
                }),
            )
                .into_response();
        }
    };

    if uploads.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(BatchResponse {
                success: false,
                model: model.clone(),
                backend: backend.as_str().to_string(),
                total_time_ms: 0.0,
                items: Vec::new(),
            }),
        )
            .into_response();
    }

    let started = Instant::now();
    let mut items = Vec::with_capacity(uploads.len());
    let mut all_ok = true;

    for (index, upload) in uploads.into_iter().enumerate() {
        match state.recognize_bytes(&upload.bytes, &model, backend).await {
            Ok(recognition) => items.push(BatchItem {
                index,
                filename: upload.filename,
                success: true,
                results: OcrResponse::success(&model, backend, recognition).results,
                time_ms: 0.0,
                error: None,
            }),
            Err(err) => {
                all_ok = false;
                items.push(BatchItem {
                    index,
                    filename: upload.filename,
                    success: false,
                    results: Vec::new(),
                    time_ms: 0.0,
                    error: Some(err.message()),
                });
            }
        }
    }

    (
        StatusCode::OK,
        Json(BatchResponse {
            success: all_ok,
            model,
            backend: backend.as_str().to_string(),
            total_time_ms: started.elapsed().as_secs_f64() * 1000.0,
            items,
        }),
    )
        .into_response()
}

fn err_model(query: &OcrQuery) -> String {
    query.model.clone().unwrap_or_else(|| "unknown".to_string())
}

struct Upload {
    filename: Option<String>,
    bytes: Vec<u8>,
}

/// Reads every non-empty file field. The previous implementation accepted the
/// first non-empty field regardless of name; that leniency is preserved so
/// clients that send a differently named field keep working.
async fn collect_uploads(
    mut multipart: Multipart,
    max_image_bytes: usize,
) -> Result<Vec<Upload>, ApiError> {
    let mut uploads = Vec::new();
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|e| ApiError::Upload(format!("multipart read failed: {e}")))?
    {
        let filename = field.file_name().map(str::to_string);
        let len_hint = field
            .headers()
            .get("content-length")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.parse::<usize>().ok());
        if let Some(len) = len_hint {
            if len > max_image_bytes {
                return Err(ApiError::Upload(format!(
                    "image is {len} bytes which exceeds MAX_IMAGE_BYTES={max_image_bytes}"
                )));
            }
        }
        let bytes = field
            .bytes()
            .await
            .map_err(|e| ApiError::Upload(format!("reading an upload failed: {e}")))?;
        if bytes.is_empty() {
            continue;
        }
        uploads.push(Upload {
            filename,
            bytes: bytes.to_vec(),
        });
    }
    Ok(uploads)
}

// ─── Auth ───────────────────────────────────────────────────────────────────

fn token_from_headers(headers: &HeaderMap) -> Option<String> {
    if let Some(value) = headers.get("x-auth-token").and_then(|v| v.to_str().ok()) {
        return Some(value.trim().to_string());
    }
    headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "))
        .map(|value| value.trim().to_string())
}

fn authorize(state: &AppState, headers: &HeaderMap) -> Option<ApiError> {
    let expected = state.config.auth_token.as_deref()?;
    match token_from_headers(headers) {
        Some(provided) if constant_time_eq(provided.as_bytes(), expected.as_bytes()) => None,
        _ => Some(ApiError::Unauthorized),
    }
}

/// Length-independent comparison for the shared token.
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

// ─── Router ─────────────────────────────────────────────────────────────────

pub fn build_router(state: Arc<AppState>) -> Router {
    let body_limit = state
        .config
        .max_image_bytes
        .saturating_mul(4)
        .max(1024 * 1024);
    let guarded = Router::new()
        .route("/ocr", post(ocr))
        .route("/ocr/batch", post(ocr_batch))
        .route("/metrics", get(metrics))
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            require_auth,
        ));

    // `/health` mirrors the legacy endpoint; `/healthz` is what deployment
    // tooling probes. All of them stay reachable without a token so a
    // container can be health-checked before credentials are injected.
    // 根路径放自带页面：反代与外部探活器常直接探 `/`，只要这里还是 200 就不会被
    // 当成不健康（要 JSON 的存活响应仍然有 `/livez`）。
    // 免鉴权是刻意的：页面得先能打开，才谈得上填 token。
    // 三份资源在编译期嵌进二进制，见 docs/ocr-ui.md。
    let open = Router::new()
        .route("/", get(crate::ui::index))
        .route("/app.css", get(crate::ui::css))
        .route("/app.js", get(crate::ui::js))
        .route("/health", get(health))
        .route("/healthz", get(health))
        .route("/livez", get(livez))
        .route("/readyz", get(readyz))
        .route("/version", get(version))
        .route("/models", get(models));

    open.merge(guarded)
        .layer(DefaultBodyLimit::max(body_limit))
        .layer(
            tower_http::trace::TraceLayer::new_for_http().make_span_with(
                tower_http::trace::DefaultMakeSpan::new()
                    .level(tracing::Level::INFO)
                    .include_headers(false),
            ),
        )
        .layer(tower_http::cors::CorsLayer::permissive())
        .with_state(state)
}

async fn require_auth(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    request: axum::extract::Request,
    next: axum::middleware::Next,
) -> Response {
    if let Some(err) = authorize(&state, &headers) {
        return (
            err.status(),
            Json(serde_json::json!({ "error": err.message() })),
        )
            .into_response();
    }
    next.run(request).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn constant_time_eq_matches_only_equal_inputs() {
        assert!(constant_time_eq(b"secret", b"secret"));
        assert!(!constant_time_eq(b"secret", b"secrey"));
        assert!(!constant_time_eq(b"secret", b"secret-longer"));
        assert!(constant_time_eq(b"", b""));
    }

    #[test]
    fn auth_error_maps_to_401() {
        assert_eq!(ApiError::Unauthorized.status(), StatusCode::UNAUTHORIZED);
        assert_eq!(
            ApiError::Engine(EngineError::Overloaded { waited_secs: 30 }).status(),
            StatusCode::SERVICE_UNAVAILABLE
        );
        assert_eq!(
            ApiError::Engine(EngineError::UnknownModel("v9".into())).status(),
            StatusCode::BAD_REQUEST
        );
    }
}
