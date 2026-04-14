# NEXUS v5.0 — Strategy Layer (Strategy Pattern Factory)
# BaseStrategy + Venue-specific implementations
from .base import BaseStrategy
from .binary_ml_exotic import BinaryMLExoticStrategy
from .bitget_trend_scalper import BitgetTrendScalperStrategy

__all__ = [
    "BaseStrategy",
    "BinaryMLExoticStrategy",
    "BitgetTrendScalperStrategy",
]
