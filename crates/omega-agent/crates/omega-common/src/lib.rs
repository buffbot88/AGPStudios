//! Omega shared foundation.
//!
//! Types, configuration, model discovery, metrics, logging, and coding-agent
//! workspace management — the layer both `omega-core` and `omega-server` build
//! on. Host-dependent resolution (paths, env overlays) lives here so the upper
//! crates stay universal-source.

pub mod config;
pub mod log;
pub mod metrics;
pub mod models;
pub mod types;
pub mod workspace;
