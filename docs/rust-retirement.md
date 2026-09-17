# Rust/AshatHub retirement

The Rust AshatHub gateway and Galileo surface were retired after the Python API cutover.
The source is preserved under `archive/retired-rust/ashat-hub/` for rollback and history.
Production `/api/*` traffic is served exclusively by `services/python-api` on port 3110.
Rust and Galileo systemd/Apache deployment configuration is removed.
