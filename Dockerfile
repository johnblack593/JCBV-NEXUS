# ═══════════════════════════════════════════════════════════════════
# NEXUS v4.0 — Institutional Grade HFT Pipeline (CPU-Only)
# ═══════════════════════════════════════════════════════════════════
# Optimizations:
#   - Python 3.11-slim: stable wheels for TF, SciPy, AsyncPG
#   - tensorflow-cpu: no NVIDIA/CUDA bloat (~400MB saved)
#   - No PyTorch/SB3: optional RL not needed for production (~530MB saved)
#   - Multi-stage thinking: build deps → install → clean
# ═══════════════════════════════════════════════════════════════════
FROM python:3.11-slim

WORKDIR /app

# 1. Install ONLY essential system deps (gcc for C-extensions, git for pip+github)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        libpq-dev \
        python3-dev \
        git \
    && rm -rf /var/lib/apt/lists/*

# 2. Upgrade pip, install Python deps (CPU-only, no NVIDIA)
COPY nexus/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# 3. Copy source code and v4.0 entrypoint
COPY main.py .
COPY nexus/ ./nexus/

# 5. Create data directories
RUN mkdir -p logs models config

# 6. Suppress TensorFlow noise (no GPU available)
ENV TF_CPP_MIN_LOG_LEVEL=2
ENV TF_ENABLE_ONEDNN_OPTS=0
ENV PYTHONUNBUFFERED=1

# 7. Expose ports (8000=FastAPI OCP Dashboard, 9090=Prometheus)
EXPOSE 8000 9090

# 8. Launch HFT Daemon + Dashboard via CLI maestro
CMD ["python", "main.py", "run"]
