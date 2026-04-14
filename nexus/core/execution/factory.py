"""
NEXUS v4.0 — Execution Engine Factory
======================================
Singleton Factory que instancia el motor de ejecución correcto
según la variable de entorno EXECUTION_VENUE.

Uso:
    engine = get_execution_engine()  # Lee EXECUTION_VENUE del .env
    await engine.connect()
    result = await engine.execute(signal)
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from dotenv import load_dotenv

from .base import AbstractExecutionEngine, VenueType

logger = logging.getLogger("nexus.execution.factory")

# Singleton cache
_engine_instance: Optional[AbstractExecutionEngine] = None


def get_execution_engine(force_venue: Optional[str] = None) -> AbstractExecutionEngine:
    """
    Factory que retorna la implementación correcta del motor de ejecución.
    
    Args:
        force_venue: Override manual ("IQ_OPTION", "BITGET"). 
                     Si es None, lee de EXECUTION_VENUE en .env.
    
    Returns:
        Instancia singleton de AbstractExecutionEngine.
    
    Raises:
        ValueError si el venue no es reconocido.
    """
    global _engine_instance

    if _engine_instance is not None and force_venue is None:
        return _engine_instance

    load_dotenv()
    venue_str = (force_venue or os.getenv("EXECUTION_VENUE", "IQ_OPTION")).upper()

    if venue_str == VenueType.IQ_OPTION.value:
        from .iqoption_engine import IQOptionExecutionEngine
        _engine_instance = IQOptionExecutionEngine()
        logger.info("🏭 Factory → IQOptionExecutionEngine instanciado")

    elif venue_str == VenueType.BITGET.value:
        from .bitget_engine import BitgetExecutionEngine
        _engine_instance = BitgetExecutionEngine()
        logger.info("🏭 Factory → BitgetExecutionEngine instanciado")

    else:
        raise ValueError(
            f"EXECUTION_VENUE='{venue_str}' no reconocido. "
            f"Valores válidos: IQ_OPTION, BITGET"
        )

    return _engine_instance


def reset_engine() -> None:
    """Resetea el singleton (para tests o cambio de venue en caliente)."""
    global _engine_instance
    _engine_instance = None
    logger.info("🏭 Factory → Engine singleton reseteado")
