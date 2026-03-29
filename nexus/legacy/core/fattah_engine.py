"""
NEXUS Trading System — Modo Fattah (Macro-Hedge Engine)
=============================================================
Este módulo cuantifica el riesgo sistémico de colapso del USD (DXY),
evalúa refugios seguros (GLD, BTC, CHF), extrae noticias geopolíticas en
tiempo real como Leading Indicators, y asigna ponderaciones de portafolio
para la supervivencia y multiplicación de liquidez en entornos hostiles.
"""

import os
import sys
import json
import logging
import traceback
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

import pandas as pd
import yfinance as yf
from langchain_groq import ChatGroq

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import settings

logger = logging.getLogger("nexus.fattah_engine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(levelname)s | %(message)s")


class GeopoliticalSentimentCore:
    """
    El Oráculo de Noticias (Leading Indicator).
    Extrae titulares de yfinance para DXY y Oro, y consulta al LLM (Llama 3 70B)
    para medir el nivel de pánico institucional y desdolarización (0 a 100).
    """
    def __init__(self):
        self._llm = None
        self._setup_llm()

    def _setup_llm(self):
        api_key = getattr(settings, "GROQ_API_KEY", None)
        if api_key:
            try:
                self._llm = ChatGroq(
                    temperature=0.0,
                    model_name="llama-3.3-70b-versatile",
                    groq_api_key=api_key,
                )
            except Exception as e:
                logger.error(f"Fallo al inicializar Groq LLM en SentimentCore: {e}")

    def fetch_market_headlines(self) -> str:
        """Extrae titulares recientes de los tickers clave."""
        headlines = []
        try:
            # DXY, Oro, VIX, Petróleo
            for ticker in ["DX-Y.NYB", "GC=F", "^VIX", "CL=F"]:
                t = yf.Ticker(ticker)
                news = t.news
                if news:
                    # Tomar los 3 titulares más recientes por activo
                    for item in news[:3]:
                        title = item.get("title", "")
                        if title and title not in headlines:
                            headlines.append(f"[{ticker}] {title}")
        except Exception as e:
            logger.warning(f"Error extrayendo noticias yfinance: {e}")
            
        if not headlines:
            return "No recent geopolitical news extracted."
        return "\n".join(headlines)

    def evaluate_sentiment(self) -> Dict[str, Any]:
        """Consulta al LLM para calificar el miedo global (0 = Paz, 100 = Pánico/Colapso)."""
        headlines = self.fetch_market_headlines()
        
        fallback = {"score": 50, "reason": "No LLM available or extraction failed."}
        if not self._llm:
            return fallback

        prompt = f"""
        You are an elite BlackRock macro-economist assessing global systemic risk.
        Read these recent financial news headlines:
        {headlines}

        Evaluate the "Geopolitical & Currency Fear Score" from 0 to 100.
        0 = Total global stability, USD hegemony is absolutely unquestioned.
        50 = Normal localized tensions, standard market noise.
        100 = Imminent collapse of USD, BRICS aggressive de-dollarization, World War, severe systemic panic.

        Return ONLY a strict JSON object with this exact format:
        {{
            "score": [integer 0-100],
            "reason": "[1 short sentence summarizing the panic level]"
        }}
        """

        try:
            response = self._llm.invoke(prompt)
            content = response.content.replace("```json", "").replace("```", "").strip()
            data = json.loads(content)
            
            score = int(data.get("score", 50))
            # Normalizar límites
            score = max(0, min(100, score))
            reason = data.get("reason", "Análisis LLM completado.")
            
            logger.info(f"Geopol LLM Score: {score}/100 - {reason}")
            return {"score": score, "reason": reason}
            
        except Exception as e:
            logger.error(f"LLM Geopol Eval Failed: {e}")
            return fallback


