//! Ashat's MySQL skills database (Phase 8 seam — implemented but inert).
//!
//! The advanced coding agent's `skill` tool looks skills up here. The query
//! path is fully implemented against MySQL but disabled by default
//! (`skills_db.enabled: false` in `server-config.json`) until the connection
//! details and schema are provided. Table contract: `skills(name, content)`.
//!
//! The MySQL client is an **optional dependency** behind the `skills-db`
//! cargo feature (default off) so default builds stay lean. To activate:
//! set `skills_db.enabled: true` with real connection details and rebuild
//! with `--features omega-core/skills-db`.

use omega_common::config::SkillsSection;

#[cfg(feature = "skills-db")]
use mysql::prelude::Queryable;

#[cfg_attr(not(feature = "skills-db"), allow(dead_code))]
#[derive(Debug, Clone)]
pub struct SkillDb {
    enabled: bool,
    host: String,
    port: u16,
    database: String,
    user: String,
    password: String,
}

impl SkillDb {
    pub fn from_config(cfg: &SkillsSection) -> Self {
        Self {
            enabled: cfg.enabled,
            host: cfg.host.clone(),
            port: cfg.port,
            database: cfg.database.clone(),
            user: cfg.user.clone(),
            password: cfg.password.clone(),
        }
    }

    /// A fully disabled instance (tests / before connection details land).
    pub fn disabled() -> Self {
        Self {
            enabled: false,
            host: String::new(),
            port: 0,
            database: String::new(),
            user: String::new(),
            password: String::new(),
        }
    }

    pub fn is_enabled(&self) -> bool {
        self.enabled
    }

    /// Look up one skill by name. Returns its content, or `None` when the
    /// skill does not exist. Errors when disabled, when the connection fails,
    /// or when this build lacks the `skills-db` feature.
    pub fn lookup(&self, name: &str) -> Result<Option<String>, String> {
        if !self.enabled {
            return Err(
                "skills database disabled in server-config.json (skills_db.enabled=false)"
                    .to_owned(),
            );
        }

        #[cfg(feature = "skills-db")]
        {
            let opts = mysql::OptsBuilder::new()
                .ip_or_hostname(Some(self.host.clone()))
                .tcp_port(self.port)
                .db_name(Some(self.database.clone()))
                .user(Some(self.user.clone()))
                .pass(Some(self.password.clone()));
            let mut conn = mysql::Conn::new(opts).map_err(|e| format!("connect failed: {e}"))?;
            conn.exec_first(
                "SELECT content FROM skills WHERE name = :name LIMIT 1",
                mysql::params! { "name" => name },
            )
            .map_err(|e| format!("query failed: {e}"))
        }

        #[cfg(not(feature = "skills-db"))]
        {
            let _ = name;
            Err(
                "skills database support not compiled into this build (rebuild with \
                 --features omega-core/skills-db)"
                    .to_owned(),
            )
        }
    }
}
