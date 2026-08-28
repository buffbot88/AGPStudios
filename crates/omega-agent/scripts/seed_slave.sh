#!/usr/bin/env bash
#
# seed_slave.sh — seed or update an ANH Slave Edition peer from this master.
#
# Copies the Omega server (source + prebuilt aarch64 release binary +
# bin/llama-server), generates a slave server-config.json, applies the SELinux
# exec labels (bin_t via semanage fcontext + restorecon), installs the systemd
# unit with the restorecon ExecStartPre guard, starts the service, and verifies
# /health. Idempotent: re-running after a master build propagates the update —
# this script is the manual half of the future update-propagation system.
#
# Usage:
#   scripts/seed_slave.sh <ssh-host> [install-dir] [bind-port]
#   scripts/seed_slave.sh opc@150.136.208.93
#   scripts/seed_slave.sh opc@129.213.147.225 /home/opc/Projects/ashatneuralhost-slave 8083
#   UPDATE_MODELS=1 scripts/seed_slave.sh opc@150.136.208.93   # also refresh models
#
# Connection details live in Beta_Delta.md; auth uses ./oraclehost_id_rsa.
# The slave is a headless inference server: frontend/, logs/, workspaces/ and
# the master's private key are never synced. Models are additive by default
# (files already on the slave are never overwritten); set UPDATE_MODELS=1 to
# refresh them from the master.
#
# Rollback: the previously-deployed binary is kept as .previous and restored
# automatically if the new build fails to become healthy.

set -euo pipefail

MASTER="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$MASTER"

HOST="${1:?usage: $0 <ssh-host> [install-dir] [bind-port]}"
INSTALL="${2:-/home/opc/Projects/ashatneuralhost-slave}"
PORT="${3:-8082}"
HEALTH_PORT="$PORT"
[ "$PORT" = "8082" ] && HEALTH_PORT="18082"
[ "$PORT" = "8088" ] && HEALTH_PORT="18088"
KEY="$MASTER/oraclehost_id_rsa"
BIN_NAME="ashat-master-coding-agent"
UPDATE_MODELS="${UPDATE_MODELS:-0}"
RAND_SUF="$$-$(date +%s)"

SSH_OPTS=(-i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new)

log()  { printf '\n==> %s\n' "$*"; }

log "master:    $MASTER"
log "host:      $HOST"
log "install:   $INSTALL"
log "bind port: $PORT"
[ "$UPDATE_MODELS" = "1" ] && log "models:    UPDATE_MODELS=1 (refreshing)"

# --- preflight -------------------------------------------------------------
[ -f "$KEY" ] || { echo "missing $KEY — seed cannot authenticate"; exit 1; }
chmod 600 "$KEY"
[ -f "target/release/$BIN_NAME" ] || {
    echo "missing target/release/$BIN_NAME — run: cargo build --release"; exit 1
}
[ -f "bin/llama-server" ] || { echo "missing bin/llama-server"; exit 1; }
ssh "${SSH_OPTS[@]}" "$HOST" "sudo -n true" >/dev/null 2>&1 || {
    echo "passwordless sudo required on $HOST (opc image default)"; exit 1
}
LOCAL_ARCH="$(uname -m)"
REMOTE_ARCH="$(ssh "${SSH_OPTS[@]}" "$HOST" "uname -m" | tr -d '\r')"
[ "$LOCAL_ARCH" = "$REMOTE_ARCH" ] || {
    echo "arch mismatch: local=$LOCAL_ARCH remote=$REMOTE_ARCH — prebuilt binary not portable"; exit 1
}
log "arch: $LOCAL_ARCH (matches)"

# --- 1. sync source + tools (never clobber models/logs/workspaces) ----------
log "syncing source tree + bin/"
ssh "${SSH_OPTS[@]}" "$HOST" "mkdir -p '$INSTALL'"
rsync -az -e "ssh ${SSH_OPTS[*]}" \
    --exclude 'target' \
    --exclude 'models' \
    --exclude 'frontend' \
    --exclude 'logs' \
    --exclude 'workspaces' \
    --exclude 'oraclehost_id_rsa' \
    --exclude 'Beta_Delta.md' \
    --exclude 'server-config.json' \
    --exclude '.git' \
    "$MASTER/" "$HOST:$INSTALL/"

# --- 2. prebuilt release binary, keeping a rollback copy ----------------------
log "syncing release binary (previous kept for rollback)"
ssh "${SSH_OPTS[@]}" "$HOST" "mkdir -p '$INSTALL/target/release'"
ssh "${SSH_OPTS[@]}" "$HOST" \
    "cp -p '$INSTALL/target/release/$BIN_NAME' '$INSTALL/target/release/$BIN_NAME.previous' 2>/dev/null || true"
