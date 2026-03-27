# pyre-unsafe
"""
NEXUS Trading System — Configuración Global
=============================================
API keys, parámetros de riesgo, timeframes y constantes del sistema.
"""

from __future__ import annotations

import os
from dotenv import load_dotenv  # pyre-ignore[21]
load_dotenv()  # Carga el archivo .env automáticamente
from dataclasses import dataclass, field
from typing import List


# ──────────────────────────────────────────────
#  API Keys (Carga dinámica Multi-Cuenta Fallback)
# ──────────────────────────────────────────────

def _load_api_keys(prefix: str) -> List[str]:
    keys = []
    # 1. Usar plural si existe (e.g. GROQ_API_KEYS=key1,key2,key3)
    plural_env = os.getenv(f"{prefix}S", "")
    if plural_env:
        keys.extend([k.strip() for k in plural_env.split(",") if k.strip()])
    
    # 2. Añadir el default
    default_key = os.getenv(prefix, "")
    if default_key and default_key not in keys:
        keys.append(default_key)
        
    # 3. Añadir los numerados _1, _2, _3...
    for i in range(1, 20):
        k = os.getenv(f"{prefix}_{i}", "")
        if k and k not in keys:
            keys.append(k)
            
    return keys

OPENAI_API_KEYS: List[str] = _load_api_keys("OPENAI_API_KEY")
GROQ_API_KEYS: List[str] = _load_api_keys("GROQ_API_KEY")
GOOGLE_API_KEYS: List[str] = _load_api_keys("GOOGLE_API_KEY")

# Legacy strings fallback
OPENAI_API_KEY: str = OPENAI_API_KEYS[0] if OPENAI_API_KEYS else ""
GROQ_API_KEY: str = GROQ_API_KEYS[0] if GROQ_API_KEYS else ""
GOOGLE_API_KEY: str = GOOGLE_API_KEYS[0] if GOOGLE_API_KEYS else ""


# ──────────────────────────────────────────────
#  Parámetros de Trading
# ──────────────────────────────────────────────

@dataclass
class TradingConfig:
    """Parámetros generales de trading."""

    symbols: List[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    timeframes: List[str] = field(default_factory=lambda: ["1m", "5m", "15m", "1h", "4h"])
    base_currency: str = "USDT"
    default_leverage: int = 1


# ──────────────────────────────────────────────
#  Parámetros de Riesgo
# ──────────────────────────────────────────────

@dataclass
class RiskConfig:
    """Parámetros del gestor de riesgo."""

    max_portfolio_risk: float = 0.02        # 2 % del portafolio
    max_position_size: float = 0.10         # 10 % del portafolio por posición
    max_drawdown: float = 0.15              # 15 % drawdown máximo
    kelly_fraction: float = 0.25            # Fracción de Kelly conservadora
    monte_carlo_simulations: int = 10_000
    var_confidence: float = 0.95            # VaR al 95 %
    stop_loss_pct: float = 0.03             # Stop-loss 3 %
    take_profit_pct: float = 0.06           # Take-profit 6 %


# ──────────────────────────────────────────────
#  Parámetros de ML
# ──────────────────────────────────────────────

@dataclass
class MLConfig:
    """Parámetros para los modelos de Machine Learning."""

    lstm_lookback: int = 60
    lstm_epochs: int = 50
    lstm_batch_size: int = 32
    dqn_total_timesteps: int = 100_000
    retrain_interval_hours: int = 24


# ──────────────────────────────────────────────
#  Parámetros de Sentimiento
# ──────────────────────────────────────────────

@dataclass
class SentimentConfig:
    """Parámetros para el motor de sentimiento."""

    news_sources: List[str] = field(default_factory=lambda: [
        "https://cryptopanic.com/api/v1/posts/",
    ])
    on_chain_provider: str = "glassnode"
    sentiment_weight: float = 0.20


# ──────────────────────────────────────────────
#  Parámetros de Backtesting
# ──────────────────────────────────────────────

@dataclass
class BacktestConfig:
    """Parámetros para el módulo de backtesting."""

    initial_capital: float = 10_000.0
    commission_pct: float = 0.001           # 0.1 % comisión
    slippage_pct: float = 0.0005            # 0.05 % deslizamiento


# ──────────────────────────────────────────────
#  Instancias por defecto
# ──────────────────────────────────────────────

trading_config  = TradingConfig()
risk_config     = RiskConfig()
ml_config       = MLConfig()
sentiment_config = SentimentConfig()
backtest_config  = BacktestConfig()
