//! Injects build provenance into the binary so `/version` can report exactly
//! which commit produced a running container.

use std::process::Command;

fn main() {
    let commit = std::env::var("GIT_COMMIT")
        .ok()
        .filter(|v| !v.is_empty())
        .or_else(git_rev_parse)
        .unwrap_or_else(|| "unknown".to_string());

    println!("cargo:rustc-env=GIT_COMMIT={commit}");
    println!("cargo:rustc-env=BUILD_TIME={}", build_time());

    println!("cargo:rerun-if-env-changed=GIT_COMMIT");
    println!("cargo:rerun-if-env-changed=SOURCE_DATE_EPOCH");
}

fn git_rev_parse() -> Option<String> {
    let out = Command::new("git")
        .args(["rev-parse", "--short=12", "HEAD"])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let value = String::from_utf8(out.stdout).ok()?.trim().to_string();
    (!value.is_empty()).then_some(value)
}

fn build_time() -> String {
    // SOURCE_DATE_EPOCH is honoured for reproducible builds; otherwise fall
    // back to the wall clock via `date`, avoiding an extra crate dependency.
    if let Ok(epoch) = std::env::var("SOURCE_DATE_EPOCH") {
        if !epoch.is_empty() {
            return format!("epoch:{epoch}");
        }
    }
    Command::new("date")
        .args(["-u", "+%Y-%m-%dT%H:%M:%SZ"])
        .output()
        .ok()
        .and_then(|out| String::from_utf8(out.stdout).ok())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "unknown".to_string())
}
