use std::{env, net::SocketAddr};

use ashat_hub::{app, state_from_env};
use tracing::info;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            env::var("RUST_LOG").unwrap_or_else(|_| "ashat_hub=info,tower_http=info".to_owned()),
        )
        .init();

    let bind: SocketAddr = env::var("ASHAT_HUB_BIND")
        .unwrap_or_else(|_| "127.0.0.1:3100".to_owned())
        .parse()?;
    let state = state_from_env()?;
    ashat_hub::start_galileo_worker(state.clone()).await?;
    let listener = tokio::net::TcpListener::bind(bind).await?;

    info!(%bind, "AshatHub Rust gateway listening");
    axum::serve(
        listener,
        app(state).into_make_service_with_connect_info::<SocketAddr>(),
    )
    .await?;
    Ok(())
}
