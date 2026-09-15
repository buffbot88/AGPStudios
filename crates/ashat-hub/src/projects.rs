use std::{
    fs,
    io::{Cursor, Read, Write},
    path::{Component, Path, PathBuf},
};

use axum::{
    extract::{Path as AxumPath, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{delete, get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use uuid::Uuid;
use zip::{write::SimpleFileOptions, ZipArchive, ZipWriter};

use crate::{auth, response::error_response, AppState};

const MAX_PROJECT_BYTES: u64 = 50 * 1024 * 1024;

#[derive(Debug, Deserialize)]
struct CreateProjectRequest {
    name: String,
}

#[derive(Debug, Serialize)]
pub(crate) struct Project {
    pub(crate) id: String,
    pub(crate) name: String,
    pub(crate) description: String,
    pub(crate) created_at: String,
    pub(crate) file_count: usize,
}

#[derive(Debug, Serialize)]
struct ProjectList {
    projects: Vec<Project>,
}

#[derive(Debug, Serialize)]
struct CreateProjectResponse {
    ok: bool,
    project_id: String,
    name: String,
}

#[derive(Debug, Serialize)]
struct FileEntry {
    path: String,
    size: u64,
}

#[derive(Debug, Serialize)]
struct FileList {
    files: Vec<FileEntry>,
}

#[derive(Debug, Deserialize)]
struct FileWrite {
    content: String,
}

#[derive(Debug, Deserialize)]
struct SnapshotRequest {
    files: std::collections::HashMap<String, String>,
}

#[derive(Debug, Serialize)]
struct FileContent {
    path: String,
    content: String,
}

#[derive(Debug, Deserialize)]
struct FileMove {
    path: String,
    new_path: String,
}

#[derive(Debug, Deserialize)]
struct FilePath {
    path: String,
}

pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/api/galileo/projects", get(list).post(create))
        .route("/api/galileo/projects/:project_id/files", get(files))
        .route("/api/galileo/projects/:project_id/files/snapshot", post(snapshot))
        .route("/api/galileo/projects/:project_id/checkpoints", post(create_checkpoint).get(list_checkpoints))
        .route("/api/galileo/projects/:project_id/checkpoints/:checkpoint_id", get(read_checkpoint))
        .route(
            "/api/galileo/projects/:project_id/files/export",
            get(export_zip),
        )
        .route(
            "/api/galileo/projects/:project_id/files/import",
            post(import_zip),
        )
        .route(
            "/api/galileo/projects/:project_id/files/rename",
            post(rename_file),
        )
        .route(
            "/api/galileo/projects/:project_id/files/duplicate",
            post(duplicate_file),
        )
        .route(
            "/api/galileo/projects/:project_id/files/tree",
            delete(delete_tree),
        )
        .route(
            "/api/galileo/projects/:project_id/files/folder",
            post(create_folder),
        )
        .route(
            "/api/galileo/projects/:project_id/files/*path",
            get(read_file).put(write_file).delete(delete_file),
        )
}

async fn list(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
) -> Response {
    let root = state.projects_root.join(&user.id);
    if let Err(error) = fs::create_dir_all(&root) {
        tracing::error!(?error, "unable to create project directory");
        return error_response(
            StatusCode::INTERNAL_SERVER_ERROR,
            "project_storage_unavailable",
        );
    }

    match load_projects(&root) {
        Ok(projects) => Json(ProjectList { projects }).into_response(),
        Err(error) => {
            tracing::error!(?error, "unable to list Galileo projects");
            error_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                "project_storage_unavailable",
            )
        }
    }
}

async fn create(
    State(state): State<AppState>,
    auth::AuthenticatedUser(user): auth::AuthenticatedUser,
    Json(input): Json<CreateProjectRequest>,
) -> Response {
    let name = input.name.trim();
    if name.is_empty() || name.chars().count() > 120 {
        return error_response(StatusCode::BAD_REQUEST, "invalid_project_name");
    }
    let Some(project_id) = unique_slug(&state.projects_root.join(&user.id), name) else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_project_name");
    };
    let root = state.projects_root.join(&user.id).join(&project_id);

    if let Err(error) = fs::create_dir_all(&root) {
        tracing::error!(?error, "unable to create Galileo project");
        return error_response(
            StatusCode::INTERNAL_SERVER_ERROR,
            "project_storage_unavailable",
        );
    }

    let metadata = serde_json::json!({
        "name": name,
        "description": "",
        "created_at": chrono_like_now(),
    });
    if let Err(error) = fs::write(
        root.join(".meta.json"),
        serde_json::to_vec_pretty(&metadata).unwrap_or_default(),
    ) {
        let _ = fs::remove_dir_all(&root);
        tracing::error!(?error, "unable to write Galileo project metadata");
        return error_response(
            StatusCode::INTERNAL_SERVER_ERROR,
            "project_storage_unavailable",
        );
    }

    tracing::info!(
        event = "galileo.project.created",
        user_id = %user.id,
        project_id = %project_id
    );
    (
        StatusCode::CREATED,
        Json(CreateProjectResponse {
            ok: true,
            project_id,
            name: name.to_owned(),
        }),
    )
        .into_response()
}

