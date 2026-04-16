"""
NexusIncidentLogger — NEXUS v5.0
=================================
Handler de logging especializado que captura SOLO eventos anómalos
con contexto automático enriquecido. Genera un archivo JSON Lines separado
del log principal para diagnóstico inmediato post-mortem.

Archivo de salida: logs/nexus_incidents_YYYYMMDD.log
Rotación: diaria, 7 días de retención.

Cada línea del archivo es un JSON independiente con:
  - Timestamp, nivel, módulo
  - Contexto del pipeline (ciclo, activo, step)
  - Mensaje y traceback completo
  - Últimas N líneas de log previas (ring buffer)
  - Estado del consensus/macro al momento del incidente

Criterios de captura:
  - ERROR y CRITICAL: siempre
  - WARNING: solo si contiene keyword de la lista de alertas
"""

from __future__ import annotations

import json
import logging
import os
import re
import traceback as tb_module
from collections import deque
from datetime import datetime, timezone, timedelta
from logging.handlers import TimedRotatingFileHandler
from threading import Lock
from typing import Any, Dict, Optional


# ══════════════════════════════════════════════════════════════════════
#  Constants
# ══════════════════════════════════════════════════════════════════════

_TZ_GMT5 = timezone(timedelta(hours=-5))

# Keywords que elevan un WARNING a incidente capturado
WARNING_KEYWORDS: list[str] = [
    "suspended",
    "ATR LEAK",
    "fallback",
    "REVERTIR",
    "timeout",
    "WinError",
]

# Cuantas líneas previas del log guardar como contexto
PREV_LOG_BUFFER_SIZE = 20


# ══════════════════════════════════════════════════════════════════════
#  Ring Buffer — captura las últimas N líneas de log
# ══════════════════════════════════════════════════════════════════════

