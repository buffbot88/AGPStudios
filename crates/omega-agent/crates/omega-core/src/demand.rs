//! Generic spawn-on-demand pool of `llama-server` children.
//!
//! Spawn-on-demand pool of `llama-server` children used by both the intent
//! router / Orchestrator (18079 baseline + 18078/18077 extras) and the
//! 1.2B Coding
//! Agent (18080/18081/18082); callers `acquire()` a slot and the guard reclaims
//! it on drop.

use std::{
    collections::{HashMap, VecDeque},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicU32, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant},
};
use tokio::{process::Command, time::sleep};

use crate::queue::WaitQueue;
use omega_common::metrics::MetricsStore;
use omega_common::types::{DemandAcquireError, DemandSpawnError, Pool};

#[derive(Debug, Clone)]
pub struct DemandSpec {
    pub binary: PathBuf,
    pub model: PathBuf,
    pub ctx: u32,
    pub threads: u32,
    pub gpu_layers: u32,
    pub host: String,
    /// Pin the model in RAM (`--mlock`).
    pub mlock: bool,
    pub always_alive: bool,
}

#[derive(Debug)]
pub struct DemandPool {
    pub name: Pool,
    pub spec: DemandSpec,
    pub baseline_port: Option<u16>,
    free_ports: Mutex<VecDeque<u16>>,
    active: Mutex<HashMap<u16, tokio::process::Child>>,
    baseline_child: Mutex<Option<tokio::process::Child>>,
    queue: WaitQueue,
    spawn_attempts_threshold: u32,
    consecutive_spawn_failures: AtomicU32,
    last_failure_reason: Mutex<Option<String>>,
}

impl DemandPool {
    pub fn builder(name: Pool, spec: DemandSpec) -> DemandPoolBuilder {
        DemandPoolBuilder {
            name,
            spec,
            queue_max: 32,
            spawn_attempts_threshold: 3,
        }
    }

    pub fn queue(&self) -> WaitQueue {
        self.queue.clone()
    }

    pub fn set_last_failure(&self, reason: impl Into<String>) {
        if let Ok(mut guard) = self.last_failure_reason.lock() {
            *guard = Some(reason.into());
        }
    }

    pub fn last_failure(&self) -> Option<String> {
        self.last_failure_reason.lock().ok().and_then(|g| g.clone())
    }

    /// Test whether the baseline port (if any) has a tracked child.
    pub async fn baseline_alive(&self) -> bool {
        if !self.spec.always_alive {
            return true;
        }
        let mut guard = self.baseline_child.lock().expect("baseline lock poisoned");
        match guard.as_mut() {
            None => false,
            Some(child) => matches!(child.try_wait(), Ok(None)),
        }
    }

    /// Spawn the always-on baseline child (if `spec.always_alive`). Polls
    /// /health until ready. The port is *not* added to free_ports.
    pub async fn seed_baseline(
        self: &Arc<Self>,
        metrics: &Arc<MetricsStore>,
    ) -> Result<u16, DemandSpawnError> {
        if !self.spec.always_alive {
            return Err(DemandSpawnError::BinaryNotFound);
        }
        let port = self
            .baseline_port
            .ok_or(DemandSpawnError::NoPortsAvailable)?;

        let mut child = spawn_llama_server(&self.spec, port).await?;
        if let Err(err) = wait_for_health(port, &self.spec.host).await {
            // Health never returned ok; reclaim the child before retiring.
            let _ = child.start_kill();
            let _ = child.wait().await;
            metrics.event(format!(
                "{} baseline on {port} abandoned (health): {err}",
                self.name.as_str()
            ));
            return Err(DemandSpawnError::HealthGaveUp(err.to_string()));
        }
        {
            let mut guard = self.baseline_child.lock().expect("baseline lock poisoned");
            *guard = Some(child);
        }
        metrics.event(format!(
            "baseline {:?} ready on port {port}",
            self.name.as_str()
        ));
        Ok(port)
    }