async fn files(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(project_id): AxumPath<String>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    let Some(user) = authenticated(pool, &state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    let Some(root) = project_root(&state, &user.id, &project_id) else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_project_id");
    };
    match collect_files(&root, &root) {
        Ok(files) => Json(FileList { files }).into_response(),
        Err(error) => {
            tracing::error!(?error, "project file listing failed");
            error_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                "project_storage_unavailable",
            )
        }
    }
}

async fn snapshot(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(project_id): AxumPath<String>,
    Json(input): Json<SnapshotRequest>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    let Some(user) = authenticated(pool, &state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    let Some(root) = project_root(&state, &user.id, &project_id) else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_project_id");
    };
    let total = input.files.iter().try_fold(0u64, |total, (path, content)| {
        let target = file_path(&state, &user.id, &project_id, path)
            .ok_or(StatusCode::BAD_REQUEST)?;
        if content.len() > 2_000_000 {
            return Err(StatusCode::PAYLOAD_TOO_LARGE);
        }
        let next = total.saturating_add(content.len() as u64);
        if next > MAX_PROJECT_BYTES { Err(StatusCode::PAYLOAD_TOO_LARGE) } else { let _ = target; Ok(next) }
    });
    if total.is_err() {
        return error_response(total.unwrap_err(), "invalid_project_snapshot");
    }
    let file_count = input.files.len();
    if let Err(error) = fs::create_dir_all(&root) {
        tracing::error!(?error, "project snapshot directory creation failed");
        return error_response(StatusCode::INTERNAL_SERVER_ERROR, "project_storage_unavailable");
    }
    for (path, content) in input.files {
        let Some(target) = file_path(&state, &user.id, &project_id, &path) else {
            return error_response(StatusCode::BAD_REQUEST, "invalid_path");
        };
        if let Some(parent) = target.parent() {
            if fs::create_dir_all(parent).is_err() {
                return error_response(StatusCode::INTERNAL_SERVER_ERROR, "project_storage_unavailable");
            }
        }
        if fs::write(target, content).is_err() {
            return error_response(StatusCode::INTERNAL_SERVER_ERROR, "project_storage_unavailable");
        }
    }
    Json(serde_json::json!({"ok": true, "files": file_count})).into_response()
}