class _LogRingBuffer(logging.Handler):
    """
    Handler invisible que mantiene un ring buffer de las últimas N líneas
    de log formateadas. No escribe a disco — solo retiene en memoria.
    Se usa para inyectar `prev_log_lines` en cada incidente.
    """

    def __init__(self, capacity: int = PREV_LOG_BUFFER_SIZE):
        super().__init__(level=logging.DEBUG)
        self._buffer: deque[str] = deque(maxlen=capacity)
        self._lock = Lock()
        # Formatter compacto para las líneas de contexto
        self.setFormatter(logging.Formatter(
            "%(asctime)s │ %(name)-20s │ %(levelname)-7s │ %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            with self._lock:
                self._buffer.append(line)
        except Exception:
            pass  # Never crash the logging pipeline

    def get_recent(self, n: int = 4) -> list[str]:
        """Retorna las últimas N líneas (sin la línea actual del incidente)."""
        with self._lock:
            items = list(self._buffer)
        # Retornar las últimas n, excluyendo la última (que es el incidente mismo)
        if len(items) > 1:
            return items[-(n + 1):-1]
        return items[-n:]


# ══════════════════════════════════════════════════════════════════════
#  Incident Handler
# ══════════════════════════════════════════════════════════════════════

class NexusIncidentHandler(logging.Handler):
    """
    Logging handler que escribe incidentes como JSON Lines a un archivo
    diario con rotación automática de 7 días.

    Solo captura:
      - ERROR y CRITICAL: siempre
      - WARNING: solo si el mensaje contiene un keyword de alerta

    Cada incidente incluye contexto enriquecido automáticamente desde
    el LogRecord.extra (inyectado via LoggerAdapter en el pipeline).
    """

    def __init__(self, log_dir: str = "logs", backup_count: int = 7):
        super().__init__(level=logging.WARNING)
        self._log_dir = log_dir
        self._backup_count = backup_count
        self._ring_buffer: Optional[_LogRingBuffer] = None
        self._file_handler: Optional[TimedRotatingFileHandler] = None
        self._write_lock = Lock()

        # Compilar regex para keywords de WARNING
        escaped = [re.escape(kw) for kw in WARNING_KEYWORDS]
        self._warning_pattern = re.compile(
            "|".join(escaped), re.IGNORECASE
        )

        self._setup_file_handler()

    def _setup_file_handler(self) -> None:
        """Configura el TimedRotatingFileHandler con rotación diaria."""
        os.makedirs(self._log_dir, exist_ok=True)

        # El nombre base incluye la fecha — TimedRotatingFileHandler
        # rotará automáticamente a medianoche
        log_path = os.path.join(self._log_dir, "nexus_incidents.log")

        self._file_handler = TimedRotatingFileHandler(
            filename=log_path,
            when="midnight",
            interval=1,
            backupCount=self._backup_count,
            encoding="utf-8",
            utc=False,  # Usar hora local (GMT-5)
        )
        # Suffix para archivos rotados: nexus_incidents.log.20260416
        self._file_handler.suffix = "%Y%m%d"
        # No necesitamos formatter — escribimos JSON crudo
        self._file_handler.setFormatter(logging.Formatter("%(message)s"))

    def set_ring_buffer(self, ring: _LogRingBuffer) -> None:
        """Conecta el ring buffer para capturar líneas previas."""
        self._ring_buffer = ring

    def _should_capture(self, record: logging.LogRecord) -> bool:
        """Determina si este log record debe generar un incidente."""
        # ERROR y CRITICAL: siempre capturar
        if record.levelno >= logging.ERROR:
            return True

        # WARNING: solo si contiene keyword de alerta
        if record.levelno == logging.WARNING:
            msg = record.getMessage()
            if self._warning_pattern.search(msg):
                return True

        return False

    def emit(self, record: logging.LogRecord) -> None:
        """Genera y escribe el incidente JSON si cumple los criterios."""
        if not self._should_capture(record):
            return

        try:
            incident = self._build_incident(record)
            json_line = json.dumps(incident, ensure_ascii=False, default=str)

            with self._write_lock:
                if self._file_handler:
                    # Crear un LogRecord dummy para el file handler
                    dummy = logging.LogRecord(
                        name="incident",
                        level=record.levelno,
                        pathname="",
                        lineno=0,
                        msg=json_line,
                        args=None,
                        exc_info=None,
                    )
                    self._file_handler.emit(dummy)
        except Exception:
            # NUNCA crashear el sistema de logging
            self.handleError(record)

    def _build_incident(self, record: logging.LogRecord) -> Dict[str, Any]:
        """Construye el dict del incidente con contexto enriquecido."""
        now = datetime.now(_TZ_GMT5)

        # Extraer traceback si existe
        tb_str = None
        if record.exc_info and record.exc_info[2]:
            tb_str = "".join(tb_module.format_exception(*record.exc_info))
        elif record.exc_text:
            tb_str = record.exc_text

        # Extraer contexto del pipeline (inyectado via LoggerAdapter extra)
        cycle = getattr(record, "cycle", None)
        asset = getattr(record, "asset", None)
        step = getattr(record, "step", None)

        # Extraer contexto adicional
        last_consensus = getattr(record, "last_consensus", None)
        macro_regime = getattr(record, "macro_regime", None)

        # Líneas previas del ring buffer
        prev_lines: list[str] = []
        if self._ring_buffer:
            prev_lines = self._ring_buffer.get_recent(4)

        # Construir contexto
        context: Dict[str, Any] = {}
        if last_consensus:
            context["last_consensus"] = last_consensus
        if macro_regime:
            context["macro_regime"] = macro_regime
        if prev_lines:
            context["prev_log_lines"] = prev_lines

        incident: Dict[str, Any] = {
            "ts": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "module": record.name,
        }

        # Campos opcionales del pipeline — solo incluir si existen
        if cycle is not None:
            incident["cycle"] = cycle
        if asset:
            incident["asset"] = asset
        if step:
            incident["step"] = step

        incident["message"] = record.getMessage()

        if tb_str:
            incident["traceback"] = tb_str

        if context:
            incident["context"] = context

        return incident

    def close(self) -> None:
        """Cierra el handler de archivo subyacente."""
        if self._file_handler:
            self._file_handler.close()
        super().close()


# ══════════════════════════════════════════════════════════════════════
#  Pipeline Context Adapter
# ══════════════════════════════════════════════════════════════════════

class PipelineContextAdapter(logging.LoggerAdapter):
    """
    LoggerAdapter que inyecta contexto de pipeline (cycle, asset, step)
    en cada LogRecord. El NexusIncidentHandler los extrae automáticamente.

    Uso en pipeline._tick():
        self._log = PipelineContextAdapter(logger, {
            "cycle": self._cycle_count,
            "asset": active_symbol,
            "step": "Step 5: Execution",
        })
        self._log.info("Executing trade...")  # El extra viaja con el record
    """

    def process(self, msg, kwargs):
        # Merge our extra into the record's extra dict
        extra = kwargs.get("extra", {})
        extra.update(self.extra)
        kwargs["extra"] = extra
        return msg, kwargs


# ══════════════════════════════════════════════════════════════════════
#  Factory — wiring function
# ══════════════════════════════════════════════════════════════════════

_incident_handler: Optional[NexusIncidentHandler] = None
_ring_buffer: Optional[_LogRingBuffer] = None


def install_incident_logger(log_dir: str = "logs") -> NexusIncidentHandler:
    """
    Instala el NexusIncidentLogger como handler adicional en el logger
    raíz de 'nexus'. NO reemplaza handlers existentes.

    Debe llamarse DESPUÉS de setup_logging() en main.py.

    Returns:
        La instancia del handler (para testing o referencia).
    """
    global _incident_handler, _ring_buffer

    if _incident_handler is not None:
        return _incident_handler  # Ya instalado (idempotente)

    # 1. Crear ring buffer y registrarlo en el logger raíz de nexus
    _ring_buffer = _LogRingBuffer(capacity=PREV_LOG_BUFFER_SIZE)
    nexus_logger = logging.getLogger("nexus")
    nexus_logger.addHandler(_ring_buffer)

    # 2. Crear incident handler y conectar el ring buffer
    _incident_handler = NexusIncidentHandler(log_dir=log_dir)
    _incident_handler.set_ring_buffer(_ring_buffer)

    # 3. Agregar al logger raíz de nexus (captura TODOS los sub-loggers)
    nexus_logger.addHandler(_incident_handler)

    logging.getLogger("nexus.infrastructure").info(
        f"🛡️ NexusIncidentLogger instalado → {log_dir}/nexus_incidents.log "
        f"(rotación diaria, retención 7 días)"
    )

    return _incident_handler


def get_ring_buffer() -> Optional[_LogRingBuffer]:
    """Accede al ring buffer global (para testing)."""
    return _ring_buffer
