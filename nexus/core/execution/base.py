"""
NEXUS v4.0 — Abstract Execution Engine (Layer 5: Base)
======================================================
Define el contrato que TODO motor de ejecución debe cumplir.
Esto permite al Pipeline orquestar operaciones sin acoplarse
a un broker específico (IQ Option, Binance, o futuros venues).

Patrón: Strategy + Abstract Factory
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nexus.execution.base")


# ══════════════════════════════════════════════════════════════════════
#  Enums Compartidos (Venue-Agnostic)
# ══════════════════════════════════════════════════════════════════════

class VenueType(Enum):
    """Venues de ejecución soportados por NEXUS v5.0."""
    IQ_OPTION = "IQ_OPTION"
    BITGET = "BITGET"  # Phase 3: CCXT-based futures+spot venue


class SignalDirection(Enum):
    """Dirección de la señal generada por Alpha/ML."""
    CALL = "CALL"   # IQ Option terminology
    PUT = "PUT"     # IQ Option terminology
    BUY = "BUY"     # Binance Spot/Perps terminology
    SELL = "SELL"   # Binance Spot/Perps terminology


class ExecutionStatus(Enum):
    """Estado unificado de una orden ejecutada."""
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"


# ══════════════════════════════════════════════════════════════════════
#  Data Classes (Venue-Agnostic Order Lifecycle)
# ══════════════════════════════════════════════════════════════════════

@dataclass
class TradeSignal:
    """
    Señal validada que llega desde la Capa 3 (Alpha) + Capa 4 (Risk).
    Es el input universal para cualquier Execution Engine.
    """
    asset: str
    direction: SignalDirection
    size: float                         # $ amount (IQ) or quantity (Binance)
    confidence: float                   # 0.0 - 1.0 from ML/Alpha
    regime: str = "GREEN"               # MACRO_REGIME from Redis
    expiration_minutes: int = 1         # Only for IQ Option binary/turbo
    order_type: str = "MARKET"          # MARKET | LIMIT (Binance only)
    limit_price: Optional[float] = None # Binance limit orders
    stop_loss: Optional[float] = None   # Binance SL
    take_profit: Optional[float] = None # Binance TP
    # ── Active Trade Management (Institutional) ──────────────────
    trailing_stop_activation: Optional[float] = None   # Price where trailing stop activates
    trailing_stop_callback_pct: Optional[float] = None # Callback % for trailing stop (e.g. 0.5)
    breakeven_trigger: Optional[float] = None          # Price to move SL to entry
    time_exit_minutes: Optional[int] = None            # Max minutes to hold position
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TradeResult:
    """
    Resultado unificado post-ejecución. Ambos venues retornan esto.
    Consumido por: Observabilidad (Prometheus), Telegram, Tear Sheet.
    """
    order_id: str
    venue: VenueType
    asset: str
    direction: SignalDirection
    status: ExecutionStatus
    size: float
    executed_price: float
    payout: float = 0.0                 # IQ Option payout % (irrelevant for Binance)
    commission: float = 0.0             # Binance commission
    latency_ms: float = 0.0            # Signal -> Fill latency
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_response: Optional[Dict[str, Any]] = None


# ══════════════════════════════════════════════════════════════════════
#  Abstract Execution Engine
# ══════════════════════════════════════════════════════════════════════

class AbstractExecutionEngine(ABC):
    """
    Contrato base para todo motor de ejecución en NEXUS v4.0.
    
    Implementaciones concretas:
        - IQOptionExecutionEngine  (binary/turbo options)
        - BinanceExecutionEngine   (spot/perps DMA)
    
    El Pipeline (pipeline.py) invoca SOLO estos métodos,
    jamás se acopla a la lógica interna del broker.
    """

    @property
    @abstractmethod
    def venue(self) -> VenueType:
        """Retorna el VenueType de esta implementación."""
        ...

    @abstractmethod
    async def connect(self) -> bool:
        """Establece conexión asíncrona con el broker/exchange."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Cierra conexión limpiamente."""
        ...

    @abstractmethod
    async def get_balance(self) -> float:
        """Retorna el balance disponible en USD/USDT."""
        ...

    @abstractmethod
    async def execute(self, signal: TradeSignal) -> TradeResult:
        """
        Ejecuta una señal de trading.
        Este es EL método central. Recibe una TradeSignal validada
        por Risk Management y retorna un TradeResult.
        """
        ...

    @abstractmethod
    async def get_payout(self, asset: str) -> float:
        """
        Retorna el payout/spread actual del activo.
        - IQ Option: % de payout (ej: 85.0)
        - Binance: Spread en bps (ej: 0.5)
        """
        ...

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """True si la conexión WS/REST está activa."""
        ...

    def __repr__(self) -> str:
        status = "CONNECTED" if self.is_connected else "DISCONNECTED"
        return f"<{self.__class__.__name__} venue={self.venue.value} status={status}>"