async fn create_checkpoint(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(project_id): AxumPath<String>,
    Json(input): Json<SnapshotRequest>,
) -> Response {
    let Some(pool) = state.db.as_ref() else { return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable"); };
    let Some(user) = authenticated(pool, &state, &headers).await else { return error_response(StatusCode::UNAUTHORIZED, "unauthenticated"); };
    let Some(root) = project_root(&state, &user.id, &project_id) else { return error_response(StatusCode::BAD_REQUEST, "invalid_project_id"); };
    let total: usize = input.files.values().map(String::len).sum();
    if total > MAX_PROJECT_BYTES as usize { return error_response(StatusCode::PAYLOAD_TOO_LARGE, "project_quota_exceeded"); }
    let id = format!("cp_{}", Uuid::new_v4().simple());
    let checkpoints = root.join(".checkpoints");
    let stage = checkpoints.join(format!(".stage-{}", id));
    if fs::create_dir_all(&stage).is_err() { return error_response(StatusCode::SERVICE_UNAVAILABLE, "project_storage_unavailable"); }
    for (path, content) in input.files {
        let Some(target) = safe_path(&path).map(|path| stage.join(path)) else { let _ = fs::remove_dir_all(&stage); return error_response(StatusCode::BAD_REQUEST, "invalid_path"); };
        if let Some(parent) = target.parent() { if fs::create_dir_all(parent).is_err() { let _ = fs::remove_dir_all(&stage); return error_response(StatusCode::SERVICE_UNAVAILABLE, "project_storage_unavailable"); } }
        if fs::write(target, content).is_err() { let _ = fs::remove_dir_all(&stage); return error_response(StatusCode::SERVICE_UNAVAILABLE, "project_storage_unavailable"); }
    }
    let metadata = serde_json::json!({"id": id, "created_at": chrono_like_now(), "file_count": collect_file_paths(&stage, &stage).map(|files| files.len()).unwrap_or(0)});
    if fs::write(stage.join(".checkpoint.json"), serde_json::to_vec(&metadata).unwrap_or_default()).is_err() || fs::rename(&stage, checkpoints.join(&id)).is_err() { let _ = fs::remove_dir_all(&stage); return error_response(StatusCode::SERVICE_UNAVAILABLE, "project_storage_unavailable"); }
    Json(serde_json::json!({"ok": true, "checkpoint": metadata})).into_response()
}

async fn list_checkpoints(
    State(state): State<AppState>, headers: HeaderMap, AxumPath(project_id): AxumPath<String>,
) -> Response {
    let Some(pool) = state.db.as_ref() else { return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable"); };
    let Some(user) = authenticated(pool, &state, &headers).await else { return error_response(StatusCode::UNAUTHORIZED, "unauthenticated"); };
    let Some(root) = project_root(&state, &user.id, &project_id) else { return error_response(StatusCode::BAD_REQUEST, "invalid_project_id"); };
    let checkpoints = root.join(".checkpoints");
    let list = fs::read_dir(checkpoints).map(|entries| entries.filter_map(Result::ok).filter_map(|entry| serde_json::from_slice(&fs::read(entry.path().join(".checkpoint.json")).ok()?).ok()).collect::<Vec<serde_json::Value>>()).unwrap_or_default();
    Json(serde_json::json!({"checkpoints": list})).into_response()
}

async fn read_checkpoint(
    State(state): State<AppState>, headers: HeaderMap, AxumPath((project_id, checkpoint_id)): AxumPath<(String, String)>,
) -> Response {
    let Some(pool) = state.db.as_ref() else { return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable"); };
    let Some(user) = authenticated(pool, &state, &headers).await else { return error_response(StatusCode::UNAUTHORIZED, "unauthenticated"); };
    if !safe_segment(&checkpoint_id) { return error_response(StatusCode::BAD_REQUEST, "invalid_checkpoint_id"); }
    let Some(root) = project_root(&state, &user.id, &project_id) else { return error_response(StatusCode::BAD_REQUEST, "invalid_project_id"); };
    let checkpoint = root.join(".checkpoints").join(&checkpoint_id);
    if !checkpoint.is_dir() { return error_response(StatusCode::NOT_FOUND, "checkpoint_not_found"); }
    let files = collect_file_paths(&checkpoint, &checkpoint).ok().unwrap_or_default().into_iter().filter_map(|(path, file)| Some((path, fs::read_to_string(file).ok()?))).collect::<std::collections::HashMap<_, _>>();
    Json(serde_json::json!({"checkpoint_id": checkpoint_id, "files": files})).into_response()
}

async fn read_file(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath((project_id, path)): AxumPath<(String, String)>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    let Some(user) = authenticated(pool, &state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    let Some(target) = file_path(&state, &user.id, &project_id, &path) else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_path");
    };
    match fs::read_to_string(&target) {
        Ok(content) => Json(FileContent { path, content }).into_response(),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            error_response(StatusCode::NOT_FOUND, "file_not_found")
        }
        Err(error) => {
            tracing::error!(?error, "project file read failed");
            error_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                "project_storage_unavailable",
            )
        }
    }
}

async fn write_file(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath((project_id, path)): AxumPath<(String, String)>,
    Json(input): Json<FileWrite>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    let Some(user) = authenticated(pool, &state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    let Some(target) = file_path(&state, &user.id, &project_id, &path) else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_path");
    };
    if input.content.len() > 2_000_000 {
        return error_response(StatusCode::PAYLOAD_TOO_LARGE, "file_too_large");
    }
    let current_size = project_size(&state, &user.id, &project_id);
    let previous_size = fs::metadata(&target)
        .map(|metadata| metadata.len())
        .unwrap_or(0);
    let next_size = current_size
        .saturating_sub(previous_size)
        .saturating_add(input.content.len() as u64);
    if next_size > MAX_PROJECT_BYTES {
        return error_response(StatusCode::PAYLOAD_TOO_LARGE, "project_quota_exceeded");
    }
    if let Some(parent) = target.parent() {
        if let Err(error) = fs::create_dir_all(parent) {
            tracing::error!(?error, "project directory creation failed");
            return error_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                "project_storage_unavailable",
            );
        }
    }
    match fs::write(&target, input.content) {
        Ok(()) => {
            tracing::info!(
                event = "galileo.file.written",
                user_id = %user.id,
                project_id = %project_id,
                path = %path
            );
            Json(serde_json::json!({"ok":true,"path":path})).into_response()
        }
        Err(error) => {
            tracing::error!(?error, "project file write failed");
            error_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                "project_storage_unavailable",
            )
        }
    }
}