rsync -az -e "ssh ${SSH_OPTS[*]}" \
    "target/release/$BIN_NAME" "$HOST:$INSTALL/target/release/"

# --- 3. models: additive unless UPDATE_MODELS=1 ------------------------------
if [ "$UPDATE_MODELS" = "1" ]; then
    log "syncing models (UPDATE_MODELS=1: refreshing from master)"
    rsync -az -e "ssh ${SSH_OPTS[*]}" \
        --exclude 'LFM2.5-230M*' \
        "models/" "$HOST:$INSTALL/models/"
else
    log "syncing models (additive; existing slave models kept)"
    rsync -az --ignore-existing -e "ssh ${SSH_OPTS[*]}" \
        --exclude 'LFM2.5-230M*' \
        "models/" "$HOST:$INSTALL/models/"
fi

# --- 4. resolve model hints from what the slave actually has ------------------
# Router: use the 350M text model; slaves do not need the VL model.
ORCH_HINT="$(ssh "${SSH_OPTS[@]}" "$HOST" \
    "ls '$INSTALL/models/' | grep -i '350M' | grep -iE 'Q4' | head -1" | tr -d '\r')"
[ -n "$ORCH_HINT" ] || { echo "no 350M model present on slave; aborting"; exit 1; }
log "orchestrator hint: $ORCH_HINT"

# Remove the retired VL router from slave hosts; Omega retains its source copy.
ssh "${SSH_OPTS[@]}" "$HOST" "rm -f '$INSTALL/models/'LFM2.5-VL-450M*"
INF_HINT="$(ssh "${SSH_OPTS[@]}" "$HOST" \
    "ls '$INSTALL/models/' | grep -iE '1\\.2B.*Instruct' | grep -iE 'Q4' | head -1" | tr -d '\r')"
if [ -z "$INF_HINT" ]; then
    INF_HINT="$(ssh "${SSH_OPTS[@]}" "$HOST" \
        "ls '$INSTALL/models/' | grep -iE '1\\.2B.*Instruct' | head -1" | tr -d '\r')"
fi
[ -n "$INF_HINT" ] || { echo "no 1.2B-Instruct model present on slave; aborting"; exit 1; }
log "orchestrator hint: $ORCH_HINT"
log "inference hint:    $INF_HINT"

# --- 5. generate the slave server-config.json ---------------------------------
ASHAT_KEY="$(python3 -c 'import json;print(json.load(open("server-config.json"))["ASHAT_KEY"])')"
TMPCFG="$(mktemp)"
SEED_PORT="$PORT" SEED_KEY="$ASHAT_KEY" SEED_ORCH="$ORCH_HINT" SEED_INF="$INF_HINT" \
python3 - "$TMPCFG" <<'PYEOF'
import json, os, sys
port = int(os.environ["SEED_PORT"])
internal_port = {8082: 18082, 8088: 18088}.get(port, port)
cfg = {
    "ASHAT_KEY": os.environ["SEED_KEY"],
    "server": {
        "bind": f"127.0.0.1:{internal_port}",
        "orchestrator_port": 18079,
        "orchestrator_binary_default": "llama-server",
    },
    "models": {
        "dir": "models",
        "orchestrator_hint": os.environ["SEED_ORCH"],
        "inference_hint": os.environ["SEED_INF"],
    },
    "inference": {
        "context": 4096,
        "max_tokens": 1024,
        "timeout_seconds": 180,
        "llama_threads": 2,
        "llama_gpu_layers": 0,
    },
    "orchestrator_pool": {
        "ports_baseline": [18079],
        "ports_extra":    [18078, 18077],
        "queue_max": 32,
        "spawn_attempts_before_503": 3,
    },
    "coding_agent_pool": {
        "ports": ([18180, 18181, 18182] if port == 8082 else [18080, 18081, 18082]),
        "queue_max": 32,
        "spawn_attempts_before_503": 3,
    },
    "row_chain": [
        {"id": "omega", "host": "127.0.0.1", "port": port, "enabled": True},
        {"id": "beta",  "host": "127.0.0.1", "port": 8082, "enabled": False},
        {"id": "delta", "host": "127.0.0.1", "port": 8083, "enabled": False},
    ],
    "metrics": {"persist_path": "logs/metrics.jsonl"},
    "hub": {"enabled": False, "url": ""},
    "workspace": {"dir": "workspaces"},
    "tool_loop": {"max_iterations": 5, "command_timeout_seconds": 10, "output_max_chars": 4000},
    "skills_db": {"enabled": False, "host": "127.0.0.1", "port": 3306,
                  "database": "", "user": "", "password": ""},
}
with open(sys.argv[1], "w") as f:
    json.dump(cfg, f, indent=2)