    /// Replace a dead baseline child. Supervised respawn path.
    pub async fn respawn_baseline(
        self: &Arc<Self>,
        metrics: &Arc<MetricsStore>,
    ) -> Result<u16, DemandSpawnError> {
        let port = self
            .baseline_port
            .ok_or(DemandSpawnError::NoPortsAvailable)?;
        let dead_child = {
            let mut guard = self.baseline_child.lock().expect("baseline lock poisoned");
            guard.take()
        };
        if let Some(mut c) = dead_child {
            let _ = c.start_kill();
            let _ = c.wait().await;
        }
        let mut child = spawn_llama_server(&self.spec, port).await?;
        if let Err(err) = wait_for_health(port, &self.spec.host).await {
            let _ = child.start_kill();
            let _ = child.wait().await;
            metrics.event(format!(
                "{} baseline respawn on {port} failed (health): {err}",
                self.name.as_str()
            ));
            return Err(DemandSpawnError::HealthGaveUp(err.to_string()));
        }
        {
            let mut guard = self.baseline_child.lock().expect("baseline lock poisoned");
            *guard = Some(child);
        }
        metrics.event(format!(
            "baseline {:?} respawned on port {port}",
            self.name.as_str()
        ));
        Ok(port)
    }

    /// Snapshot for /api/public_status.
    pub async fn snapshot(&self) -> PoolSnapshotCompact {
        let free: Vec<u16> = self
            .free_ports
            .lock()
            .expect("poisoned")
            .iter()
            .copied()
            .collect();
        let active_ports: Vec<u16> = self
            .active
            .lock()
            .expect("poisoned")
            .keys()
            .copied()
            .collect();
        let baseline_alive = if self.spec.always_alive {
            self.baseline_alive().await
        } else {
            true
        };
        PoolSnapshotCompact {
            ports_total: free.len()
                + active_ports.len()
                + if self.baseline_port.is_some() { 1 } else { 0 },
            ports_active: active_ports.len()
                + if baseline_alive && self.spec.always_alive {
                    1
                } else {
                    0
                },
            baseline_alive,
            extras_active: active_ports,
            free_ports: free,
            queue_depth: self.queue.depth().await,
            queue_limit: self.queue.limit(),
            last_failure_reason: self.last_failure(),
        }
    }
    /// Acquire a port for an immediate task. Returns the RAII `InstanceGuard`.
    /// If every spawn slot is busy, awaits the bounded FIFO and surfaces
    /// `QueueAgedOut` if the wait exceeds the timeout. Waiters that wake on a
    /// slot-available signal but lose the race back into the wait loop instead
    /// of returning `PoolExhausted`: that path is reserved for true saturation.
    pub async fn acquire(
        self: Arc<Self>,
        metrics: &Arc<MetricsStore>,
        timeout: Duration,
    ) -> Result<InstanceGuard, DemandAcquireError> {
        // Fast path: baseline always-on, just claim it.
        if self.spec.always_alive {
            self.consecutive_spawn_failures.store(0, Ordering::SeqCst);
            return Ok(InstanceGuard::baseline(
                Arc::clone(&self),
                self.baseline_port.unwrap_or(0),
            ));
        }

        // Fast path: free port available.
        if let Some(guard) = self.spawn_on_demand(metrics).await {
            return Ok(guard);
        }

        // Slow path: queue, then retry-acquire loop.
        let queue = self.queue();
        let accepted = queue.try_enqueue().await;
        if !accepted {
            let fails = self
                .consecutive_spawn_failures
                .fetch_add(1, Ordering::SeqCst)
                + 1;
            if fails > self.spawn_attempts_threshold {
                self.set_last_failure("queue full and spawn attempts exceeded threshold");
            }
            return Err(DemandAcquireError::PoolExhausted);
        }

        let wait_outcome = wait_and_pump(&queue, &self, metrics, timeout).await;
        if let Err(err) = wait_outcome {
            queue.remove().await;
            self.set_last_failure("queue head aged out before slot acquired");
            return Err(err);
        }
        Ok(wait_outcome.unwrap())
    }

