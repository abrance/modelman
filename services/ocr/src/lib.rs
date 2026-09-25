//! modelman OCR service: PP-OCR detection + recognition behind HTTP.

pub mod api;
pub mod config;
pub mod engine;
pub mod metrics;
pub mod ui;

pub use api::{build_router, AppState};
pub use config::{Backend, ServerConfig};
pub use engine::EngineKey;
