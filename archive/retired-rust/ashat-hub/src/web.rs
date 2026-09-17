use std::{
    fs,
    path::{Component, Path, PathBuf},
};

use axum::{
    extract::{Path as AxumPath, State},
    http::{header, HeaderMap, StatusCode, Uri},
    response::{IntoResponse, Response},
    routing::get,
    Router,
};

use crate::{response::error_response, AppState};

const MAX_ASSET_BYTES: u64 = 10 * 1024 * 1024;

pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/", get(index))
        .route("/host/*path", get(host))
        .route("/x/*path", get(backup_host))
        .fallback(fallback)
}

async fn index(State(state): State<AppState>, headers: HeaderMap) -> Response {
    if let Some(response) = serve_custom_host(&state, &headers, "index.html").await {
        return response;
    }
    serve_spa(&state.web_root)
}

async fn host(State(state): State<AppState>, AxumPath(path): AxumPath<String>) -> Response {
    let segments: Vec<&str> = path
        .split('/')
        .filter(|segment| !segment.is_empty())
        .collect();
    if segments.len() < 2
        || segments.iter().any(|segment| {
            segment.starts_with('.')
                || !segment
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
        })
    {
        return error_response(StatusCode::NOT_FOUND, "not_found");
    }
    let relative = segments.join("/");
    let Some(path) = safe_join(&state.deploy_root, &relative) else {
        return error_response(StatusCode::NOT_FOUND, "not_found");
    };
    let path = if path.is_dir() {
        path.join("index.html")
    } else {
        path
    };
    serve_file(&path).unwrap_or_else(|| error_response(StatusCode::NOT_FOUND, "not_found"))
}

async fn backup_host(State(state): State<AppState>, AxumPath(path): AxumPath<String>) -> Response {
    let mut segments = path.split('/').filter(|segment| !segment.is_empty());
    let Some(project_id) = segments.next() else {
        return error_response(StatusCode::NOT_FOUND, "not_found");
    };
    if !safe_segment(project_id) {
        return error_response(StatusCode::NOT_FOUND, "not_found");
    }
    let relative = segments.collect::<Vec<_>>().join("/");
    if relative.split('/').any(|segment| {
        segment.starts_with('.')
            || !segment
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
    }) {
        return error_response(StatusCode::NOT_FOUND, "not_found");
    }
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "database_not_configured");
    };
    let users = match sqlx::query_scalar::<_, String>(
        "SELECT user_id FROM galileo_deployments WHERE project_id=? AND status='deployed' LIMIT 2",
    )
    .bind(project_id)
    .fetch_all(pool)
    .await
    {
        Ok(users) => users,
        Err(error) => {
            tracing::warn!(?error, "backup deployment lookup failed");
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "deployment_unavailable");
        }
    };
    let Some(user_id) = users.first().filter(|_| users.len() == 1) else {
        return error_response(StatusCode::NOT_FOUND, "deployment_not_found");
    };
    let relative = if relative.is_empty() {
        "index.html".to_owned()
    } else {
        relative
    };
    let Some(path) = safe_join(
        &state.deploy_root,
        &format!("{}/{}", user_id, format!("{}/{}", project_id, relative)),
    ) else {
        return error_response(StatusCode::NOT_FOUND, "not_found");
    };
    serve_file(&path).unwrap_or_else(|| error_response(StatusCode::NOT_FOUND, "not_found"))
}

async fn fallback(State(state): State<AppState>, headers: HeaderMap, uri: Uri) -> Response {
    let path = uri.path();
    if let Some(response) = serve_custom_host(&state, &headers, path.trim_start_matches('/')).await {
        return response;
    }
    if path.starts_with("/api/") || path == "/api" {
        return error_response(StatusCode::NOT_FOUND, "not_found");
    }
    if path.starts_with("/host/") || path.starts_with("/x/") {
        return error_response(StatusCode::NOT_FOUND, "not_found");
    }
    if path.split('/').any(|segment| segment == "..") {
        return error_response(StatusCode::FORBIDDEN, "forbidden");
    }
    let relative = path.trim_start_matches('/');
    if !relative.is_empty() && !relative.ends_with('/') {
        if let Some(response) = serve_asset(&state.web_root, relative) {
            return response;
        }
        if Path::new(relative).extension().is_some() {
            return error_response(StatusCode::NOT_FOUND, "not_found");
        }
    }
    serve_spa(&state.web_root)
}