    /// Try to immediately spawn a fresh instance. Returns `None` if every port
    /// is busy or the spawn health-check fails.
    async fn spawn_on_demand(
        self: &Arc<Self>,
        metrics: &Arc<MetricsStore>,
    ) -> Option<InstanceGuard> {
        let port = {
            let mut free = self.free_ports.lock().expect("free-ports lock poisoned");
            free.pop_front()?
        };

        match spawn_llama_server(&self.spec, port).await {
            Ok(child) => {
                {
                    let mut active = self.active.lock().expect("active lock poisoned");
                    active.insert(port, child);
                }
                if let Err(err) = wait_for_health(port, &self.spec.host).await {
                    let child = {
                        let mut active = self.active.lock().expect("active lock poisoned");
                        active.remove(&port)
                    };
                    if let Some(mut c) = child {
                        let _ = c.start_kill();
                        let _ = c.wait().await;
                    }
                    let mut free = self.free_ports.lock().expect("poisoned");
                    free.push_back(port);
                    self.set_last_failure(format!(
                        "spawned child on {port} but /health did not pass: {err}"
                    ));
                    metrics.event(format!(
                        "spawn-on {port} for {} abandoned (health)",
                        self.name.as_str()
                    ));
                    // Wake waiters so the pump retries the spawn rather than
                    // stalling until the acquire timeout.
                    self.queue().notify_slot_available();
                    return None;
                }
                metrics.event(format!("{} spawn-on {port} succeeded", self.name.as_str()));
                self.consecutive_spawn_failures.store(0, Ordering::SeqCst);
                Some(InstanceGuard::spawned(Arc::clone(self), port))
            }
            Err(err) => {
                let mut free = self.free_ports.lock().expect("poisoned");
                free.push_back(port);
                self.set_last_failure(err.to_string());
                metrics.event(format!(
                    "spawn-on {port} for {} failed: {err}",
                    self.name.as_str()
                ));
                let _ = self
                    .consecutive_spawn_failures
                    .fetch_add(1, Ordering::SeqCst);
                // Wake waiters so the pump retries the spawn rather than
                // stalling until the acquire timeout.
                self.queue().notify_slot_available();
                None
            }
        }
    }

    /// Kill any active spawn slot immediately. Called on graceful shutdown.
    pub async fn kill_active(&self) {
        let drain: Vec<(u16, tokio::process::Child)> = {
            let mut active = self.active.lock().expect("poisoned");
            active.drain().collect()
        };
        for (port, mut child) in drain {
            tracing::info!(port, "killing active child on shutdown");
            let _ = child.start_kill();
            let _ = child.wait().await;
        }
    }
}

/// RAII handle: held by the proxy while it streams the response. On drop,
/// the child is killed and the port is returned to the demand pool.
pub struct InstanceGuard {
    pool: Arc<DemandPool>,
    port: u16,
    is_baseline: bool,
}

impl InstanceGuard {
    pub(crate) fn spawned(pool: Arc<DemandPool>, port: u16) -> Self {
        Self {
            pool,
            port,
            is_baseline: false,
        }
    }

    pub(crate) fn baseline(pool: Arc<DemandPool>, port: u16) -> Self {
        Self {
            pool,
            port,
            is_baseline: true,
        }
    }

    pub fn port(&self) -> u16 {
        self.port
    }

    pub fn is_baseline(&self) -> bool {
        self.is_baseline
    }

    /// Borrowed reference to the pool. Used by `proxy.rs` for upstream URLs.
    pub fn pool(&self) -> &Arc<DemandPool> {
        &self.pool
    }
}