async fn delete_file(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath((project_id, path)): AxumPath<(String, String)>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    let Some(user) = authenticated(pool, &state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    let Some(target) = file_path(&state, &user.id, &project_id, &path) else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_path");
    };
    match fs::metadata(&target) {
        Ok(metadata) if metadata.is_dir() => fs::remove_dir_all(&target),
        Ok(_) => fs::remove_file(&target),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return error_response(StatusCode::NOT_FOUND, "file_not_found");
        }
        Err(error) => Err(error),
    }
    .map_or_else(
        |error| {
            tracing::error!(?error, "project file delete failed");
            error_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                "project_storage_unavailable",
            )
        },
        |_| Json(serde_json::json!({"ok": true, "path": path})).into_response(),
    )
}

async fn rename_file(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(project_id): AxumPath<String>,
    Json(input): Json<FileMove>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    let Some(user) = authenticated(pool, &state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    let Some(old) = file_path(&state, &user.id, &project_id, &input.path) else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_path");
    };
    let Some(new) = file_path(&state, &user.id, &project_id, &input.new_path) else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_path");
    };
    if new.exists() {
        return error_response(StatusCode::CONFLICT, "file_exists");
    }
    if let Some(parent) = new.parent() {
        if let Err(error) = fs::create_dir_all(parent) {
            tracing::error!(?error, "project rename directory creation failed");
            return error_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                "project_storage_unavailable",
            );
        }
    }
    match fs::rename(old, new) {
        Ok(()) => Json(serde_json::json!({"ok": true, "path": input.new_path})).into_response(),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            error_response(StatusCode::NOT_FOUND, "file_not_found")
        }
        Err(error) => {
            tracing::error!(?error, "project file rename failed");
            error_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                "project_storage_unavailable",
            )
        }
    }
}

