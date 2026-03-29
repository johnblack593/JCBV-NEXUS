"""
NEXUS Trading System — Agent Bull (Agente Alcista)
====================================================
Agente especializado en construir el argumento alcista a partir de
datos técnicos, precio actual y métricas on-chain.

Retorna: {"stance": "BULL", "argument": str, "strength": 0-10}
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nexus.agent_bull")


class AgentBull:
    """
    Agente alcista: construye el mejor argumento posible a favor
    de una posición larga (BUY).

    Inputs:
        - precio actual
        - señal técnica (del TechnicalSignalEngine)
        - datos on-chain / sentimiento

    Selecciona los datos más favorables para construir su caso.
    """

    # Pesos para cada fuente de datos en el cálculo de strength
    _WEIGHTS = {
        "technical": 0.35,
        "on_chain": 0.20,
        "price_action": 0.25,
        "microstructure": 0.20,
    }

    def __init__(self) -> None:
        self._last_output: Optional[Dict[str, Any]] = None
        self._active = True

    # ══════════════════════════════════════════════════════════════════
    #  Método principal (API pública)
    # ══════════════════════════════════════════════════════════════════

    def build_argument(
        self,
        price: float,
        signal: Dict[str, Any],
        onchain_data: Optional[Dict[str, Any]] = None,
        alt_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Construye el argumento alcista.

        Args:
            price:        Precio actual del activo.
            signal:       Output de TechnicalSignalEngine.generate_signal()
            onchain_data: Métricas on-chain / sentimiento (opcional).
            alt_data:     Datos alternativos (OrderBook y Funding).

        Returns:
            {"stance": "BULL", "argument": str, "strength": int 0-10}
        """
        result = self.analyze(price, signal, onchain_data, alt_data)
        result["strength"] = int(result["strength"])
        return result


    def analyze(
        self,
        current_price: float,
        technical_signal: Dict[str, Any],
        on_chain_data: Optional[Dict[str, Any]] = None,
        alt_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Construye el argumento alcista.

        Args:
            current_price:    Precio actual del activo.
            technical_signal: Output de TechnicalSignalEngine.generate_signal()
            on_chain_data:    Métricas on-chain / sentimiento (opcional).
            alt_data:         Microestructura y Funding Rate.

        Returns:
            {"stance": "BULL", "argument": str, "strength": 0-10}
        """
        on_chain = on_chain_data or {}
        micro_data = alt_data or {}

        # 1. Extraer puntos alcistas de la señal técnica
        tech_points, tech_score = self._evaluate_technical(technical_signal)

        # 2. Extraer puntos alcistas de on-chain
        chain_points, chain_score = self._evaluate_on_chain(on_chain)

        # 3. Evaluar acción del precio
        price_points, price_score = self._evaluate_price_action(
            current_price, technical_signal
        )
        
        # 3.5. Microestructura (L1) & Funding
        micro_points, micro_score = self._evaluate_microstructure(micro_data)

        # 4. Calcular strength (0–10)
        raw_strength = (
            tech_score * self._WEIGHTS["technical"]
            + chain_score * self._WEIGHTS["on_chain"]
            + price_score * self._WEIGHTS["price_action"]
            + micro_score * self._WEIGHTS["microstructure"]
        )
        strength = float(f"{max(0.0, min(10.0, raw_strength)):.1f}")

        # 5. Construir argumento narrativo
        all_points = tech_points + chain_points + price_points + micro_points
        argument = self._build_argument(current_price, all_points, strength)

        self._last_output = {
            "stance": "BULL",
            "argument": argument,
            "strength": round(strength),
        }

        logger.info("🐂 AgentBull: strength=%.1f | %d puntos alcistas", strength, len(all_points))
        return self._last_output  # type: ignore

    # ══════════════════════════════════════════════════════════════════
    #  Evaluadores de fuentes de datos
    # ══════════════════════════════════════════════════════════════════

    def _evaluate_technical(
        self, signal: Dict[str, Any]
    ) -> tuple[List[str], float]:
        """Extrae puntos alcistas de la señal técnica."""
        points: List[str] = []
        score = 5.0  # neutral base

        indicators = signal.get("indicators", {})
        signal_dir = signal.get("signal", "HOLD")
        confidence = signal.get("confidence", 0.0)

        # Si la señal global es BUY, fuerte punto a favor
        if signal_dir == "BUY":
            score += 3.0 + (confidence * 2.0)
            points.append(
                f"Señal técnica compuesta es BUY (confianza {confidence:.0%})"
            )

        # Analizar indicadores individuales
        for name, info in indicators.items():
            direction = info.get("direction", "NEUTRAL")
            detail = info.get("detail", "")

            if direction in ("BUY", "STRONG_BUY"):
                points.append(f"{name}: {detail}")
                score += 1.0

            # Puntos parcialmente favorables
            if name == "RSI" and info.get("value", 50) < 40:
                if direction == "NEUTRAL":
                    points.append(f"RSI en zona baja ({info['value']}) — acercándose a sobreventa")
                    score += 0.5

            if name == "EMA_Cross" and info.get("value", 0) > 0:
                if direction == "NEUTRAL":
                    points.append(f"EMA50 por encima de EMA200 (spread +{info['value']:.2f}%)")
                    score += 0.5

        return points, min(score, 10.0)

    def _evaluate_on_chain(
        self, data: Dict[str, Any]
    ) -> tuple[List[str], float]:
        """Extrae puntos alcistas de datos on-chain."""
        points: List[str] = []
        score = 5.0

        if not data:
            return points, score

        # Exchange outflow > inflow → acumulación
        inflow = data.get("exchange_inflow", 0)
        outflow = data.get("exchange_outflow", 0)
        if outflow > inflow and inflow > 0:
            ratio = outflow / inflow
            points.append(
                f"Outflow > Inflow en exchanges (ratio {ratio:.2f}x) — acumulación"
            )
            score += min(ratio, 3.0)

        # Active addresses creciendo
        active = data.get("active_addresses", 0)
        active_prev = data.get("active_addresses_prev", 0)
        if active > active_prev and active_prev > 0:
            growth = ((active - active_prev) / active_prev) * 100
            points.append(f"Direcciones activas +{growth:.1f}% — adopción creciente")
            score += min(growth / 10, 2.0)

        # Fear & Greed bajo = oportunidad contrarian
        fg = data.get("fear_greed_index")
        if fg is not None and fg < 30:
            points.append(f"Fear & Greed Index = {fg} — miedo extremo (contrarian bullish)")
            score += 2.0

        # Sentimiento positivo
        sentiment = data.get("sentiment_score", 0)
        if sentiment > 0.3:
            points.append(f"Sentimiento de mercado positivo ({sentiment:.2f})")
            score += sentiment * 2

        # NVT bajo = red infravalorada
        nvt = data.get("nvt_ratio", 0)
        if 0 < nvt < 50:
            points.append(f"NVT ratio bajo ({nvt:.1f}) — red potencialmente infravalorada")
            score += 1.5

        return points, min(score, 10.0)

    def _evaluate_microstructure(self, alt_data: Dict[str, Any]) -> tuple[List[str], float]:
        """Extrae convexidad alcista desde imbalance del L1 y Perpetuos."""
        points: List[str] = []
        score = 5.0

        if not alt_data:
            return points, score

        ob = alt_data.get("orderbook", {})
        fund = alt_data.get("funding_rate", 0.0)

        # Imbalance > 0 significa mas bids (compradores absorbiendo)
        imbal = ob.get("imbalance", 0.0)
        if imbal > 0.4:
            points.append(f"Gigante pared de Bids en L1 (Imbalance +{imbal:.0%}), bloqueando caídas.")
            score += 2.0
        elif imbal > 0.1:
            points.append(f"Mano compradora activa en el Book Ticker (+{imbal:.0%})")
            score += 0.5
            
        # Funding rate negativo masivo favorece un short-squeeze alcista
        if fund < -0.001:
            points.append(f"Funding extremadamente negativo ({fund:.4%}). ALTA probabilidad de Short-Squeeze institucional.")
            score += 2.5
        elif fund < 0:
            points.append(f"Cortos pagando a largos (Funding {fund:.4%}), sesgo alcista.")
            score += 0.5
            
        return points, min(score, 10.0)

    def _evaluate_price_action(
        self, current_price: float, signal: Dict[str, Any]
    ) -> tuple[List[str], float]:
        """Evalúa la acción del precio como argumento alcista."""
        points: List[str] = []
        score = 5.0

        indicators = signal.get("indicators", {})

        # Precio cerca de banda inferior de Bollinger
        bb = indicators.get("Bollinger", {})
        bb_value = bb.get("value", 0.5)  # %B
        if bb_value < 0.2:
            points.append(
                f"Precio cerca de banda inferior Bollinger (%B={bb_value:.2f}) — zona de soporte"
            )
            score += 2.5

        # Volumen anómalo con vela verde
        vol = indicators.get("Volume", {})
        vol_dir = vol.get("direction", "NEUTRAL")
        vol_value = vol.get("value", 1.0)
        if vol_dir == "BUY" and vol_value > 2.0:
            points.append(
                f"Volumen anómalo alcista ({vol_value:.1f}× promedio)"
            )
            score += 2.0

        if not points:
            points.append(f"Precio actual: {current_price:,.2f} USDT")

        return points, min(score, 10.0)

    # ══════════════════════════════════════════════════════════════════
    #  Narrativa
    # ══════════════════════════════════════════════════════════════════

    def _build_argument(
        self,
        price: float,
        points: List[str],
        strength: float,
    ) -> str:
        """Construye un argumento narrativo alcista."""
        if not points:
            return f"Sin argumentos alcistas sólidos al precio actual de {price:,.2f} USDT."

        conviction = (
            "FUERTE" if strength >= 7
            else "MODERADA" if strength >= 4
            else "DÉBIL"
        )

        header = (
            f"📈 ARGUMENTO ALCISTA (convicción {conviction}, strength={strength}/10)\n"
            f"Precio actual: {price:,.2f} USDT\n"
        )
        body = "\n".join(f"  • {p}" for p in points)

        return f"{header}\nPuntos a favor:\n{body}"

    # ══════════════════════════════════════════════════════════════════
    #  Estado para el árbitro
    # ══════════════════════════════════════════════════════════════════

    def get_state(self) -> Dict[str, Any]:
        """Retorna el último output para que el árbitro lo consuma."""
        return self._last_output or {
            "stance": "BULL",
            "argument": "Sin análisis disponible",
            "strength": 0,
        }

    def reset(self) -> None:
        """Reinicia el estado interno."""
        self._last_output = None
