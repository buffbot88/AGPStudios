FROM python:3.11

# System packages needed by llama-server install path + runtime:
#   curl/wget     -> installer.py GitHub/HF mirror fetches
#   git           -> llama-server source-build fallback
#   libgomp1      -> openmp runtime for llama-server CPU inference
#   libstdc++6    -> gcc runtime (some llama-server binaries link against it)
#   ca-certificates -> TLS for HF Hub + GitHub release downloads
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl wget git ca-certificates libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# HF Spaces default port (must keep in sync with `app_port: 7860` in README frontmatter).
EXPOSE 7860

WORKDIR /app

# Install Python deps before the COPY so the dependency layer is cached
# when only app.py changes (most pushes).
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -U pip && \
    pip install --no-cache-dir -r /app/requirements.txt

# Application source. Only app.py + dashboard.py + the supporting
# modules listed below; no .git, no tests, no editor config.
COPY app.py /app/app.py
COPY dashboard.py /app/dashboard.py
COPY public_snapshot.py /app/public_snapshot.py
COPY telemetry.py /app/telemetry.py
COPY metrics_store.py /app/metrics_store.py
COPY run_metrics.py /app/run_metrics.py
COPY run_errors.py /app/run_errors.py
COPY response_adapter.py /app/response_adapter.py
COPY domain.py /app/domain.py
COPY backend_launcher.py /app/backend_launcher.py
COPY completion_client.py /app/completion_client.py
COPY installer.py /app/installer.py
COPY install_strategies.py /app/install_strategies.py
COPY lane_resolver.py /app/lane_resolver.py

# Single-process FastAPI serving. No second uvicorn (no bind race), no
# Gradio runtime (no Login auth shim). HF Spaces with `sdk: docker`
# runs this CMD and proxies 7860 to the public hostname.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860", \
     "--workers", "1", "--log-level", "info"]
