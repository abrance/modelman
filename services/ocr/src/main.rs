//! Service entry point.
//!
//! Also implements `--healthcheck`, a self-contained readiness probe. Using
//! the binary for the container health check keeps `curl` out of the runtime
//! image and guarantees the probe speaks the same protocol version as the
//! server.

use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

use ocr_service::AppState;
use ocr_service::{build_router, ServerConfig};
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.iter().any(|arg| arg == "--healthcheck") {
        std::process::exit(run_healthcheck());
    }
    if args.iter().any(|arg| arg == "--version") {
        println!(
            "{} {} ({}, built {})",
            ocr_service::api::NAME,
            ocr_service::api::VERSION,
            ocr_service::api::GIT_COMMIT,
            ocr_service::api::BUILD_TIME
        );
        return;
    }

    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| "ocr_service=info".into()),
        )
        .init();

    let config = match ServerConfig::from_env() {
        Ok(config) => config,
        Err(reason) => {
            eprintln!("configuration error: {reason}");
            std::process::exit(2);
        }
    };

    tracing::info!(
        version = ocr_service::api::VERSION,
        commit = ocr_service::api::GIT_COMMIT,
        built = ocr_service::api::BUILD_TIME,
        "starting"
    );
    tracing::info!(
        listen = %config.listen_addr,
        models_dir = %config.models_dir.display(),
        default_model = %config.default_model,
        preload = ?config.preload,
        conf_threshold = config.conf_threshold,
        max_concurrency = config.max_concurrency,
        max_side = config.max_side,
        gpu_supported = ocr_service::engine::GPU_SUPPORTED,
        auth_required = config.auth_token.is_some(),
        "configuration"
    );

    let listen_addr = config.listen_addr.clone();
    let state = AppState::new(config).await;
    let app = build_router(state);

    let listener = match tokio::net::TcpListener::bind(&listen_addr).await {
        Ok(listener) => listener,
        Err(err) => {
            eprintln!("cannot bind {listen_addr}: {err}");
            std::process::exit(2);
        }
    };

    tracing::info!("ready");
    if let Err(err) = axum::serve(
        listener,
        app.into_make_service_with_connect_info::<std::net::SocketAddr>(),
    )
    .with_graceful_shutdown(shutdown_signal())
    .await
    {
        eprintln!("server error: {err}");
        std::process::exit(1);
    }
    tracing::info!("stopped");
}

async fn shutdown_signal() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };

    #[cfg(unix)]
    let terminate = async {
        match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            Ok(mut signal) => {
                signal.recv().await;
            }
            Err(err) => {
                tracing::warn!(error = %err, "cannot install SIGTERM handler");
                std::future::pending::<()>().await;
            }
        }
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }

    tracing::info!("shutdown signal received");
}

/// Minimal HTTP/1.1 GET against the local listener. Returns a process exit code.
fn run_healthcheck() -> i32 {
    let listen = std::env::var("LISTEN_ADDR").unwrap_or_else(|_| "0.0.0.0:8080".to_string());
    let port = listen.rsplit(':').next().unwrap_or("8080");
    let target = format!("127.0.0.1:{port}");

    let addr = match target
        .to_socket_addrs()
        .map(|mut it| it.next())
        .ok()
        .flatten()
    {
        Some(addr) => addr,
        None => {
            eprintln!("healthcheck: cannot resolve {target}");
            return 1;
        }
    };

    let mut stream = match TcpStream::connect_timeout(&addr, Duration::from_secs(5)) {
        Ok(stream) => stream,
        Err(err) => {
            eprintln!("healthcheck: connect {target} failed: {err}");
            return 1;
        }
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(5)));

    let request = "GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n";
    if let Err(err) = std::io::Write::write_all(&mut stream, request.as_bytes()) {
        eprintln!("healthcheck: write failed: {err}");
        return 1;
    }

    let mut response = String::new();
    if let Err(err) = std::io::Read::read_to_string(&mut stream, &mut response) {
        eprintln!("healthcheck: read failed: {err}");
        return 1;
    }

    match response.lines().next() {
        Some(status) if status.contains(" 200") => 0,
        other => {
            eprintln!("healthcheck: unexpected status line {other:?}");
            1
        }
    }
}