impl Drop for InstanceGuard {
    fn drop(&mut self) {
        let pool = Arc::clone(&self.pool);
        let port = self.port;
        let is_baseline = self.is_baseline;
        tokio::spawn(async move {
            if is_baseline {
                // Baseline is supervised, never killed on guard drop.
                pool.queue().notify_slot_available();
                return;
            }
            let child = {
                let mut active = match pool.active.lock() {
                    Ok(g) => g,
                    Err(_) => return,
                };
                active.remove(&port)
            };
            if let Some(mut c) = child {
                let _ = c.start_kill();
                let _ = c.wait().await;
            }
            {
                let mut free = match pool.free_ports.lock() {
                    Ok(g) => g,
                    Err(_) => return,
                };
                if !free.contains(&port) {
                    free.push_back(port);
                }
            }
            pool.queue().notify_slot_available();
        });
    }
}

#[derive(Debug, Clone)]
pub struct PoolSnapshotCompact {
    pub ports_total: usize,
    pub ports_active: usize,
    pub baseline_alive: bool,
    pub extras_active: Vec<u16>,
    /// Allocated ports with no live instance right now.
    pub free_ports: Vec<u16>,
    pub queue_depth: usize,
    pub queue_limit: usize,
    pub last_failure_reason: Option<String>,
}

pub struct DemandPoolBuilder {
    name: Pool,
    spec: DemandSpec,
    queue_max: usize,
    spawn_attempts_threshold: u32,
}

impl DemandPoolBuilder {
    pub fn queue_max(mut self, n: usize) -> Self {
        self.queue_max = n;
        self
    }
    pub fn spawn_attempts_before_503(mut self, n: u32) -> Self {
        self.spawn_attempts_threshold = n;
        self
    }
    pub fn build(self, ports: &[u16]) -> DemandPool {
        let mut free_ports = VecDeque::with_capacity(ports.len());
        for port in ports {
            free_ports.push_back(*port);
        }
        let baseline_port = if self.spec.always_alive {
            ports.first().copied()
        } else {
            None
        };
        DemandPool {
            name: self.name,
            spec: self.spec,
            baseline_port,
            free_ports: Mutex::new(free_ports),
            active: Mutex::new(HashMap::new()),
            baseline_child: Mutex::new(None),
            queue: WaitQueue::new(self.queue_max),
            spawn_attempts_threshold: self.spawn_attempts_threshold,
            consecutive_spawn_failures: AtomicU32::new(0),
            last_failure_reason: Mutex::new(None),
        }
    }
}

/// Wait for a slot-available signal and loop into `spawn_on_demand` until the
/// caller either succeeds or the timeout fires. Spurious wake-ups that don't
/// yield a free port (because we lost the race) re-enter the wait rather than
/// escalating to `PoolExhausted`.
async fn wait_and_pump(
    queue: &crate::queue::WaitQueue,
    pool: &Arc<DemandPool>,
    metrics: &Arc<MetricsStore>,
    timeout: Duration,
) -> Result<InstanceGuard, DemandAcquireError> {
    let deadline = Instant::now() + timeout;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(DemandAcquireError::QueueAgedOut);
        }
        let notified = tokio::time::timeout(remaining, queue.wait_for_slot()).await;
        match notified {
            Ok(()) => {
                if let Some(guard) = pool.spawn_on_demand(metrics).await {
                    return Ok(guard);
                }
                // Lost the race; loop back into the wait.
            }
            Err(_) => return Err(DemandAcquireError::QueueAgedOut),
        }
    }
}

/// Spawn `llama-server --host $host --port $port -m $model -c $ctx -t $threads -ngl $gpu_layers`.
async fn spawn_llama_server(
    spec: &DemandSpec,
    port: u16,
) -> Result<tokio::process::Child, DemandSpawnError> {
    if !Path::new(&spec.model).exists() {
        return Err(DemandSpawnError::MissingModel);
    }
    if !Path::new(&spec.binary).exists() {
        // Allow PATH-resolved binaries even though we can only check existence
        // at the location we resolved earlier.
        tracing::warn!(
            binary = %spec.binary.display(),
            "llama-server binary not present at the resolved path; PATH lookup still applies if command resolves elsewhere"
        );
    }
    let mut cmd = Command::new(&spec.binary);
    cmd.arg("--host")
        .arg(&spec.host)
        .arg("--port")
        .arg(port.to_string())
        .arg("-m")
        .arg(&spec.model)
        .arg("-c")
        .arg(spec.ctx.to_string())
        .arg("-t")
        .arg(spec.threads.to_string())
        .arg("-ngl")
        .arg(spec.gpu_layers.to_string());
    if spec.mlock {
        cmd.arg("--mlock");
    }
    cmd.stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true);
    let child = cmd.spawn().map_err(DemandSpawnError::CommanderSpawn)?;
    sleep(Duration::from_millis(150)).await;
    Ok(child)
}

