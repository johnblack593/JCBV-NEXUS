# ═══════════════════════════════════════════════════════════════
# NEXUS v4.0 — Institutional Grade HFT Pipeline
# ═══════════════════════════════════════════════════════════════
# Python 3.11-slim: TensorFlow, SciPy, AsyncPG all have stable
# binary wheels for this version, avoiding C-extension compile
# failures in Docker.
# ═══════════════════════════════════════════════════════════════
FROM python:3.11-slim

WORKDIR /app

# 1. Install system-level C-extension build dependencies
#    - gcc/g++/build-essential: compile any missing wheels
#    - libpq-dev: asyncpg / psycopg2 (QuestDB PG wire)
#    - python3-dev: Python C headers
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    build-essential \
    libpq-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# 2. Install Python dependencies (upgrade pip/wheel first)
COPY nexus/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# 3. Copy source code and entrypoint v4.0
COPY main.py .
COPY nexus/ ./nexus/

# 4. Create data directories for persistence
RUN mkdir -p logs models config

# 5. Expose ports (8000=FastAPI Dashboard, 9090=Prometheus metrics)
EXPOSE 8000 9090

# 6. Launch the HFT Daemon via the CLI maestro
CMD ["python", "main.py", "run"]