class FattahFearIndex:
    """
    Combina métricas cuantitativas (Price Action de Refugios) con el Oráculo
    de Noticias cualitativo para emitir un Fear Score final de 0 a 100.
    """
    def __init__(self):
        self.sentiment_core = GeopoliticalSentimentCore()

    def get_market_metrics(self) -> Dict[str, float]:
        """Descarga el pulso cuantitativo de los mercados hoy vs hace 20 días."""
        metrics = {"dxy_drop_pct": 0.0, "vix_level": 15.0, "gold_surge_pct": 0.0}
        try:
            # DXY
            dxy = yf.Ticker("DX-Y.NYB").history(period="1mo")
            if len(dxy) >= 20:
                dxy_old = dxy['Close'].iloc[-20]
                dxy_new = dxy['Close'].iloc[-1]
                metrics["dxy_drop_pct"] = ((dxy_old - dxy_new) / dxy_old) * 100

            # VIX
            vix = yf.Ticker("^VIX").history(period="5d")
            if not vix.empty:
                metrics["vix_level"] = float(vix['Close'].iloc[-1])

            # GOLD
            gld = yf.Ticker("GC=F").history(period="1mo")
            if len(gld) >= 20:
                gld_old = gld['Close'].iloc[-20]
                gld_new = gld['Close'].iloc[-1]
                metrics["gold_surge_pct"] = ((gld_new - gld_old) / gld_old) * 100

        except Exception as e:
            logger.warning(f"Error obteniendo métricas Macro YF: {e}")
            
        return metrics

    def calculate_fear_index(self) -> Dict[str, Any]:
        """Retorna el Índice de Miedo global (0-100)."""
        metrics = self.get_market_metrics()
        sentiment = self.sentiment_core.evaluate_sentiment()

        # Score Cuantitativo (0 a 100)
        # 1. Si el DXY cae mucho (+ miedo), ej: cae 3% en 1 mes = 30 puntos
        quant_dxy = max(0, min(50, metrics["dxy_drop_pct"] * 10))
        
        # 2. Si VIX > 20 es miedo, cada punto sobre 15 suma 2 puntos
        quant_vix = max(0, min(25, (metrics["vix_level"] - 15) * 2))
        
        # 3. Si el Oro sube (+ miedo), ej: sube 5% en 1 mes = 25 puntos
        quant_gold = max(0, min(25, metrics["gold_surge_pct"] * 5))
        
        quant_score = quant_dxy + quant_vix + quant_gold
        
        # Ponderación: 60% Cuantitativo (Lagging pero real) + 40% Noticias (Leading pero ruidoso)
        llm_score = sentiment["score"]
        final_score = int((quant_score * 0.6) + (llm_score * 0.4))
        
        # Determinar régimen
        if final_score >= 65:
            regime = "PANIC_MODE (DXY COLLAPSE)"
        elif final_score >= 45:
            regime = "CAUTION (ROTATION START)"
        else:
            regime = "STABLE (USD HEGEMONY INTACT)"

        return {
            "score": final_score,
            "regime": regime,
            "quant_breakdown": {
                "dxy_score": round(quant_dxy, 1),
                "vix_score": round(quant_vix, 1),
                "gold_score": round(quant_gold, 1)
            },
            "sentiment_score": llm_score,
            "sentiment_reason": sentiment["reason"],
            "timestamp": datetime.now(timezone.utc).isoformat()
        }


class FattahAllocator:
    """
    Define los rebalanceos de capital basándose en el FEAR INDEX.
    """
    def __init__(self):
        self.engine = FattahFearIndex()
        self.state_file = os.path.join(os.path.dirname(__file__), "..", "data", "fattah_state.json")

    def run_allocation_cycle(self) -> Dict[str, Any]:
        fear_data = self.engine.calculate_fear_index()
        score = fear_data["score"]

        # Asignación conservadora (Bajo Miedo - USD Fuerte)
        allocation = {
            "USD_Stable": 70, # Bonos cortos / stablecoins
            "Gold_GLD": 10,
            "BTC_USD": 5,
            "CHF_JPY": 5,
            "Commodities": 10
        }

        # Asignación de Pánico (DXY Colapsando)
        if score >= 65:
            allocation = {
                "USD_Stable": 10, # Huir del dólar
                "Gold_GLD": 35,   # Refugio físico supremo
                "BTC_USD": 25,    # Refugio digital libre de confiscación
                "CHF_JPY": 15,    # Francos Suizos (solidez bancaria no-USA)
                "Commodities": 15 # Petróleo/Plata para inflación
            }
        # Asignación Cautelosa (Comienza a rotar)
        elif score >= 45:
            allocation = {
                "USD_Stable": 40,
                "Gold_GLD": 20,
                "BTC_USD": 15,
                "CHF_JPY": 10,
                "Commodities": 15
            }

        state = {
            "fear_index": fear_data,
            "optimal_allocation_pct": allocation
        }

        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

        logger.info(f"⚖️ Fattah Allocation Cycle Complete. Regime: {fear_data['regime']}")
        return state

if __name__ == "__main__":
    allocator = FattahAllocator()
    res = allocator.run_allocation_cycle()
    print("\n[MODO FATTAH] REPORTE DE ESTADO:")
    print(json.dumps(res, indent=2))
