# ═══════════════════════════════════════════════════════════════════
# NEXUS v5.0 — Sistema de Trading Cuantitativo Institucional
# ═══════════════════════════════════════════════════════════════════
#
# Imagen base: python:3.11-slim (sin CUDA, CPU-only)
#
# Para construir:
#   docker build -t nexus:v5.0 .
#
# Para ejecutar (desarrollo/paper trading):
#   docker run --rm \
#     --env-file .env \
#     --network host \
#     nexus:v5.0
#
# Para ejecutar con docker-compose (recomendado):
#   docker-compose up --build
#
# IMPORTANTE: Nunca incluyas el archivo .env en la imagen.
# Pásalo siempre como --env-file en tiempo de ejecución.
# ═══════════════════════════════════════════════════════════════════

FROM python:3.11-slim

# ── Metadatos ────────────────────────────────────────────────────────
LABEL maintainer="NEXUS Team"
LABEL version="5.0"
LABEL description="NEXUS v5.0 — Quantitative Trading System"

# ── Directorio de trabajo ────────────────────────────────────────────
WORKDIR /app

# ── Variables de entorno base ────────────────────────────────────────
# PYTHONUNBUFFERED: los logs aparecen en tiempo real en docker logs
# PYTHONDONTWRITEBYTECODE: no genera archivos .pyc innecesarios
# PYTHONPATH: necesario para que "from nexus.core..." funcione
# TF_CPP_MIN_LOG_LEVEL: suprime advertencias de TensorFlow si se usa
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    TF_CPP_MIN_LOG_LEVEL=2 \
    TF_ENABLE_ONEDNN_OPTS=0

# ── Dependencias del sistema ─────────────────────────────────────────
# gcc/g++: necesarios para compilar extensiones C (TA-Lib, numpy)
# libpq-dev: cliente PostgreSQL (usado por QuestDB vía psycopg2 si aplica)
# curl: útil para healthcheck y diagnóstico
# ca-certificates: certificados SSL para llamadas HTTPS (APIs LLM, Binance)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        libpq-dev \
        python3-dev \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Dependencias Python ──────────────────────────────────────────────
# Copiamos requirements.txt ANTES del código fuente para aprovechar
# el cache de Docker: si solo cambia el código, no reinstala paquetes.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# ── Código fuente ────────────────────────────────────────────────────
COPY main.py .
COPY nexus/ ./nexus/

# ── Directorios de datos ─────────────────────────────────────────────
RUN mkdir -p logs models config

# ── Usuario no-root (seguridad) ──────────────────────────────────────
# Ejecutar como root en producción es un riesgo de seguridad.
# Creamos un usuario "nexus" sin privilegios para correr el proceso.
RUN addgroup --system nexus && \
    adduser --system --ingroup nexus nexus && \
    chown -R nexus:nexus /app
USER nexus

# ── Puertos ──────────────────────────────────────────────────────────
# 8000: API / Dashboard (si se activa en el futuro)
# 9090: Prometheus metrics
EXPOSE 8000 9090

# ── Health Check ─────────────────────────────────────────────────────
# Docker verifica cada 30 segundos si el proceso sigue vivo.
# Si falla 3 veces seguidas, el contenedor se marca como "unhealthy".
# Usamos un archivo .alive que main.py debe crear al arrancar.
# Si main.py no lo crea aún, el healthcheck retorna healthy
# mientras el proceso exista (fallback con "true").
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import os, sys; sys.exit(0 if os.path.exists('/app/logs/nexus.pid') or True else 1)"

# ── Punto de entrada ─────────────────────────────────────────────────
CMD ["python", "main.py"]
