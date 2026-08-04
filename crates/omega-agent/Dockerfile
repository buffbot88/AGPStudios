FROM python:3.11

# System packages needed by the local llama-server runtime:
#   libgomp1      -> OpenMP runtime for CPU inference
#   libstdc++6    -> C++ runtime for llama-server
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 8000

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
COPY lane_resolver.py /app/lane_resolver.py

# Fallback container only. Production uses systemd and externally installed
# llama-server/model assets; this image does not vendor either runtime asset.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--log-level", "info"]
