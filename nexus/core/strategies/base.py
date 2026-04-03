"""
NEXUS v4.0 — Base Strategy (Strategy Pattern Contract)
=======================================================
Clase abstracta que define el contrato para TODA estrategia de trading
en NEXUS. Cada implementación concreta (Binary, Crypto Scalp, etc.)
DEBE implementar el método `analyze(df)`.

Patrón: Strategy Design Pattern
    - Desacopla la lógica de generación de señales del pipeline.
    - Permite al pipeline ser venue-agnostic.
    - Cada estrategia retorna un dict estandarizado con signal + management.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict

import pandas as pd  # type: ignore

logger = logging.getLogger("nexus.strategies.base")


class BaseStrategy(ABC):
    """
    Contrato base para todas las estrategias de NEXUS v4.0.

    Implementaciones concretas:
        - BinaryMLExoticStrategy   (IQ Option — ML on-the-fly)
        - CryptoQuantScalpStrategy (Binance — Mean-Reversion Z-Score)

    El Pipeline invoca SOLO `analyze(df)` y mapea el resultado
    a TradeSignal + Active Trade Management fields.

    Retorno esperado de analyze():
        {
            "signal": "BUY" | "SELL" | "HOLD",
            "confidence": float (0.0 – 1.0),
            "reason": str,
            "indicators": dict,
            # Campos opcionales de gestión activa (Binance only):
            "stop_loss": Optional[float],
            "take_profit": Optional[float],
            "trailing_stop_activation": Optional[float],
            "trailing_stop_callback_pct": Optional[float],
            "breakeven_trigger": Optional[float],
            "time_exit_minutes": Optional[int],
        }
    """

    @abstractmethod
    async def analyze(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Analiza un DataFrame OHLCV y retorna la señal + parámetros de gestión.

        Args:
            df: DataFrame con columnas mínimas: open, high, low, close, volume.
                Mínimo 30 filas para cualquier estrategia.

        Returns:
            Dict estandarizado con al menos 'signal', 'confidence', 'reason'.
        """
        ...

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}>"
