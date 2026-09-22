//! Runtime configuration and the model catalogue.
//!
//! Everything that can differ between environments (models directory, listen
//! address, preloaded tiers, thresholds, concurrency) is resolved from
//! environment variables so the same image can be promoted unchanged.

use std::path::PathBuf;
use std::time::Duration;

/// Tier used when a request does not name one. Matches the previous NAS
/// deployment so existing clients keep getting identical output.
pub const DEFAULT_MODEL: &str = "v6small";

/// Default minimum recognition confidence. Kept at 0.3 to stay bit-compatible
/// with the service this replaces.
pub const DEFAULT_CONF_THRESHOLD: f32 = 0.3;

/// Default cap on simultaneous inference calls. The target hosts have 4 shared
/// vCPUs; more parallelism than this only adds context switching.
pub const DEFAULT_MAX_CONCURRENCY: usize = 2;

/// Default upper bound for the longest image side. `0` disables resizing.
///
/// Disabled by default on purpose: downscaling can erase the small glyphs these
/// models are good at, and the reference implementation never resized either.
/// Enable it per deployment (`MAX_SIDE=1920`) when huge screenshots dominate.
pub const DEFAULT_MAX_SIDE: u32 = 0;

/// Largest accepted request body. Screenshots are far below this; the cap only
/// exists to stop a malformed or hostile upload from exhausting memory.
pub const DEFAULT_MAX_IMAGE_BYTES: usize = 20 * 1024 * 1024;

/// Longest a request may wait for a free inference slot before being rejected
/// with 503. Queuing is preferred over immediate rejection for this workload,
/// but unbounded queuing turns an overload into a latency cliff.
pub const DEFAULT_QUEUE_TIMEOUT_SECS: u64 = 30;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Backend {
    Cpu,
    Vulkan,
}

impl Backend {
    pub fn as_str(self) -> &'static str {
        match self {
            Backend::Cpu => "cpu",
            Backend::Vulkan => "gpu",
        }
    }

    pub fn parse(value: &str) -> Result<Self, String> {
        match value.trim().to_ascii_lowercase().as_str() {
            "cpu" => Ok(Backend::Cpu),
            // `gpu` keeps the historical alias so existing clients keep working.
            "gpu" | "vulkan" => Ok(Backend::Vulkan),
            other => Err(format!(
                "unknown backend '{other}': this build supports cpu only"
            )),
        }
    }
}

/// A selectable detection+recognition pair.
#[derive(Clone, Copy, Debug)]
pub struct ModelTier {
    /// Public identifier used in requests (`model=` query parameter).
    pub id: &'static str,
    /// Detection model file name inside `MODELS_DIR`.
    pub det: &'static str,
    /// Recognition model file name inside `MODELS_DIR`.
    pub rec: &'static str,
    /// Character dictionary file name inside `MODELS_DIR`.
    pub keys: &'static str,
    /// Human-readable summary used by `/models`.
    pub label: &'static str,
}

/// Every tier shipped inside the image. The model files are small enough
/// (3 MB - 25 MB) that bundling all of them removes a rebuild from the loop
/// when switching tiers, while `PRELOAD_MODELS` still controls what is held in
/// memory.
///
/// PP-OCRv6 medium is deliberately absent: the published `PP-OCRv6_medium_*.mnn`
/// files fail to load in MNN with `Engine creation failed`, and the same
/// failure reproduces in the upstream tooling these files came from. Shipping a
/// tier that cannot load would only produce a 500 at request time.
/// See `docs/ocr-model-selection.md` for the measured comparison.
pub const MODEL_TIERS: &[ModelTier] = &[
    ModelTier {
        id: "v6tiny",
        det: "PP-OCRv6_tiny_det.mnn",
        rec: "PP-OCRv6_tiny_rec.mnn",
        keys: "ppocr_keys_v6_tiny.txt",
        label: "PP-OCRv6 tiny - 1.2 ms/img and ~33 MB RSS, but misses about 40% of the lines small catches",
    },
    ModelTier {
        id: "v6small",
        det: "PP-OCRv6_small_det.mnn",
        rec: "PP-OCRv6_small_rec.mnn",
        keys: "ppocr_keys_v6_small.txt",
        label: "PP-OCRv6 small - default; best accuracy per millisecond in the shipped corpus",
    },
    ModelTier {
        id: "v5",
        det: "PP-OCRv5_mobile_det.mnn",
        rec: "PP-OCRv5_mobile_rec.mnn",
        keys: "ppocr_keys_v5.txt",
        label: "PP-OCRv5 mobile - previous generation, kept for output compatibility",
    },
];

impl ModelTier {
    pub fn lookup(id: &str) -> Option<&'static ModelTier> {
        MODEL_TIERS.iter().find(|tier| tier.id == id)
    }

    pub fn det_path(&self, models_dir: &std::path::Path) -> PathBuf {
        models_dir.join(self.det)
    }

    pub fn rec_path(&self, models_dir: &std::path::Path) -> PathBuf {
        models_dir.join(self.rec)
    }

    pub fn keys_path(&self, models_dir: &std::path::Path) -> PathBuf {
        models_dir.join(self.keys)
    }

    pub fn ids() -> Vec<&'static str> {
        MODEL_TIERS.iter().map(|tier| tier.id).collect()
    }
}