async fn duplicate_file(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(project_id): AxumPath<String>,
    Json(input): Json<FileMove>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    let Some(user) = authenticated(pool, &state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    let Some(source) = file_path(&state, &user.id, &project_id, &input.path) else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_path");
    };
    let Some(target) = file_path(&state, &user.id, &project_id, &input.new_path) else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_path");
    };
    if target.exists() {
        return error_response(StatusCode::CONFLICT, "file_exists");
    }
    if let Some(parent) = target.parent() {
        if let Err(error) = fs::create_dir_all(parent) {
            tracing::error!(?error, "project duplicate directory creation failed");
            return error_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                "project_storage_unavailable",
            );
        }
    }
    match fs::copy(source, target) {
        Ok(_) => Json(serde_json::json!({"ok": true, "path": input.new_path})).into_response(),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            error_response(StatusCode::NOT_FOUND, "file_not_found")
        }
        Err(error) => {
            tracing::error!(?error, "project file duplicate failed");
            error_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                "project_storage_unavailable",
            )
        }
    }
}

async fn export_zip(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(project_id): AxumPath<String>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    let Some(user) = authenticated(pool, &state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    let Some(root) = project_root(&state, &user.id, &project_id) else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_project_id");
    };
    let mut output = Cursor::new(Vec::new());
    let mut archive = ZipWriter::new(&mut output);
    let options = SimpleFileOptions::default().compression_method(zip::CompressionMethod::Deflated);
    let entries = match collect_file_paths(&root, &root) {
        Ok(entries) => entries,
        Err(error) => {
            tracing::error!(?error, "project export listing failed");
            return error_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                "project_storage_unavailable",
            );
        }
    };
    for (path, target) in entries {
        let Ok(content) = fs::read(&target) else {
            return error_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                "project_storage_unavailable",
            );
        };
        if archive.start_file(path, options).is_err() || archive.write_all(&content).is_err() {
            return error_response(StatusCode::INTERNAL_SERVER_ERROR, "project_export_failed");
        }
    }
    if archive.finish().is_err() {
        return error_response(StatusCode::INTERNAL_SERVER_ERROR, "project_export_failed");
    }
    Response::builder()
        .status(StatusCode::OK)
        .header("content-type", "application/zip")
        .header(
            "content-disposition",
            format!("attachment; filename=project-{}.zip", project_id),
        )
        .body(axum::body::Body::from(output.into_inner()))
        .unwrap_or_else(|_| {
            error_response(StatusCode::INTERNAL_SERVER_ERROR, "project_export_failed")
        })
}

