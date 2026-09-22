//! Engine lifecycle and inference.
//!
//! Two properties matter here and both are deliberate departures from the
//! implementation this replaces:
//!
//! 1. Inference runs on a blocking thread while holding only a per-engine
//!    lock, so two different tiers can run concurrently and the async runtime
//!    is never blocked by an in-flight recognition.
//! 2. The number of simultaneous recognitions is capped by a semaphore, which
//!    keeps a burst from oversubscribing the shared vCPUs.

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::Instant;

use image::imageops::FilterType;
use image::{DynamicImage, Limits};
use tokio::sync::{Mutex as AsyncMutex, Semaphore};

use crate::config::{Backend, ModelTier, ServerConfig};
use crate::metrics::Metrics;

/// Whether this binary was compiled with the optional Vulkan backend.
pub const GPU_SUPPORTED: bool = cfg!(feature = "vulkan");

#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct EngineKey {
    pub model: String,
    pub backend: Backend,
}

impl EngineKey {
    pub fn new(model: &str, backend: Backend) -> Self {
        Self {
            model: model.to_string(),
            backend,
        }
    }

    pub fn label(&self) -> String {
        format!("{}/{}", self.model, self.backend.as_str())
    }
}

/// A single recognised text line.
#[derive(Clone, Debug)]
pub struct TextLine {
    pub text: String,
    pub confidence: f32,
    pub bbox: Option<BoundingBox>,
}

#[derive(Clone, Copy, Debug)]
pub struct BoundingBox {
    pub left: i32,
    pub top: i32,
    pub width: u32,
    pub height: u32,
}

/// Outcome of one recognition call.
#[derive(Clone, Debug)]
pub struct Recognition {
    /// Lines at or above `CONF_THRESHOLD`, in engine order.
    pub lines: Vec<TextLine>,
    /// Line count before thresholding, reported for observability.
    pub raw_line_count: usize,
    /// Wall-clock time spent inside inference, milliseconds.
    pub inference_ms: f64,
    /// Whether the input was downscaled before inference (`MAX_SIDE`).
    pub resized: bool,
}

impl Recognition {
    pub fn lines_dropped(&self) -> u64 {
        self.raw_line_count.saturating_sub(self.lines.len()) as u64
    }
}

#[derive(Debug)]
pub enum EngineError {
    UnknownModel(String),
    ModelFileMissing { tier: String, path: String },
    GpuUnavailable,
    LoadFailed { tier: String, reason: String },
    InferenceFailed(String),
    Overloaded { waited_secs: u64 },
}

impl std::fmt::Display for EngineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            EngineError::UnknownModel(id) => write!(
                f,
                "unknown model '{id}'; available: {}",
                ModelTier::ids().join(", ")
            ),
            EngineError::ModelFileMissing { tier, path } => {
                write!(f, "model files for '{tier}' are missing: {path} not found")
            }
            EngineError::GpuUnavailable => write!(
                f,
                "backend 'gpu' is not available in this build; use backend=cpu"
            ),
            EngineError::LoadFailed { tier, reason } => {
                write!(f, "failed to load model '{tier}': {reason}")
            }
            EngineError::InferenceFailed(reason) => write!(f, "ocr inference failed: {reason}"),
            EngineError::Overloaded { waited_secs } => write!(
                f,
                "server busy: no inference slot after waiting {waited_secs}s"
            ),
        }
    }
}

impl EngineError {
    /// Short machine-readable status label used as a metric dimension.
    pub fn status_label(&self) -> &'static str {
        match self {
            EngineError::UnknownModel(_) => "unknown_model",
            EngineError::ModelFileMissing { .. } => "model_missing",
            EngineError::GpuUnavailable => "gpu_unavailable",
            EngineError::LoadFailed { .. } => "load_failed",
            EngineError::InferenceFailed(_) => "inference_failed",
            EngineError::Overloaded { .. } => "overloaded",
        }
    }
}

/// Holds one MNN session pair per (tier, backend).
pub struct EngineManager {
    models_dir: std::path::PathBuf,
    conf_threshold: f32,
    max_side: u32,
    engines: AsyncMutex<HashMap<EngineKey, Arc<Mutex<ocr_rs::OcrEngine>>>>,
    permits: Arc<Semaphore>,
    queue_timeout: std::time::Duration,
}

