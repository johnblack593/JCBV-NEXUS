FROM python:3.13-slim

WORKDIR /app

# Instalar dependencias del sistema
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

# Copiar requirements e instalar dependencias Python
COPY nexus/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código fuente
COPY nexus/ ./nexus/
COPY .env .env

# Crear directorios de datos
RUN mkdir -p logs models config

# Exponer puerto de métricas Prometheus
EXPOSE 8000

# Ejecutar el HFT Daemon
CMD ["python", "-m", "nexus.iq_main"]
