# Beta / Delta peer access + seeding

## SSH access (uses ./oraclehost_id_rsa, chmod 600)

Beta
ssh -i "./oraclehost_id_rsa" opc@150.136.208.93

Delta
ssh -i "./oraclehost_id_rsa" opc@129.213.147.225

## Seed / update a peer (ANH Slave Edition)

From the master repo root, the idempotent seed script copies the source tree,
prebuilt aarch64 release binary, bin/llama-server, and missing models; generates
the slave server-config.json (bind 0.0.0.0:8082 for Beta); applies the SELinux
bin_t exec labels; installs the systemd unit with the restorecon guard; starts
the service; and verifies /health. Re-running it propagates master updates.

```bash
# Beta  (port 8082, install /home/opc/Projects/ashatneuralhost-slave)
./scripts/seed_slave.sh opc@150.136.208.93

# Delta (bind port 8088)
./scripts/seed_slave.sh opc@129.213.147.225 /home/opc/Projects/ashatneuralhost-slave 8088
```

The router model on a peer is resolved from what is staged in its models/ dir
(preferring a Q8_0 VL-450M quant when present). Verify afterwards:

```bash
curl -s http://<peer-ip>:8082/health
curl -s http://<peer-ip>:8082/v1/models
```

## Networking: peer port must be open in TWO places

1. **OCI security list** (console) — allow TCP ingress on the peer's bind port
   from the master's public IP `129.213.94.124/32` (or `0.0.0.0/0`).
2. **Host firewalld on the peer** — the default `public` zone only allows
   `ssh`; open the port for the master's IP:

   ```bash
   ssh -i ./oraclehost_id_rsa opc@<peer-ip> \
     "sudo firewall-cmd --permanent --add-rich-rule='rule family=\"ipv4\" source address=\"129.213.94.124\" port port=<port> protocol=\"tcp\" accept' && sudo firewall-cmd --reload"
   ```

   Both Beta (8082) and Delta (8088) have the OCI security list **and** host
   firewalld rule in place already; this is only needed again for a new peer.

## Routing semantics (row_chain)

The row chain is a **weighted round-robin rotation** (smooth WRRS, nginx-
style), not a failover-only list. `RowRouter::pick()` probes every enabled
backend's `/health` **concurrently** (results cached ~5s for healthy, ~2s
for failures), then picks among the healthy ones proportionally to each
backend's `weight`:

- Current weights: `omega: 2`, `beta: 1`, `delta: 1` → omega carries twice
  the traffic of each slave (e.g. 12 requests ≈ 6 / 3 / 3).
- A backend that fails its probe (or dies mid-flight — the chat handler
  marks it unhealthy on a connection error and retries once) simply **loses
  its share** until it recovers; the other backends absorb it. If every
  backend is unhealthy the router falls back to the first enabled backend
  so the service stays up.
- Cross-server requests authenticate with the `api_key` on the backend's
  row_chain entry.

Verified 2026-08-09: 12 chats → omega 6 (lanes 18080/18081/18082), beta 3,
delta 3. Stopping delta's service removed it from the mix within seconds
(omega 8 / beta 4 / delta 0, zero 503s); it rejoined ~10s after restart.

## Delta (seeded 2026-08-09)

- Bind port: **8088** (OCI security list + firewalld open for the master IP).
- Seeded with `./scripts/seed_slave.sh opc@129.213.147.225 /home/opc/Projects/ashatneuralhost-slave 8088`.
- Legacy `delta.service` (old 230M-generation server on 8080) was retired
  (stopped/disabled) so the new slave could own the pool ports.
- Master row_chain `delta` entry points at `129.213.147.225:8088`, enabled,
  with the shared `api_key`.

## Automatic propagation

`POST /api/admin/update` (auth: `X-Ashat-Key`) runs `scripts/seed_slave.sh`
against every enabled peer in the master's `server-config.json` `update.peers`
section (Beta and Delta are both enabled). It pushes the **current** master
build — run `cargo build --release` first (or
wire the GitHub pull/build hook into it later). The endpoint is serialized and
returns a per-peer JSON report with status and the script output tail; each
peer gets `update.timeout_seconds` (default 600) and the script keeps a
`.previous` binary for rollback.

```bash
curl -X POST http://<master>:8080/api/admin/update -H "X-Ashat-Key: <key>"
```

Admin auth: the master now runs with a **dedicated admin key** — set via
`ASHAT_ADMIN_KEY` in the root-only systemd drop-in
(`/etc/systemd/system/ashat-neural-host.service.d/admin-key.conf`, mode 600;
regenerate with `openssl rand -hex 32`). The shared `ASHAT_KEY` no longer
triggers deploys (returns 401). Until a dedicated key is configured, the
endpoint falls back to the shared `ASHAT_KEY`.