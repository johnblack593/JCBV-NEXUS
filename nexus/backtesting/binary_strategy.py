import logging
import math
from datetime import datetime
from typing import Any, Dict, List

try:
    import backtrader as bt  # type: ignore
except ImportError:
    bt = None

from core.signal_engine import TechnicalSignalEngine  # type: ignore
from core.risk_manager import QuantRiskManager  # type: ignore
from agents.agent_bull import AgentBull  # type: ignore
from agents.agent_bear import AgentBear  # type: ignore
from agents.agent_arbitro import AgentArbitro  # type: ignore

logger = logging.getLogger("nexus.binary_strategy")


class NexusBinaryStrategy(bt.Strategy):
    """
    Estrategia NEXUS especializada para Opciones Binarias (HFT).
    
    A diferencia de la operativa en Spot/Futuros, en opciones binarias:
    - El riesgo es fijo (Fixed Stake)
    - La expiración es rígida por tiempo (N barras/minutos)
    - El pago es binario (+85% OTM / -100% ITM)
    
    Salteamos el broker nativo de Backtrader para evitar cálculos 
    flotantes de margen que no aplican a Opciones Binarias, simulando
    el libro contable internamente en `self.binary_equity`.
    """

    params = dict(
        expiry_bars=3,           # Expiración: 3 minutos (asumiendo data 1m)
        payout_pct=0.85,         # Payout de IQ Option / Broker: 85%
        stake_pct=1.0,           # Porcentaje del capital a arriesgar por trade (1%)
        max_positions=3,         # Máximo de operaciones concurrentes
        min_confidence=0.70,     # Confianza requerida del LLM (más estricta para Binarias)
        lookback=20,             # Barras mínimas requeridas
    )

    def __init__(self):
        # Componentes NEXUS
        self.signal_engine = TechnicalSignalEngine()
        self.bull = AgentBull()
        self.bear = AgentBear()
        self.arbitro = AgentArbitro()
        self.arbitro.initialize()
        
        # Libro Contable Interno para Binarias
        self.metrics_initialized = False
        self.binary_equity = 10000.0  # Se sobreescribirá en next() la primera vez
        
        # Estado 
        self._active_options: List[Dict[str, Any]] = []
        self._trade_log: List[Dict[str, Any]] = []
        self._equity_curve: List[float] = []
        self._dates: List[datetime] = []

    def next(self):
        # 1. Inicializar equity en la barra 0
        if not self.metrics_initialized:
            self.binary_equity = self.broker.getcash()
            self.metrics_initialized = True

        # 2. Registrar equity para gráficas de reporte (antes del tick actual)
        self._equity_curve.append(self.binary_equity)
        self._dates.append(self.data.datetime.datetime(0))

        # 3. Evaluar Expiración de las Opciones Activas
        self._evaluate_expirations()

        if len(self.data) < self.p.lookback:
            return

        # 4. Generar OHLCV y Señales Técnicas
        df = self._build_ohlcv_df()
        if df is None or df.empty:
            return

        try:
            technical = self.signal_engine.generate_signal(df)
        except Exception:
            return

        current_price = self.data.close[0]

        # 5. Agentes elaboran sus argumentos (Micro-Estructura)
        self.bull.build_argument(current_price, technical)
        self.bear.build_argument(current_price, technical)
        
        bull_state = self.bull.get_state()
        bear_state = self.bear.get_state()
        
        bull_strength = bull_state.get("strength", 0.0)
        bear_strength = bear_state.get("strength", 0.0)
        max_strength = max(bull_strength, bear_strength)
        
        # EVENT-DRIVEN GATING: En TF chicos (1m) el 95% del tiempo es ruido.
        if max_strength < 6.5:
            return  # Silencio

        if len(self._active_options) >= self.p.max_positions:
            return  # Limite máximo de trades concurrentes alcanzado

        # Inyectamos el Timestamp
        dt_str = self.data.datetime.datetime(0).isoformat()
        market_ctx = {
            "symbol": "BINARY_HFT", 
            "price": current_price, 
            "timestamp": dt_str,
            "binary_context": f"Opcion Binaria a {self.p.expiry_bars} min"
        }
        
        # 6. Deliberación LLM
        decision = self.arbitro.deliberate(
            bull_state=bull_state,
            bear_state=bear_state,
            risk_metrics={"max_drawdown": 0, "current_exposure": 0},
            market_context=market_ctx
        )

        confidence = decision.get("confidence", 0)
        action = decision.get("decision", "HOLD")

        if confidence >= self.p.min_confidence:
            if action in ("BUY", "SELL"):
                stake = self.binary_equity * (self.p.stake_pct / 100.0)
                if stake < 1.0:
                    stake = 1.0  # Mínimo de broker usualmente $1
                
                # Descontar el stake inmediatamente (Riesgo Fijo)
                if self.binary_equity >= stake:
                    self.binary_equity -= stake
                    self._active_options.append({
                        "entry_bar": len(self.data),
                        "entry_time": self.data.datetime.datetime(0),
                        "action": action,
                        "entry_price": current_price,
                        "stake": stake,
                        "payout_pct": self.p.payout_pct,
                        "expiry_bars": self.p.expiry_bars
                    })
                    logger.debug(f"[BINARIAS] Abrimos {action} a {current_price} | Stake: ${stake:.2f}")

    def _evaluate_expirations(self):
        """Revisa la cola de opciones activas y liquida las caducadas."""
        current_bar = len(self.data)
        current_price = self.data.close[0]
        
        surviving_options = []

        for opt in self._active_options:
            bars_elapsed = current_bar - opt["entry_bar"]
            
            if bars_elapsed >= opt["expiry_bars"]:
                # Expiración alcanzada. Evaluación ITM vs OTM.
                itm = False
                if opt["action"] == "BUY" and current_price > opt["entry_price"]:
                    itm = True
                elif opt["action"] == "SELL" and current_price < opt["entry_price"]:
                    itm = True
                
                if itm:
                    # Win (ITM) -> Devuelve Stake + Profit
                    profit = opt["stake"] * opt["payout_pct"]
                    return_amount = opt["stake"] + profit
                    self.binary_equity += return_amount
                    res_str = f"WIN (+${profit:.2f})"
                else:
                    # Loss (OTM) -> Pierde el stake por defecto (ya deducido)
                    profit = -opt["stake"]
                    res_str = f"LOSS (-${opt['stake']:.2f})"
                
                # Logear para el dashboard
                self._trade_log.append({
                    "datetime": self.data.datetime.datetime(0),
                    "action": opt["action"],
                    "price": opt["entry_price"], # Entry
                    "close_price": current_price, # Exit
                    "size": opt["stake"],
                    "confidence": 0.85, # Constante para binarias logs
                    "sl": 0, "tp": 0,
                    "pnl": profit,
                    "result": "WIN" if itm else "LOSS"
                })
                logger.debug(f"[BINARIAS] Expiracion de {opt['action']} -> {res_str}. Equidad: ${self.binary_equity:.2f}")
            else:
                surviving_options.append(opt)
                
        self._active_options = surviving_options

    def _build_ohlcv_df(self):
        import pandas as pd  # type: ignore
        n = min(len(self.data), self.p.lookback)
        data = {
            "open": [self.data.open[-i] for i in range(n - 1, -1, -1)],
            "high": [self.data.high[-i] for i in range(n - 1, -1, -1)],
            "low": [self.data.low[-i] for i in range(n - 1, -1, -1)],
            "close": [self.data.close[-i] for i in range(n - 1, -1, -1)],
            "volume": [self.data.volume[-i] for i in range(n - 1, -1, -1)],
        }
        return pd.DataFrame(data)
