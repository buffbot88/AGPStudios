//! tracing-subscriber initializer for the Omega server.

use std::env;

/// Initialise the global `tracing` subscriber.
///
/// Honors the `RUST_LOG` environment variable. Defaults to `omega=info` so the
/// server's own modules stay quiet and its own logs readable in dev.
pub fn init() {
    let filter = env::var("RUST_LOG").unwrap_or_else(|_| "omega=info".to_owned());
    let _ = tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(false)
        .try_init();
}