async fn import_zip(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(project_id): AxumPath<String>,
    body: axum::body::Bytes,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    let Some(user) = authenticated(pool, &state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    let Some(root) = project_root(&state, &user.id, &project_id) else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_project_id");
    };
    if body.len() > 50 * 1024 * 1024 {
        return error_response(StatusCode::PAYLOAD_TOO_LARGE, "archive_too_large");
    }
    let mut archive = match ZipArchive::new(Cursor::new(body)) {
        Ok(archive) => archive,
        Err(_) => return error_response(StatusCode::BAD_REQUEST, "invalid_archive"),
    };
    let stage = root.join(format!(".import-stage-{}", Uuid::new_v4().simple()));
    if let Err(error) = fs::create_dir(&stage) {
        tracing::error!(?error, "project import staging failed");
        return error_response(
            StatusCode::INTERNAL_SERVER_ERROR,
            "project_storage_unavailable",
        );
    }
    let mut total = project_size(&state, &user.id, &project_id);
    let result = (|| -> std::io::Result<usize> {
        let mut imported = 0usize;
        for index in 0..archive.len() {
            let mut file = archive.by_index(index).map_err(|_| {
                std::io::Error::new(std::io::ErrorKind::InvalidData, "invalid archive entry")
            })?;
            let Some(relative) = file.enclosed_name() else {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidInput,
                    "archive path escaped project",
                ));
            };
            if relative.as_os_str().is_empty()
                || relative.components().any(|component| {
                    matches!(
                        component,
                        Component::ParentDir | Component::RootDir | Component::Prefix(_)
                    )
                })
            {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidInput,
                    "invalid archive path",
                ));
            }
            if relative
                .file_name()
                .is_some_and(|name| name == ".meta.json" || name == ".preview.log")
            {
                continue;
            }
            let target = stage.join(&relative);
            if file.is_dir() {
                fs::create_dir_all(&target)?;
                continue;
            }
            let mut content = Vec::new();
            file.read_to_end(&mut content).map_err(|_| {
                std::io::Error::new(std::io::ErrorKind::InvalidData, "archive read failed")
            })?;
            total = total.saturating_add(content.len() as u64);
            if total > MAX_PROJECT_BYTES {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::FileTooLarge,
                    "project quota exceeded",
                ));
            }
            if let Some(parent) = target.parent() {
                fs::create_dir_all(parent)?;
            }
            fs::write(target, content)?;
            imported += 1;
        }
        for entry in collect_file_paths(&stage, &stage)? {
            let target = root.join(&entry.0);
            if let Some(parent) = target.parent() {
                fs::create_dir_all(parent)?;
            }
            fs::rename(entry.1, target)?;
        }
        Ok(imported)
    })();
    let _ = fs::remove_dir_all(&stage);
    match result {
        Ok(imported) => {
            Json(serde_json::json!({ "ok": true, "imported": imported })).into_response()
        }
        Err(error) if error.kind() == std::io::ErrorKind::FileTooLarge => {
            error_response(StatusCode::PAYLOAD_TOO_LARGE, "project_quota_exceeded")
        }
        Err(error) => {
            tracing::warn!(?error, "project archive import failed");
            error_response(StatusCode::BAD_REQUEST, "invalid_archive")
        }
    }
}

async fn create_folder(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(project_id): AxumPath<String>,
    Json(input): Json<FilePath>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    let Some(user) = authenticated(pool, &state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    let Some(target) = file_path(&state, &user.id, &project_id, &input.path) else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_path");
    };
    match fs::create_dir_all(&target) {
        Ok(()) => Json(serde_json::json!({ "ok": true, "path": input.path })).into_response(),
        Err(error) => {
            tracing::error!(?error, "project folder creation failed");
            error_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                "project_storage_unavailable",
            )
        }
    }
}

async fn delete_tree(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(project_id): AxumPath<String>,
    Json(input): Json<FilePath>,
) -> Response {
    let Some(pool) = state.db.as_ref() else {
        return error_response(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable");
    };
    let Some(user) = authenticated(pool, &state, &headers).await else {
        return error_response(StatusCode::UNAUTHORIZED, "unauthenticated");
    };
    let Some(target) = file_path(&state, &user.id, &project_id, &input.path) else {
        return error_response(StatusCode::BAD_REQUEST, "invalid_path");
    };
    match fs::remove_dir_all(target) {
        Ok(()) => Json(serde_json::json!({"ok": true, "path": input.path})).into_response(),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            error_response(StatusCode::NOT_FOUND, "folder_not_found")
        }
        Err(error) => {
            tracing::error!(?error, "project tree delete failed");
            error_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                "project_storage_unavailable",
            )
        }
    }
}

async fn authenticated(
    pool: &sqlx::MySqlPool,
    state: &AppState,
    headers: &HeaderMap,
) -> Option<auth::PublicUser> {
    auth::authenticated_user(pool, state, headers)
        .await
        .ok()
        .flatten()
}