async fn wait_for_health(port: u16, host: &str) -> Result<(), DemandSpawnError> {
    let url = format!("http://{host}:{port}/health");
    let client = match reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
    {
        Ok(c) => c,
        Err(err) => return Err(DemandSpawnError::HealthGaveUp(err.to_string())),
    };
    // 60 attempts x 500 ms: a 730 MB 1.2B GGUF on a loaded 1-core box can
    // take longer than the old 15 s window to finish loading.
    for attempt in 1..=60 {
        if let Ok(r) = client.get(&url).send().await {
            if r.status().is_success() {
                return Ok(());
            }
        }
        sleep(Duration::from_millis(500)).await;
        tracing::debug!(port, attempt, "waiting for /health");
    }
    Err(DemandSpawnError::HealthGaveUp(format!(
        "{url} never reported healthy in 30 s"
    )))
}

#[cfg(test)]
mod tests {
    use super::*;
    use omega_common::metrics::MetricsStore;
    use omega_common::types::Pool;

    fn spec(always_alive: bool, model: PathBuf) -> DemandSpec {
        DemandSpec {
            binary: PathBuf::from("llama-server"),
            model,
            ctx: 4096,
            threads: 2,
            gpu_layers: 0,
            host: "127.0.0.1".to_owned(),
            mlock: false,
            always_alive,
        }
    }

    fn metrics(name: &str) -> Arc<MetricsStore> {
        let path = std::env::temp_dir().join(format!(
            "omega-demand-metrics-{}-{name}.jsonl",
            std::process::id()
        ));
        Arc::new(MetricsStore::open(&path))
    }

    #[tokio::test]
    async fn builder_sets_baseline_port_for_always_alive() {
        let pool = DemandPool::builder(
            Pool::Orchestrator,
            spec(true, PathBuf::from("/nonexistent/model.gguf")),
        )
        .build(&[18079, 18078, 18077]);
        assert_eq!(pool.baseline_port, Some(18079));
        assert!(pool.spec.always_alive);
    }

    #[tokio::test]
    async fn acquire_on_unseeded_baseline_returns_baseline_guard() {
        let pool = Arc::new(
            DemandPool::builder(
                Pool::Orchestrator,
                spec(true, PathBuf::from("/nonexistent/model.gguf")),
            )
            .build(&[18079]),
        );
        let guard = pool
            .clone()
            .acquire(&metrics("baseline"), Duration::from_secs(1))
            .await
            .expect("baseline fast path");
        assert!(guard.is_baseline());
        assert_eq!(guard.port(), 18079);
    }

    #[tokio::test]
    async fn coding_pool_with_bad_model_ages_out_queue() {
        let pool = Arc::new(
            DemandPool::builder(
                Pool::CodingAgent,
                spec(false, PathBuf::from("/nonexistent/model.gguf")),
            )
            .build(&[18280]),
        );
        match pool
            .clone()
            .acquire(&metrics("aged"), Duration::from_millis(300))
            .await
        {
            Err(DemandAcquireError::QueueAgedOut) => {}
            Err(e) => panic!("expected QueueAgedOut, got {e:?}"),
            Ok(g) => panic!("unexpected guard for port {}", g.port()),
        }
        // Failed spawn returned the port to the free set; the waiter was removed.
        let snap = pool.snapshot().await;
        assert!(snap.free_ports.contains(&18280));
        assert_eq!(snap.queue_depth, 0);
    }
}