#[derive(Clone, Debug)]
pub struct ServerConfig {
    pub models_dir: PathBuf,
    pub listen_addr: String,
    /// Tiers loaded during startup. Everything else is loaded on first use.
    pub preload: Vec<String>,
    pub default_model: String,
    pub conf_threshold: f32,
    pub max_concurrency: usize,
    pub max_side: u32,
    pub max_image_bytes: usize,
    pub queue_timeout: Duration,
    /// When set, `/ocr`, `/ocr/batch` and `/metrics` require this token via
    /// `X-Auth-Token` or `Authorization: Bearer`. The service is reachable from
    /// the internet in the reference deployment, so the pipeline injects one.
    pub auth_token: Option<String>,
}

impl ServerConfig {
    pub fn from_env() -> Result<Self, String> {
        let models_dir = env_or("MODELS_DIR", "/app/models").into();
        let listen_addr = env_or("LISTEN_ADDR", "0.0.0.0:8080");
        let default_model = env_or("DEFAULT_MODEL", DEFAULT_MODEL);

        if ModelTier::lookup(&default_model).is_none() {
            return Err(format!(
                "DEFAULT_MODEL='{default_model}' is not a known tier; available: {}",
                ModelTier::ids().join(", ")
            ));
        }

        // PRELOAD_MODELS unset -> preload the default tier only. Set to an empty
        // string to preload nothing (useful when memory is the binding limit).
        let preload = match std::env::var("PRELOAD_MODELS") {
            Ok(raw) => raw
                .split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(str::to_string)
                .collect::<Vec<_>>(),
            Err(_) => vec![default_model.clone()],
        };
        for id in &preload {
            if ModelTier::lookup(id).is_none() {
                return Err(format!(
                    "PRELOAD_MODELS contains unknown tier '{id}'; available: {}",
                    ModelTier::ids().join(", ")
                ));
            }
        }

        let conf_threshold = match std::env::var("CONF_THRESHOLD") {
            Ok(raw) => raw
                .trim()
                .parse::<f32>()
                .map_err(|e| format!("CONF_THRESHOLD='{raw}' is not a number: {e}"))?,
            Err(_) => DEFAULT_CONF_THRESHOLD,
        };
        if !(0.0..=1.0).contains(&conf_threshold) {
            return Err(format!(
                "CONF_THRESHOLD={conf_threshold} is outside the valid range 0.0..=1.0"
            ));
        }

        let max_concurrency = match std::env::var("MAX_CONCURRENCY") {
            Ok(raw) => raw
                .trim()
                .parse::<usize>()
                .map_err(|e| format!("MAX_CONCURRENCY='{raw}' is not a number: {e}"))?,
            Err(_) => DEFAULT_MAX_CONCURRENCY,
        };
        if max_concurrency == 0 {
            return Err("MAX_CONCURRENCY must be >= 1".to_string());
        }

        let max_side = match std::env::var("MAX_SIDE") {
            Ok(raw) => raw
                .trim()
                .parse::<u32>()
                .map_err(|e| format!("MAX_SIDE='{raw}' is not a number: {e}"))?,
            Err(_) => DEFAULT_MAX_SIDE,
        };

        let max_image_bytes = match std::env::var("MAX_IMAGE_BYTES") {
            Ok(raw) => raw
                .trim()
                .parse::<usize>()
                .map_err(|e| format!("MAX_IMAGE_BYTES='{raw}' is not a number: {e}"))?,
            Err(_) => DEFAULT_MAX_IMAGE_BYTES,
        };
        if max_image_bytes == 0 {
            return Err("MAX_IMAGE_BYTES must be >= 1".to_string());
        }

        let queue_timeout_secs = match std::env::var("QUEUE_TIMEOUT_SECS") {
            Ok(raw) => raw
                .trim()
                .parse::<u64>()
                .map_err(|e| format!("QUEUE_TIMEOUT_SECS='{raw}' is not a number: {e}"))?,
            Err(_) => DEFAULT_QUEUE_TIMEOUT_SECS,
        };

        Ok(Self {
            models_dir,
            listen_addr,
            preload,
            default_model,
            conf_threshold,
            max_concurrency,
            max_side,
            max_image_bytes,
            queue_timeout: Duration::from_secs(queue_timeout_secs),
            auth_token: std::env::var("AUTH_TOKEN")
                .ok()
                .map(|v| v.trim().to_string())
                .filter(|v| !v.is_empty()),
        })
    }
}

fn env_or(key: &str, fallback: &str) -> String {
    std::env::var(key)
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| fallback.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tier_lookup_is_stable() {
        assert_eq!(ModelTier::lookup("v6small").unwrap().id, "v6small");
        assert!(ModelTier::lookup("v4").is_none());
        assert_eq!(ModelTier::ids().len(), MODEL_TIERS.len());
    }

    #[test]
    fn backend_parsing_keeps_legacy_alias() {
        assert_eq!(Backend::parse("cpu").unwrap(), Backend::Cpu);
        assert_eq!(Backend::parse("GPU").unwrap(), Backend::Vulkan);
        assert!(Backend::parse("tpu").is_err());
    }
}
