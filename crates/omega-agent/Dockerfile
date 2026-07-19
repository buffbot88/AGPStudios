FROM python:3.11-slim

# System packages needed by llama-server install path (curl + ca-certs
# for the GitHub release + HF Hub mirror downloads installer.py makes).
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# HF Spaces default port. Exposed explicitly so the docker runner can
# surface the actual port being served.
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
COPY lane_keygate.py /app/lane_keygate.py
COPY lane_resolver.py /app/lane_resolver.py

# Single-process FastAPI serving. No second uvicorn (no bind race), no
# Gradio runtime (no Login auth shim). HF Spaces with `sdk: docker`
# runs this ENTRYPOINT and proxies 7860 to the public hostname.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860", \
     "--workers", "1", "--log-level", "info"]