fn project_root(state: &AppState, user_id: &str, project_id: &str) -> Option<PathBuf> {
    if !safe_segment(user_id) || !safe_segment(project_id) {
        return None;
    }
    let root = state.projects_root.join(user_id).join(project_id);
    let metadata = fs::symlink_metadata(&root).ok()?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return None;
    }
    Some(root)
}

fn file_path(state: &AppState, user_id: &str, project_id: &str, path: &str) -> Option<PathBuf> {
    let root = project_root(state, user_id, project_id)?;
    let relative = safe_path(path)?;
    let root_canonical = fs::canonicalize(&root).ok()?;
    let target = root.join(relative);
    let existing = nearest_existing(&target)?;
    let existing_canonical = fs::canonicalize(existing).ok()?;
    if !existing_canonical.starts_with(&root_canonical) {
        return None;
    }
    Some(target)
}

fn nearest_existing(path: &Path) -> Option<PathBuf> {
    let mut current = path.to_owned();
    loop {
        if current.exists() {
            return Some(current);
        }
        if !current.pop() {
            return None;
        }
    }
}

fn project_size(state: &AppState, user_id: &str, project_id: &str) -> u64 {
    let Some(root) = project_root(state, user_id, project_id) else {
        return 0;
    };
    collect_files_with_size(&root).unwrap_or_default()
}

fn collect_files_with_size(root: &Path) -> std::io::Result<u64> {
    let mut total = 0u64;
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        if matches!(entry.file_name().to_str(), Some(".meta.json" | ".checkpoints")) || entry.file_type()?.is_symlink() {
            continue;
        }
        let path = entry.path();
        if path.is_dir() {
            total = total.saturating_add(collect_files_with_size(&path)?);
        } else if path.is_file() {
            total = total.saturating_add(entry.metadata()?.len());
        }
    }
    Ok(total)
}
fn safe_segment(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 100
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}
fn safe_path(value: &str) -> Option<PathBuf> {
    let path = PathBuf::from(value);
    if value.is_empty()
        || path.is_absolute()
        || path.components().any(|c| {
            matches!(
                c,
                Component::ParentDir | Component::RootDir | Component::Prefix(_)
            )
        })
    {
        None
    } else {
        Some(path)
    }
}
fn collect_file_paths(root: &Path, current: &Path) -> std::io::Result<Vec<(String, PathBuf)>> {
    let mut files = Vec::new();
    for entry in fs::read_dir(current)? {
        let entry = entry?;
        let path = entry.path();
        if matches!(entry.file_name().to_str(), Some(".meta.json" | ".checkpoints" | ".preview.log" | ".checkpoint.json"))
            || entry.file_type()?.is_symlink()
        {
            continue;
        }
        if path.is_dir() {
            files.extend(collect_file_paths(root, &path)?);
        } else if path.is_file() {
            files.push((
                path.strip_prefix(root)
                    .unwrap_or(&path)
                    .to_string_lossy()
                    .replace('\\', "/"),
                path,
            ));
        }
    }
    Ok(files)
}

fn collect_files(root: &Path, current: &Path) -> std::io::Result<Vec<FileEntry>> {
    let mut files = Vec::new();
    if !current.exists() {
        return Ok(files);
    }
    for entry in fs::read_dir(current)? {
        let entry = entry?;
        let path = entry.path();
        if matches!(entry.file_name().to_str(), Some(".meta.json" | ".checkpoints")) || entry.file_type()?.is_symlink() {
            continue;
        }
        if path.is_dir() {
            files.extend(collect_files(root, &path)?);
        } else if path.is_file() {
            files.push(FileEntry {
                path: path
                    .strip_prefix(root)
                    .unwrap_or(&path)
                    .to_string_lossy()
                    .replace('\\', "/"),
                size: entry.metadata()?.len(),
            });
        }
    }
    Ok(files)
}

