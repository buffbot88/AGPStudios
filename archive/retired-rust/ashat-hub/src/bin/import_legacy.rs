use std::{
    env,
    path::{Component, Path, PathBuf},
};

use sha2::{Digest, Sha256};
use sqlx::{mysql::MySqlPoolOptions, FromRow, MySqlPool};

#[derive(FromRow)]
struct LegacyFile {
    user_id: String,
    path: String,
    content: Option<String>,
}

#[derive(Default)]
struct Report {
    eligible: usize,
    copied: usize,
    skipped: usize,
    mismatched: usize,
    invalid: usize,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let database = env::var("ASHAT_DATABASE_URL").or_else(|_| env::var("DATABASE_URL"))?;
    let root = PathBuf::from(
        env::var("ASHAT_PROJECTS_ROOT").unwrap_or_else(|_| "modules/AshatHub/projects".into()),
    );
    let args: Vec<String> = env::args().collect();
    let overwrite = args.iter().any(|arg| arg == "--overwrite");
    let dry_run = args.iter().any(|arg| arg == "--dry-run");
    let verify = args.iter().any(|arg| arg == "--verify") || dry_run;
    let pool = MySqlPoolOptions::new()
        .max_connections(4)
        .connect(&database)
        .await?;
    let report = import(&pool, &root, overwrite, dry_run, verify).await?;
    println!(
        "legacy import: eligible={} copied={} skipped={} mismatched={} invalid={} dry_run={} overwrite={}",
        report.eligible, report.copied, report.skipped, report.mismatched, report.invalid, dry_run, overwrite
    );
    if report.mismatched > 0 || report.invalid > 0 {
        return Err("legacy import verification failed".into());
    }
    Ok(())
}

async fn import(
    pool: &MySqlPool,
    root: &Path,
    overwrite: bool,
    dry_run: bool,
    verify: bool,
) -> Result<Report, Box<dyn std::error::Error>> {
    let rows = sqlx::query_as::<_, LegacyFile>(
        "SELECT user_id, path, content FROM files ORDER BY user_id, path",
    )
    .fetch_all(pool)
    .await?;
    let mut report = Report::default();
    for row in rows {
        let mut parts = row.path.splitn(2, '/');
        let project = parts.next().unwrap_or_default();
        let relative = parts.next().unwrap_or_default();
        let Some(relative) = safe_path(relative) else {
            report.invalid += 1;
            continue;
        };
        if !safe_segment(&row.user_id) || !safe_segment(project) {
            report.invalid += 1;
            continue;
        }
        report.eligible += 1;
        let content = row.content.unwrap_or_default();
        let target = root.join(&row.user_id).join(project).join(relative);
        if target.exists() {
            report.skipped += 1;
            if verify && file_hash(&target)? != hash(content.as_bytes()) {
                report.mismatched += 1;
            }
            if !overwrite || dry_run {
                continue;
            }
        }
        if dry_run {
            continue;
        }
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(target, content)?;
        report.copied += 1;
    }
    Ok(report)
}

fn file_hash(path: &Path) -> std::io::Result<String> {
    Ok(hash(&std::fs::read(path)?))
}

fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
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

#[cfg(test)]
mod tests {
    use super::{hash, safe_path};

    #[test]
    fn hashes_are_stable_and_paths_cannot_escape() {
        assert_eq!(
            hash(b"hello"),
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        );
        assert!(safe_path("src/main.rs").is_some());
        assert!(safe_path("../escape").is_none());
    }
}