PYEOF
scp "${SSH_OPTS[@]}" -q "$TMPCFG" "$HOST:$INSTALL/server-config.json"
rm -f "$TMPCFG"

# --- 6. SELinux exec labels (same recipe as master) ---------------------------
log "applying SELinux bin_t labels"
ssh "${SSH_OPTS[@]}" "$HOST" "
    sudo semanage fcontext -a -t bin_t '$INSTALL/target/release/$BIN_NAME' 2>/dev/null || true
    sudo semanage fcontext -a -t bin_t '$INSTALL/bin/llama-server' 2>/dev/null || true
    sudo restorecon -v '$INSTALL/target/release/$BIN_NAME' '$INSTALL/bin/llama-server'"

# --- 7. systemd unit + restorecon ExecStartPre guard --------------------------
log "installing systemd unit"
UNIT_SRC="$(mktemp)"
cat > "$UNIT_SRC" <<EOF
[Unit]
Description=Ashat Neural Host Slave Edition (peer)
After=network.target

[Service]
Type=simple
User=opc
WorkingDirectory=$INSTALL
ExecStart=$INSTALL/target/release/$BIN_NAME
Restart=always
RestartSec=5
Environment=ASHAT_LLAMA_BIN=$INSTALL/bin/llama-server

[Install]
WantedBy=multi-user.target
EOF
DROPIN_SRC="$(mktemp)"
cat > "$DROPIN_SRC" <<EOF
[Service]
# SELinux enforcing: freshly-synced binaries land as user_home_t which the
# init_t domain cannot exec. Re-apply the bin_t label (registered via
# semanage fcontext) before each start.
ExecStartPre=/usr/sbin/restorecon -v $INSTALL/target/release/$BIN_NAME
ExecStartPre=/usr/sbin/restorecon -v $INSTALL/bin/llama-server
EOF
REMOTE_TMP="/tmp/ashat-seed-$RAND_SUF"
scp "${SSH_OPTS[@]}" -q "$UNIT_SRC" "$HOST:$REMOTE_TMP.service"
scp "${SSH_OPTS[@]}" -q "$DROPIN_SRC" "$HOST:$REMOTE_TMP.selinux.conf"
rm -f "$UNIT_SRC" "$DROPIN_SRC"
ssh "${SSH_OPTS[@]}" "$HOST" "
    sudo install -m 644 '$REMOTE_TMP.service' /etc/systemd/system/ashat-neural-host.service
    sudo mkdir -p /etc/systemd/system/ashat-neural-host.service.d
    sudo install -m 644 '$REMOTE_TMP.selinux.conf' /etc/systemd/system/ashat-neural-host.service.d/selinux.conf
    rm -f '$REMOTE_TMP.service' '$REMOTE_TMP.selinux.conf'
    sudo systemctl daemon-reload
    sudo systemctl enable ashat-neural-host.service
    sudo systemctl restart ashat-neural-host.service"

# --- 8. verify /health on the slave's bind port, roll back on failure ---------
wait_healthy() {
    for _ in $(seq 1 90); do
        code="$(ssh "${SSH_OPTS[@]}" "$HOST" \
            "curl -s -o /dev/null -w '%{http_code}' 'http://127.0.0.1:$HEALTH_PORT/health'" 2>/dev/null || true)"
        if [ "$code" = "200" ]; then return 0; fi
        sleep 2
    done
    return 1
}

log "waiting for slave /health on :$PORT"
if wait_healthy; then
    log "slave READY on :$PORT"
    ssh "${SSH_OPTS[@]}" "$HOST" "curl -s 'http://127.0.0.1:$HEALTH_PORT/health'; echo"
    ssh "${SSH_OPTS[@]}" "$HOST" "systemctl is-active ashat-neural-host.service"
else
    echo "new build did not become healthy within 180s — rolling back to .previous"
    ssh "${SSH_OPTS[@]}" "$HOST" "
        cp -p '$INSTALL/target/release/$BIN_NAME.previous' '$INSTALL/target/release/$BIN_NAME' 2>/dev/null || true
        sudo restorecon -v '$INSTALL/target/release/$BIN_NAME'
        sudo systemctl restart ashat-neural-host.service"
    if wait_healthy; then
        log "rollback complete — previous build serving on :$PORT"
        exit 1
    fi
    echo "rollback also failed; manual intervention required:"
    echo "  ssh ${SSH_OPTS[*]} $HOST 'journalctl -u ashat-neural-host.service -n 50'"
    exit 1
fi