async fn serve_custom_host(
    state: &AppState,
    headers: &HeaderMap,
    relative: &str,
) -> Option<Response> {
    let domain = state.deploy_domain.as_deref()?;
    let host = headers.get("host")?.to_str().ok()?.split(':').next()?;
    let suffix = format!(".{domain}");
    let subdomain = host.strip_suffix(&suffix)?;
    if !safe_segment(subdomain) {
        return None;
    }
    let pool = state.db.as_ref()?;
    let row = sqlx::query_as::<_, (String, String)>(
        "SELECT user_id, project_id FROM galileo_deployments WHERE subdomain=? AND status='deployed'",
    )
    .bind(subdomain)
    .fetch_optional(pool)
    .await
    .ok()??;
    let relative = if relative.is_empty() { "index.html" } else { relative };
    if relative.split('/').any(|segment| {
        segment.is_empty()
            || segment.starts_with('.')
            || !segment
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
    }) {
        return None;
    }
    let path = safe_join(
        &state.deploy_root,
        &format!("{}/{}/{}", row.0, row.1, relative),
    )?;
    serve_file(&path)
}

fn serve_spa(root: &Path) -> Response {
    serve_asset(root, "index.html").unwrap_or_else(|| {
        error_response(StatusCode::SERVICE_UNAVAILABLE, "web_bundle_unavailable")
    })
}

fn serve_asset(root: &Path, relative: &str) -> Option<Response> {
    let path = safe_join(root, relative)?;
    let metadata = fs::metadata(&path).ok()?;
    if !metadata.is_file() || metadata.len() > MAX_ASSET_BYTES {
        return None;
    }
    serve_file(&path)
}

fn serve_file(path: &Path) -> Option<Response> {
    let metadata = fs::metadata(path).ok()?;
    if !metadata.is_file() || metadata.len() > MAX_ASSET_BYTES {
        return None;
    }
    let content = fs::read(path).ok()?;
    let content_type = content_type(path);
    Some(
        (
            StatusCode::OK,
            [(header::CONTENT_TYPE, content_type)],
            content,
        )
            .into_response(),
    )
}

fn safe_join(root: &Path, relative: &str) -> Option<PathBuf> {
    if relative.is_empty()
        || relative.split('/').any(|segment| {
            segment.is_empty() || segment == ".." || segment.starts_with('.') || segment.len() > 255
        })
    {
        return None;
    }
    let relative_path = Path::new(relative);
    if relative_path.is_absolute()
        || relative_path.components().any(|component| {
            matches!(
                component,
                Component::ParentDir | Component::RootDir | Component::Prefix(_)
            )
        })
    {
        return None;
    }
    let canonical_root = fs::canonicalize(root).ok()?;
    let candidate = canonical_root.join(relative_path);
    let canonical_candidate = fs::canonicalize(candidate).ok()?;
    canonical_candidate
        .starts_with(&canonical_root)
        .then_some(canonical_candidate)
}

fn safe_segment(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 120
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

fn content_type(path: &Path) -> &'static str {
    match path
        .extension()
        .and_then(|extension| extension.to_str())
        .unwrap_or_default()
    {
        "html" => "text/html; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "js" => "text/javascript; charset=utf-8",
        "json" => "application/json; charset=utf-8",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "ico" => "image/x-icon",
        "webp" => "image/webp",
        "woff" => "font/woff",
        "woff2" => "font/woff2",
        "map" => "application/json; charset=utf-8",
        _ => "application/octet-stream",
    }
}

#[cfg(test)]
mod tests {
    use super::content_type;
    use std::path::Path;

    #[test]
    fn content_types_cover_vite_assets() {
        assert_eq!(
            content_type(Path::new("index.html")),
            "text/html; charset=utf-8"
        );
        assert_eq!(
            content_type(Path::new("app.js")),
            "text/javascript; charset=utf-8"
        );
        assert_eq!(
            content_type(Path::new("app.css")),
            "text/css; charset=utf-8"
        );
    }
}
