#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${ASHAT_OMEGA_SSH_HOST:-opc@129.213.94.124}"
KEY="${ASHAT_OMEGA_SSH_KEY:-/var/oled/data/oraclehost_id_rsa}"
INSTALL="${ASHAT_OMEGA_INSTALL_DIR:-/var/oled/data/AshatCodingAgent}"
SSH=(ssh -i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new)
SCP=(scp -i "$KEY" -o BatchMode=yes)
[ -r "$KEY" ] || { echo "Omega SSH key unavailable" >&2; exit 1; }
cd "$ROOT"
cargo build --release -q -p omega-agent
mapfile -t LLAMA_LIBS < <(find /home/opc/llama.cpp-src/build/bin -maxdepth 1 -type f -name '*.so*')
[ "${#LLAMA_LIBS[@]}" -gt 0 ] || { echo "llama shared libraries unavailable" >&2; exit 1; }
"${SSH[@]}" "$HOST" "sudo mkdir -p '$INSTALL/bin' '$INSTALL/target/release' '$INSTALL/models' && sudo chown -R opc:opc '$INSTALL'"
"${SCP[@]}" target/release/omega-agent "$HOST:$INSTALL/target/release/omega-agent"
"${SCP[@]}" /home/opc/llama.cpp-src/build/bin/llama-server "${LLAMA_LIBS[@]}" "$HOST:$INSTALL/bin/"
"${SSH[@]}" "$HOST" "sudo semanage fcontext -a -t bin_t '$INSTALL/target/release/omega-agent' 2>/dev/null || true; sudo semanage fcontext -a -t bin_t '$INSTALL/bin(/.*)?' 2>/dev/null || true; sudo restorecon -R '$INSTALL/bin' '$INSTALL/target/release'; sudo systemctl enable omega-agent.service; sudo systemctl restart omega-agent.service"
for _ in $(seq 1 90); do
  code="$("${SSH[@]}" "$HOST" "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/ready" 2>/dev/null || true)"
  [ "$code" = 200 ] && exit 0
  sleep 2
done
echo "Omega did not become ready" >&2
exit 1