impl EngineManager {
    pub fn new(config: &ServerConfig) -> Self {
        Self {
            models_dir: config.models_dir.clone(),
            conf_threshold: config.conf_threshold,
            max_side: config.max_side,
            engines: AsyncMutex::new(HashMap::new()),
            permits: Arc::new(Semaphore::new(config.max_concurrency)),
            queue_timeout: config.queue_timeout,
        }
    }

    /// True when the tier was preloaded and is still resident.
    pub async fn is_loaded(&self, key: &EngineKey) -> bool {
        self.engines.lock().await.contains_key(key)
    }

    /// Load a tier if it is not resident yet.
    ///
    /// The map lock is held across the load. That serialises the first request
    /// for a new tier for a few hundred milliseconds, which is an acceptable
    /// trade for not maintaining a second in-flight bookkeeping structure. The
    /// default tier is preloaded at startup, so the steady-state path never
    /// takes this branch.
    pub async fn ensure_loaded(
        &self,
        key: &EngineKey,
        metrics: &Metrics,
    ) -> Result<Arc<Mutex<ocr_rs::OcrEngine>>, EngineError> {
        let mut guard = self.engines.lock().await;
        if let Some(engine) = guard.get(key) {
            return Ok(engine.clone());
        }

        if key.backend == Backend::Vulkan && !GPU_SUPPORTED {
            return Err(EngineError::GpuUnavailable);
        }

        let tier = ModelTier::lookup(&key.model)
            .ok_or_else(|| EngineError::UnknownModel(key.model.clone()))?;

        for path in [
            tier.det_path(&self.models_dir),
            tier.rec_path(&self.models_dir),
            tier.keys_path(&self.models_dir),
        ] {
            if !path.exists() {
                return Err(EngineError::ModelFileMissing {
                    tier: tier.id.to_string(),
                    path: path.display().to_string(),
                });
            }
        }

        let det = tier.det_path(&self.models_dir);
        let rec = tier.rec_path(&self.models_dir);
        let keys = tier.keys_path(&self.models_dir);
        let backend = key.backend;
        let tier_id = tier.id;

        let started = Instant::now();
        let engine = tokio::task::spawn_blocking(move || build_engine(&det, &rec, &keys, backend))
            .await
            .map_err(|e| EngineError::LoadFailed {
                tier: tier_id.to_string(),
                reason: format!("loader task panicked: {e}"),
            })?
            .map_err(|reason| EngineError::LoadFailed {
                tier: tier_id.to_string(),
                reason,
            })?;

        tracing::info!(
            model = %key.model,
            backend = %key.backend.as_str(),
            elapsed_ms = started.elapsed().as_secs_f64() * 1000.0,
            "model loaded"
        );

        let engine = Arc::new(Mutex::new(engine));
        guard.insert(key.clone(), engine.clone());
        metrics.set_model_loaded(&key.model, key.backend.as_str(), true);
        Ok(engine)
    }

    /// Run recognition end to end: validate dimensions, resize if configured,
    /// acquire a slot, infer on a blocking thread.
    pub async fn recognize(
        &self,
        key: &EngineKey,
        image: DynamicImage,
        metrics: &Metrics,
    ) -> Result<Recognition, EngineError> {
        let engine = self.ensure_loaded(key, metrics).await?;

        let (image, resized) = self.apply_size_limit(image);

        let permit = tokio::time::timeout(self.queue_timeout, self.permits.clone().acquire_owned())
            .await
            .map_err(|_| {
                metrics.record_concurrency_rejected(&key.model, key.backend.as_str());
                EngineError::Overloaded {
                    waited_secs: self.queue_timeout.as_secs(),
                }
            })?
            .map_err(|_| EngineError::InferenceFailed("inference semaphore closed".to_string()))?;

        let threshold = self.conf_threshold;
        let started = Instant::now();

        let outcome = tokio::task::spawn_blocking(move || {
            let _permit = permit;
            let guard = engine
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            match guard.recognize(&image) {
                Ok(raw) => Ok(raw
                    .into_iter()
                    .map(|item| TextLine {
                        text: item.text,
                        confidence: item.confidence,
                        bbox: Some(BoundingBox {
                            left: item.bbox.rect.left(),
                            top: item.bbox.rect.top(),
                            width: item.bbox.rect.width(),
                            height: item.bbox.rect.height(),
                        }),
                    })
                    .collect::<Vec<_>>()),
                Err(err) => Err(format!("{err}")),
            }
        })
        .await
        .map_err(|e| EngineError::InferenceFailed(format!("worker panicked: {e}")))?;

        let inference_ms = started.elapsed().as_secs_f64() * 1000.0;

        let raw = outcome.map_err(EngineError::InferenceFailed)?;
        let raw_line_count = raw.len();
        let lines: Vec<TextLine> = raw
            .into_iter()
            .filter(|line| line.confidence > threshold)
            .collect();

        Ok(Recognition {
            lines,
            raw_line_count,
            inference_ms,
            resized,
        })
    }

