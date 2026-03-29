"""
NEXUS Trading System — Agent Bear (Agente Bajista)
====================================================
Agente especializado en construir el argumento bajista a partir de
datos de riesgo, señales técnicas y sentimiento negativo.

Retorna: {"stance": "BEAR", "argument": str, "strength": 0-10}
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nexus.agent_bear")


class AgentBear:
    """
    Agente bajista: construye el mejor argumento posible a favor
    de una posición corta (SELL) o de reducción de exposición.

    Inputs:
        - precio actual
        - señal técnica (del TechnicalSignalEngine)
        - datos on-chain / sentimiento

    Selecciona los datos de mayor riesgo para construir su caso.
    """

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
        Construye el argumento bajista.

        Args:
            price:        Precio actual del activo.
            signal:       Output de TechnicalSignalEngine.generate_signal()
            onchain_data: Métricas on-chain / sentimiento (opcional).
            alt_data:     Microestructura y Funding Rate (opcional).

        Returns:
            {"stance": "BEAR", "argument": str, "strength": int 0-10}
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
        Construye el argumento bajista.

        Args:
            current_price:    Precio actual del activo.
            technical_signal: Output de TechnicalSignalEngine.generate_signal()
            on_chain_data:    Métricas on-chain / sentimiento (opcional).
            alt_data:         Microestructura y Funding Rate (opcional).

        Returns:
            {"stance": "BEAR", "argument": str, "strength": 0-10}
        """
        on_chain = on_chain_data or {}
        micro_data = alt_data or {}

        # 1. Puntos bajistas de señal técnica
        tech_points, tech_score = self._evaluate_technical(technical_signal)

        # 2. Puntos bajistas de on-chain
        chain_points, chain_score = self._evaluate_on_chain(on_chain)

        # 3. Acción de precio negativa
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
            "stance": "BEAR",
            "argument": argument,
            "strength": int(round(strength)),
        }

        logger.info("🐻 AgentBear: strength=%.1f | %d puntos bajistas", strength, len(all_points))
        return self._last_output  # type: ignore

    # ══════════════════════════════════════════════════════════════════
    #  Evaluadores de fuentes de datos
    # ══════════════════════════════════════════════════════════════════

    def _evaluate_technical(
        self, signal: Dict[str, Any]
    ) -> tuple[List[str], float]:
        """Extrae puntos bajistas de la señal técnica."""
        points: List[str] = []
        score = 5.0

        indicators = signal.get("indicators", {})
        signal_dir = signal.get("signal", "HOLD")
        confidence = signal.get("confidence", 0.0)

        # Señal global SELL
        if signal_dir == "SELL":
            score += 3.0 + (confidence * 2.0)
            points.append(
                f"Señal técnica compuesta es SELL (confianza {confidence:.0%})"
            )

        # Indicadores individuales bajistas
        for name, info in indicators.items():
            direction = info.get("direction", "NEUTRAL")
            detail = info.get("detail", "")

            if direction in ("SELL", "STRONG_SELL"):
                points.append(f"{name}: {detail}")
                score += 1.0

            # RSI en zona alta (PRIORIDAD: sobrecompra)
            if name == "RSI":
                rsi_val = info.get("value", 50)
                if direction in ("SELL", "STRONG_SELL"):
                    score += 1.5  # Boost extra por RSI sobrecomprado
                elif rsi_val > 55 and direction == "NEUTRAL":
                    points.append(
                        f"⚠️ RSI en zona alta ({rsi_val}) — acercándose a sobrecompra"
                    )
                    score += 1.0

            # EMA50 por debajo de EMA200 (tendencia bajista)
            if name == "EMA_Cross" and info.get("value", 0) < 0:
                if direction == "NEUTRAL":
                    points.append(
                        f"EMA50 por debajo de EMA200 (spread {info['value']:.2f}%)"
                    )
                    score += 0.5

        return points, min(score, 10.0)

    def _evaluate_on_chain(
        self, data: Dict[str, Any]
    ) -> tuple[List[str], float]:
        """Extrae puntos bajistas de datos on-chain."""
        points: List[str] = []
        score = 5.0

        if not data:
            return points, score

        # Exchange inflow > outflow → venta en exchanges
        inflow = data.get("exchange_inflow", 0)
        outflow = data.get("exchange_outflow", 0)
        if inflow > outflow and outflow > 0:
            ratio = inflow / outflow
            points.append(
                f"Inflow > Outflow en exchanges (ratio {ratio:.2f}x) — presión de venta"
            )
            score += min(ratio, 3.0)

        # Active addresses cayendo
        active = data.get("active_addresses", 0)
        active_prev = data.get("active_addresses_prev", 0)
        if active < active_prev and active_prev > 0:
            decline = ((active_prev - active) / active_prev) * 100
            points.append(f"Direcciones activas -{decline:.1f}% — adopción decreciente")
            score += min(decline / 10, 2.0)

        # Fear & Greed alto = euforia (contrarian bearish)
        fg = data.get("fear_greed_index")
        if fg is not None and fg > 75:
            points.append(f"Fear & Greed Index = {fg} — codicia extrema (contrarian bearish)")
            score += 2.0

        # Sentimiento negativo
        sentiment = data.get("sentiment_score", 0)
        if sentiment < -0.3:
            points.append(f"Sentimiento de mercado negativo ({sentiment:.2f})")
            score += abs(sentiment) * 2

        # NVT alto = red sobrevalorada
        nvt = data.get("nvt_ratio", 0)
        if nvt > 100:
            points.append(f"NVT ratio alto ({nvt:.1f}) — red potencialmente sobrevalorada")
            score += 1.5

        # Hash rate cayendo (riesgo de seguridad)
        hr = data.get("hash_rate")
        hr_prev = data.get("hash_rate_prev")
        if hr is not None and hr_prev is not None and hr < hr_prev:
            decline = ((hr_prev - hr) / hr_prev) * 100
            points.append(f"Hash rate -{decline:.1f}% — posible capitulación de mineros")
            score += 1.0

        # PRIORIDAD: Whale alerts (grandes transacciones)
        whale_alerts = data.get("whale_alerts", [])
        if whale_alerts:
            n_alerts = len(whale_alerts)
            total_vol = sum(a.get("amount", 0) for a in whale_alerts)
            # Distinguir dirección: si van a exchanges = venta masiva
            to_exchange = [a for a in whale_alerts if a.get("to_exchange", False)]
            if to_exchange:
                points.insert(0,  # Insertar al inicio = prioridad
                    f"🐋 {len(to_exchange)} whale alert(s) hacia exchanges "
                    f"(total {total_vol:,.0f} BTC) — presión de venta inminente"
                )
                score += min(len(to_exchange) * 1.5, 3.0)
            elif n_alerts > 0:
                points.append(
                    f"🐋 {n_alerts} whale alert(s) detectada(s) (total {total_vol:,.0f} BTC)"
                )
                score += 1.0

        # PRIORIDAD: Sentimiento negativo (reforzado)
        sentiment = data.get("sentiment_score", 0)
        if sentiment < -0.5:
            points.insert(0,
                f"🔴 Sentimiento muy negativo ({sentiment:.2f}) — pánico en mercado"
            )
            score += abs(sentiment) * 3
        elif sentiment < -0.3:
            points.append(f"Sentimiento negativo ({sentiment:.2f})")
            score += abs(sentiment) * 3

        return points, min(score, 10.0)

    def _evaluate_microstructure(self, alt_data: Dict[str, Any]) -> tuple[List[str], float]:
        """Extrae convexidad bajista desde imbalance del L1 y Perpetuos."""
        points: List[str] = []
        score = 5.0

        if not alt_data:
            return points, score

        ob = alt_data.get("orderbook", {})
        fund = alt_data.get("funding_rate", 0.0)

        # Imbalance < 0 significa mas asks (vendedores ahogando)
        imbal = ob.get("imbalance", 0.0)
        if imbal < -0.4:
            points.append(f"Gigante pared de Asks en L1 (Imbalance {imbal:.0%}), bloqueando subidas.")
            score += 2.0
        elif imbal < -0.1:
            points.append(f"Mano vendedora activa en el Book Ticker ({imbal:.0%})")
            score += 0.5
            
        # Funding rate altamente positivo castiga a largos (crowded trade riesgoso)
        if fund > 0.001:
            points.append(f"Funding extremadamente alto ({fund:.4%}). Largos pagando agresivamente, alta probabilidad de corrección (Long-Squeeze).")
            score += 2.5
        elif fund > 0.0001:
            points.append(f"Largos apalancados pagando a cortos (Funding {fund:.4%}), sesgo alcista exhausto.")
            score += 0.5
            
        return points, min(score, 10.0)

    def _evaluate_price_action(
        self, current_price: float, signal: Dict[str, Any]
    ) -> tuple[List[str], float]:
        """Evalúa la acción del precio como argumento bajista."""
        points: List[str] = []
        score = 5.0

        indicators = signal.get("indicators", {})

        # Precio cerca de banda superior de Bollinger
        bb = indicators.get("Bollinger", {})
        bb_value = bb.get("value", 0.5)  # %B
        if bb_value > 0.8:
            points.append(
                f"Precio cerca de banda superior Bollinger (%B={bb_value:.2f}) — zona de resistencia"
            )
            score += 2.5

        # Volumen anómalo con vela roja
        vol = indicators.get("Volume", {})
        vol_dir = vol.get("direction", "NEUTRAL")
        vol_value = vol.get("value", 1.0)
        if vol_dir == "SELL" and vol_value > 2.0:
            points.append(
                f"Volumen anómalo bajista ({vol_value:.1f}× promedio)"
            )
            score += 2.0

        if not points:
            points.append(f"Precio actual: {current_price:,.2f} USDT — sin señales de debilidad claras")

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
        """Construye un argumento narrativo bajista."""
        if not points:
            return f"Sin argumentos bajistas sólidos al precio actual de {price:,.2f} USDT."

        conviction = (
            "FUERTE" if strength >= 7
            else "MODERADA" if strength >= 4
            else "DÉBIL"
        )

        header = (
            f"📉 ARGUMENTO BAJISTA (convicción {conviction}, strength={strength}/10)\n"
            f"Precio actual: {price:,.2f} USDT\n"
        )
        body = "\n".join(f"  • {p}" for p in points)

        return f"{header}\nPuntos de riesgo:\n{body}"

    # ══════════════════════════════════════════════════════════════════
    #  Estado para el árbitro
    # ══════════════════════════════════════════════════════════════════

    def get_state(self) -> Dict[str, Any]:
        """Retorna el último output para que el árbitro lo consuma."""
        return self._last_output or {
            "stance": "BEAR",
            "argument": "Sin análisis disponible",
            "strength": 0,
        }

    def reset(self) -> None:
        """Reinicia el estado interno."""
        self._last_output = None