pub(crate) fn load_projects(root: &Path) -> std::io::Result<Vec<Project>> {
    let mut projects = Vec::new();
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let path = entry.path();
        if !path.is_dir() || entry.file_type()?.is_symlink() {
            continue;
        }
        let id = entry.file_name().to_string_lossy().into_owned();
        if !is_safe_project_id(&id) {
            continue;
        }
        let metadata = read_metadata(&path);
        let created_at = metadata
            .get("created_at")
            .and_then(serde_json::Value::as_str)
            .map(str::to_owned)
            .or_else(|| {
                entry
                    .metadata()
                    .ok()
                    .and_then(|value| value.modified().ok())
                    .map(|_| chrono_like_now())
            })
            .unwrap_or_else(chrono_like_now);
        projects.push(Project {
            id: id.clone(),
            name: metadata
                .get("name")
                .and_then(serde_json::Value::as_str)
                .filter(|value| !value.is_empty())
                .unwrap_or(&id)
                .to_owned(),
            description: metadata
                .get("description")
                .and_then(serde_json::Value::as_str)
                .unwrap_or_default()
                .to_owned(),
            created_at,
            file_count: count_files(&path),
        });
    }
    projects.sort_by(|a, b| b.created_at.cmp(&a.created_at));
    Ok(projects)
}

fn read_metadata(root: &Path) -> serde_json::Value {
    fs::read_to_string(root.join(".meta.json"))
        .ok()
        .and_then(|content| serde_json::from_str(&content).ok())
        .unwrap_or_else(|| serde_json::json!({}))
}

fn count_files(root: &Path) -> usize {
    let Ok(entries) = fs::read_dir(root) else {
        return 0;
    };
    entries
        .filter_map(Result::ok)
        .map(|entry| {
            let path = entry.path();
            if matches!(entry.file_name().to_str(), Some(".meta.json" | ".checkpoints"))
                || entry.file_type().map(|t| t.is_symlink()).unwrap_or(true)
            {
                return 0;
            }
            if path.is_dir() {
                count_files(&path)
            } else if path.is_file() {
                1
            } else {
                0
            }
        })
        .sum()
}

fn unique_slug(root: &Path, name: &str) -> Option<String> {
    let base = slugify(name);
    let base = if base.is_empty() {
        "project".to_owned()
    } else {
        base
    };
    if !root.join(&base).exists() {
        return Some(base);
    }
    for attempt in 1..=20 {
        let candidate = format!("{}-{}", base, attempt);
        if !root.join(&candidate).exists() {
            return Some(candidate);
        }
    }
    Some(format!(
        "{}-{}",
        base,
        &Uuid::new_v4().simple().to_string()[..8]
    ))
}

fn slugify(value: &str) -> String {
    let mut result = String::new();
    let mut separator = false;
    for character in value.chars() {
        if character.is_ascii_alphanumeric() {
            result.push(character.to_ascii_lowercase());
            separator = false;
        } else if !result.is_empty() {
            separator = true;
        }
        if separator && !result.ends_with('-') {
            result.push('-');
        }
    }
    result.trim_matches('-').to_owned()
}

fn is_safe_project_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 100
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
}

/// Format the current time as RFC 3339 / PHP `date('c')`, e.g.
/// `2026-08-15T03:05:51+00:00`. This keeps `.meta.json` compatible with the
/// legacy PHP-created projects (and parseable by `new Date(...)` in the web
/// UI) without adding a date-time dependency.
fn chrono_like_now() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_secs())
        .unwrap_or_default();
    // Howard Hinnant's civil-from-days algorithm (proleptic Gregorian).
    let days = (seconds / 86_400) as i64;
    let secs_of_day = (seconds % 86_400) as i64;
    let (hour, minute, second) = (
        secs_of_day / 3_600,
        (secs_of_day % 3_600) / 60,
        secs_of_day % 60,
    );
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if month <= 2 { year + 1 } else { year };
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}+00:00",
        year, month, day, hour, minute, second
    )
}