    /// Downscale when `MAX_SIDE` is set; no-op otherwise.
    fn apply_size_limit(&self, image: DynamicImage) -> (DynamicImage, bool) {
        if self.max_side == 0 {
            return (image, false);
        }
        let (width, height) = (image.width(), image.height());
        let longest = width.max(height);
        if longest <= self.max_side {
            return (image, false);
        }
        let scale = self.max_side as f64 / longest as f64;
        let target_w = ((width as f64 * scale).round() as u32).max(1);
        let target_h = ((height as f64 * scale).round() as u32).max(1);
        tracing::debug!(width, height, target_w, target_h, "downscaling input");
        (
            image.resize_exact(target_w, target_h, FilterType::Triangle),
            true,
        )
    }
}

fn build_engine(
    det: &Path,
    rec: &Path,
    keys: &Path,
    backend: Backend,
) -> Result<ocr_rs::OcrEngine, String> {
    let config = match backend {
        Backend::Cpu => None,
        #[cfg(feature = "vulkan")]
        Backend::Vulkan => {
            Some(ocr_rs::OcrEngineConfig::new().with_backend(ocr_rs::Backend::Vulkan))
        }
        #[cfg(not(feature = "vulkan"))]
        Backend::Vulkan => return Err("vulkan support was not compiled in".to_string()),
    };

    ocr_rs::OcrEngine::new(
        det.display().to_string(),
        rec.display().to_string(),
        keys.display().to_string(),
        config,
    )
    .map_err(|e| format!("{e}"))
}

/// Decode an upload with explicit limits so a malformed image cannot allocate
/// unbounded memory.
pub fn decode_image(bytes: &[u8], max_side_hint: u32) -> Result<DynamicImage, String> {
    use std::io::Cursor;

    let reader = image::ImageReader::new(Cursor::new(bytes))
        .with_guessed_format()
        .map_err(|e| format!("Image decode failed: {e}"))?;
    let format = reader
        .format()
        .map(|f| format!("{f:?}"))
        .unwrap_or_else(|| "unknown".to_string());

    let mut limits = Limits::default();
    limits.max_image_width = Some(20_000);
    limits.max_image_height = Some(20_000);
    limits.max_alloc = Some(512 * 1024 * 1024);

    let mut reader = reader;
    reader.limits(limits);
    let image = reader
        .decode()
        .map_err(|e| format!("Image decode failed ({format}): {e}"))?;

    if image.width() == 0 || image.height() == 0 {
        return Err("Image decode failed: zero-sized image".to_string());
    }
    if max_side_hint > 0 {
        tracing::trace!(width = image.width(), height = image.height(), "decoded");
    }
    Ok(image)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn size_limit_is_opt_in() {
        let mut config = ServerConfig {
            models_dir: "/tmp".into(),
            listen_addr: "0.0.0.0:0".into(),
            preload: vec![],
            default_model: "v6small".into(),
            conf_threshold: 0.3,
            max_concurrency: 1,
            max_side: 0,
            max_image_bytes: 1024,
            queue_timeout: std::time::Duration::from_secs(1),
            auth_token: None,
        };
        let manager = EngineManager::new(&config);
        let image = DynamicImage::new_rgb8(4000, 100);
        let (out, resized) = manager.apply_size_limit(image);
        assert!(!resized);
        assert_eq!(out.width(), 4000);

        config.max_side = 1000;
        let manager = EngineManager::new(&config);
        let (out, resized) = manager.apply_size_limit(DynamicImage::new_rgb8(4000, 100));
        assert!(resized);
        assert_eq!(out.width(), 1000);
        assert_eq!(out.height(), 25);
    }

    #[test]
    fn decode_rejects_garbage() {
        assert!(decode_image(b"not an image", 0).is_err());
    }

    #[test]
    fn error_labels_are_stable() {
        assert_eq!(
            EngineError::GpuUnavailable.status_label(),
            "gpu_unavailable"
        );
        assert_eq!(
            EngineError::Overloaded { waited_secs: 1 }.status_label(),
            "overloaded"
        );
    }
}
