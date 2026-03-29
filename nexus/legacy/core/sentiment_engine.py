"""
NEXUS Trading System — Sentiment Engine
=========================================
Analisis de sentimiento NLP sobre noticias cripto y metricas on-chain
para generar un score complementario a las senales tecnicas.

Componentes:
  - NewsFetcher: obtiene noticias de APIs publicas (CoinGecko, CryptoPanic)
  - NLPAnalyzer: analisis de sentimiento via LLM (Groq/Gemini) o reglas
  - OnChainAnalyzer: metricas on-chain (Fear & Greed, exchange flows)
  - SentimentEngine: orquestador que combina NLP + on-chain
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import numpy as np  # type: ignore

logger = logging.getLogger("nexus.sentiment")

# Lazy imports
try:
    import aiohttp  # type: ignore
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False

try:
    import pandas as pd  # type: ignore
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False


# ══════════════════════════════════════════════════════════════════════
#  Data Classes
# ══════════════════════════════════════════════════════════════════════

@dataclass
class NewsArticle:
    """Articulo de noticias normalizado."""
    title: str
    source: str
    url: str
    published_at: datetime
    content: Optional[str] = None
    sentiment_score: Optional[float] = None   # -1.0 a 1.0


@dataclass
class OnChainMetrics:
    """Metricas on-chain relevantes."""
    active_addresses: int = 0
    exchange_inflow: float = 0.0
    exchange_outflow: float = 0.0
    nvt_ratio: float = 0.0
    hash_rate: Optional[float] = None
    fear_greed_index: Optional[int] = None


@dataclass
class SentimentResult:
    """Resultado consolidado del analisis de sentimiento."""
    overall_score: float = 0.0          # -1.0 a 1.0
    news_score: float = 0.0
    on_chain_score: float = 0.0
    confidence: float = 0.0             # 0.0 a 1.0
    num_articles_analyzed: int = 0
    fear_greed: Optional[int] = None
    dominant_sentiment: str = "neutral"  # bullish, bearish, neutral
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ══════════════════════════════════════════════════════════════════════
#  News Fetcher
# ══════════════════════════════════════════════════════════════════════

# Diccionarios para analisis basado en reglas
_BULLISH_KEYWORDS = [
    "rally", "surge", "bullish", "breakout", "all-time high", "ath",
    "pump", "moon", "adoption", "institutional", "etf approved",
    "partnership", "upgrade", "halving", "accumulation", "buy signal",
    "support", "recovery", "green", "soar", "gain", "profit",
    "uptick", "uptrend", "growth", "approval", "alcista", "sube",
]

_BEARISH_KEYWORDS = [
    "crash", "dump", "bearish", "breakdown", "sell-off", "plunge",
    "liquidation", "hack", "ban", "regulation", "fraud", "scam",
    "ponzi", "lawsuit", "sec", "fud", "fear", "panic", "collapse",
    "decline", "drop", "fall", "loss", "resistance", "bajista", "baja",
    "warning", "risk", "concern", "investigation", "manipulation",
]


class NewsFetcher:
    """
    Obtiene noticias de multiples fuentes publicas:
    - CryptoPanic API (gratuita, requiere token opcional)
    - Alternative.me Fear & Greed Index (gratuito)
    """

    CRYPTOPANIC_URL = "https://cryptopanic.com/api/v1/posts/"
    FEAR_GREED_URL = "https://api.alternative.me/fng/"

    def __init__(self, sources: Optional[List[str]] = None) -> None:
        self.sources = sources or ["cryptopanic", "alternative_me"]
        self._cryptopanic_token = os.getenv("CRYPTOPANIC_API_TOKEN", "")

    async def fetch_latest(self, limit: int = 50) -> List[NewsArticle]:
        """Descarga las ultimas noticias cripto."""
        articles: List[NewsArticle] = []

        if not _HAS_AIOHTTP:
            logger.warning("aiohttp no disponible, retornando lista vacia")
            return articles

        async with aiohttp.ClientSession() as session:
            # CryptoPanic
            try:
                params: Dict[str, Any] = {
                    "public": "true",
                    "kind": "news",
                }
                if self._cryptopanic_token:
                    params["auth_token"] = self._cryptopanic_token

                async with session.get(
                    self.CRYPTOPANIC_URL,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for item in data.get("results", [])[:limit]:
                            try:
                                pub_date = datetime.fromisoformat(
                                    item.get("published_at", "").replace("Z", "+00:00")
                                )
                            except (ValueError, AttributeError):
                                pub_date = datetime.now(timezone.utc)

                            articles.append(NewsArticle(
                                title=item.get("title", ""),
                                source=item.get("source", {}).get("title", "CryptoPanic"),
                                url=item.get("url", ""),
                                published_at=pub_date,
                                content=item.get("title", ""),
                            ))
                    else:
                        logger.warning("CryptoPanic API status %d", resp.status)
            except Exception as exc:
                logger.warning("Error fetching CryptoPanic: %s", exc)

        logger.info("Noticias obtenidas: %d articulos", len(articles))
        return articles

    async def fetch_by_symbol(self, symbol: str, limit: int = 20) -> List[NewsArticle]:
        """Descarga noticias filtradas por simbolo."""
        articles: List[NewsArticle] = []

        if not _HAS_AIOHTTP:
            return articles

        # Convertir symbol (BTCUSDT -> BTC)
        coin = symbol.replace("USDT", "").replace("USD", "").upper()

        async with aiohttp.ClientSession() as session:
            try:
                params: Dict[str, Any] = {
                    "public": "true",
                    "kind": "news",
                    "currencies": coin,
                }
                if self._cryptopanic_token:
                    params["auth_token"] = self._cryptopanic_token

                async with session.get(
                    self.CRYPTOPANIC_URL,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for item in data.get("results", [])[:limit]:
                            try:
                                pub_date = datetime.fromisoformat(
                                    item.get("published_at", "").replace("Z", "+00:00")
                                )
                            except (ValueError, AttributeError):
                                pub_date = datetime.now(timezone.utc)

                            articles.append(NewsArticle(
                                title=item.get("title", ""),
                                source=item.get("source", {}).get("title", "CryptoPanic"),
                                url=item.get("url", ""),
                                published_at=pub_date,
                                content=item.get("title", ""),
                            ))
            except Exception as exc:
                logger.warning("Error fetching news for %s: %s", symbol, exc)

        return articles

    async def fetch_fear_greed(self) -> Optional[int]:
        """Obtiene el indice Fear & Greed de Alternative.me."""
        if not _HAS_AIOHTTP:
            return None

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.FEAR_GREED_URL,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("data", [])
                        if items:
                            value = int(items[0].get("value", 50))
                            label = items[0].get("value_classification", "Neutral")
                            logger.info("Fear & Greed Index: %d (%s)", value, label)
                            return value
        except Exception as exc:
            logger.warning("Error fetching Fear & Greed: %s", exc)

        return None


# ══════════════════════════════════════════════════════════════════════
#  NLP Analyzer
# ══════════════════════════════════════════════════════════════════════

class NLPAnalyzer:
    """
    Analisis de sentimiento NLP.

    Usa un enfoque hibrido:
    1. Primario:   LLM via LangChain (Groq/Gemini) para analisis detallado
    2. Fallback:   Analisis basado en keywords (sin dependencia externa)
    """

    _SENTIMENT_PROMPT = """Analyze the sentiment of the following cryptocurrency news headline.
Return ONLY a JSON object with:
- "score": float between -1.0 (very bearish) and 1.0 (very bullish), 0.0 is neutral
- "label": "bullish", "bearish", or "neutral"

Headline: {text}

JSON:"""

    def __init__(self) -> None:
        self._chain = None
        self._initialized = False

    def initialize(self) -> None:
        """Configura la cadena LangChain para analisis de sentimiento."""
        try:
            from langchain_core.output_parsers import StrOutputParser  # type: ignore
            from langchain_core.prompts import ChatPromptTemplate  # type: ignore

            # Intentar Groq primero, luego Gemini
            llm = self._create_llm()
            if llm is None:
                logger.info("NLP: Sin LLM disponible, usando keyword analysis")
                return

            self._chain = (
                ChatPromptTemplate.from_template(self._SENTIMENT_PROMPT)
                | llm
                | StrOutputParser()
            )
            self._initialized = True
            logger.info("NLPAnalyzer inicializado con LLM")
        except Exception as exc:
            logger.warning("Error inicializando NLPAnalyzer: %s", exc)

    def _create_llm(self) -> Any:
        """Crea un LLM para analisis de sentimiento."""
        # Intentar Groq
        try:
            from langchain_groq import ChatGroq  # type: ignore
            api_key = os.getenv("GROQ_API_KEY", "")
            if api_key:
                return ChatGroq(
                    model="llama-3.3-70b-versatile",
                    api_key=api_key,
                    temperature=0.0,
                    max_tokens=100,
                )
        except ImportError:
            pass

        # Intentar Gemini
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI  # type: ignore
            api_key = os.getenv("GOOGLE_API_KEY", "")
            if api_key:
                return ChatGoogleGenerativeAI(
                    model="gemini-2.5-flash-preview-05-20",
                    google_api_key=api_key,
                    temperature=0.0,
                    max_output_tokens=100,
                )
        except ImportError:
            pass

        return None

    def analyze_text(self, text: str) -> float:
        """Retorna score de sentimiento (-1.0 a 1.0) para un texto."""
        if self._initialized and self._chain:
            try:
                return self._analyze_with_llm(text)
            except Exception:
                pass

        return self._analyze_with_keywords(text)

    def _analyze_with_llm(self, text: str) -> float:
        """Analisis via LLM."""
        import json as _json
        raw = self._chain.invoke({"text": text})  # type: ignore
        match = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
        if match:
            data = _json.loads(match.group())
            return max(-1.0, min(1.0, float(data.get("score", 0.0))))
        return 0.0

    def _analyze_with_keywords(self, text: str) -> float:
        """Analisis basado en diccionarios de keywords."""
        text_lower = text.lower()
        bull_count = sum(1 for kw in _BULLISH_KEYWORDS if kw in text_lower)
        bear_count = sum(1 for kw in _BEARISH_KEYWORDS if kw in text_lower)

        total = bull_count + bear_count
        if total == 0:
            return 0.0

        score = (bull_count - bear_count) / total
        return max(-1.0, min(1.0, score))

    def analyze_batch(self, articles: List[NewsArticle]) -> List[NewsArticle]:
        """Analiza multiples articulos y actualiza su sentiment_score."""
        for article in articles:
            text = article.title
            if article.content:
                text = f"{article.title}. {article.content[:200]}"  # type: ignore
            article.sentiment_score = self.analyze_text(text)
        return articles

    def summarize_sentiment(self, articles: List[NewsArticle]) -> str:
        """Genera un resumen del sentimiento general."""
        if not articles:
            return "Sin articulos para analizar."

        scored = [a for a in articles if a.sentiment_score is not None]
        if not scored:
            return "Sin scores de sentimiento disponibles."

        scores = [a.sentiment_score for a in scored]
        avg = np.mean(scores)
        bull = sum(1 for s in scores if s > 0.2)  # type: ignore
        bear = sum(1 for s in scores if s < -0.2)  # type: ignore
        neutral = len(scores) - bull - bear

        if avg > 0.2:
            mood = "ALCISTA"
        elif avg < -0.2:
            mood = "BAJISTA"
        else:
            mood = "NEUTRAL"

        return (
            f"Sentimiento {mood} (score promedio: {avg:.2f}). "
            f"Articulos: {len(scored)} total | "
            f"{bull} alcistas | {bear} bajistas | {neutral} neutrales"
        )


# ══════════════════════════════════════════════════════════════════════
#  On-Chain Analyzer
# ══════════════════════════════════════════════════════════════════════

class OnChainAnalyzer:
    """
    Analiza metricas on-chain para generar un score complementario.
    Usa la API gratuita de Alternative.me para Fear & Greed index
    y simula metricas avanzadas (Glassnode requiere API de pago).
    """

    def __init__(self, provider: str = "alternative_me") -> None:
        self.provider = provider

    async def fetch_metrics(self, symbol: str = "BTC") -> OnChainMetrics:
        """Obtiene las metricas on-chain mas recientes."""
        metrics = OnChainMetrics()

        # Fear & Greed Index (gratuito)
        fetcher = NewsFetcher()
        fgi = await fetcher.fetch_fear_greed()
        if fgi is not None:
            metrics.fear_greed_index = fgi

        # Las metricas avanzadas (active_addresses, exchange_flow, etc.)
        # requieren Glassnode API de pago. Por ahora simulamos con valores
        # basados en el Fear & Greed Index.
        if fgi is not None:
            metrics.active_addresses = int(800_000 + (fgi / 100) * 400_000)
            metrics.nvt_ratio = 50.0 + (50 - fgi) * 0.5
            if fgi > 60:
                metrics.exchange_inflow = 5000 + (fgi - 50) * 100
                metrics.exchange_outflow = 8000 + (fgi - 50) * 200
            else:
                metrics.exchange_inflow = 8000 + (50 - fgi) * 200
                metrics.exchange_outflow = 5000 + (50 - fgi) * 50

        return metrics

    def calculate_score(self, metrics: OnChainMetrics) -> float:
        """
        Calcula un score on-chain normalizado (-1.0 a 1.0).

        Factores:
        - Fear & Greed Index: < 25 = bearish, > 75 = bullish
        - Net exchange flow: outflow > inflow = bullish (acumulacion)
        - NVT ratio: bajo = bullish (uso alto vs valorizacion)
        """
        scores: List[float] = []

        # Fear & Greed (0-100 → -1 a 1)
        if metrics.fear_greed_index is not None:
            fgi_normalized = (metrics.fear_greed_index - 50) / 50.0  # type: ignore
            scores.append(fgi_normalized)

        # Net exchange flow
        net_flow = metrics.exchange_outflow - metrics.exchange_inflow
        if metrics.exchange_inflow > 0:
            flow_ratio = net_flow / max(metrics.exchange_inflow, 1)
            flow_score = max(-1.0, min(1.0, flow_ratio * 0.5))
            scores.append(flow_score)

        # NVT ratio (alto = bearish, bajo = bullish)
        if metrics.nvt_ratio > 0:
            if metrics.nvt_ratio > 80:
                scores.append(-0.6)
            elif metrics.nvt_ratio < 30:
                scores.append(0.6)
            else:
                nvt_score = (50 - metrics.nvt_ratio) / 50.0 * 0.5
                scores.append(nvt_score)

        if not scores:
            return 0.0

        return round(max(-1.0, min(1.0, float(np.mean(scores)))), 4)  # type: ignore

    def detect_whale_activity(self, metrics: OnChainMetrics) -> Dict[str, Any]:
        """Detecta actividad inusual de ballenas."""
        total_flow = metrics.exchange_inflow + metrics.exchange_outflow
        net_flow = metrics.exchange_outflow - metrics.exchange_inflow

        is_whale = total_flow > 20000  # Umbral simplificado
        direction = "accumulation" if net_flow > 0 else "distribution"

        return {
            "detected": is_whale,
            "direction": direction,
            "net_flow": round(net_flow, 2),  # type: ignore
            "total_flow": round(total_flow, 2),  # type: ignore
            "severity": "HIGH" if total_flow > 50000 else "MODERATE" if is_whale else "LOW",
        }


# ══════════════════════════════════════════════════════════════════════
#  Sentiment Engine (Orquestador)
# ══════════════════════════════════════════════════════════════════════

class SentimentEngine:
    """
    Motor principal de sentimiento: combina NLP de noticias y datos on-chain
    en un score consolidado.

    Pesos por defecto: 60% noticias NLP + 40% on-chain
    """

    def __init__(
        self,
        news_weight: float = 0.6,
        on_chain_weight: float = 0.4,
    ) -> None:
        self.news_weight = news_weight
        self.on_chain_weight = on_chain_weight
        self.news_fetcher = NewsFetcher()
        self.nlp = NLPAnalyzer()
        self.on_chain = OnChainAnalyzer()
        self._history: List[SentimentResult] = []

    def initialize(self) -> None:
        """Inicializa el NLPAnalyzer con LLM si esta disponible."""
        self.nlp.initialize()

    async def analyze(self, symbol: str = "BTC") -> SentimentResult:
        """
        Ejecuta analisis completo y retorna resultado consolidado.

        Pasos:
        1. Obtener noticias recientes
        2. Analizar sentimiento con NLP
        3. Obtener metricas on-chain
        4. Combinar scores con pesos configurados
        """
        # 1. Noticias
        try:
            articles = await self.news_fetcher.fetch_by_symbol(symbol, limit=30)
        except Exception as exc:
            logger.warning("Error obteniendo noticias: %s", exc)
            articles = []

        # 2. NLP
        if articles:
            articles = self.nlp.analyze_batch(articles)
            scored = [a for a in articles if a.sentiment_score is not None]
            news_score = float(np.mean([a.sentiment_score for a in scored])) if scored else 0.0
        else:
            news_score = 0.0

        # 3. On-chain
        try:
            metrics = await self.on_chain.fetch_metrics(symbol)
            on_chain_score = self.on_chain.calculate_score(metrics)
            fear_greed = metrics.fear_greed_index
        except Exception as exc:
            logger.warning("Error analizando on-chain: %s", exc)
            on_chain_score = 0.0
            fear_greed = None

        # 4. Score consolidado
        overall = (
            news_score * self.news_weight +
            on_chain_score * self.on_chain_weight
        )
        overall = max(-1.0, min(1.0, overall))

        # Confianza basada en cantidad de datos
        confidence = min(1.0, len(articles) / 20.0) * 0.7 + (0.3 if fear_greed is not None else 0.0)

        # Sentimiento dominante
        if overall > 0.2:
            dominant = "bullish"
        elif overall < -0.2:
            dominant = "bearish"
        else:
            dominant = "neutral"

        result = SentimentResult(
            overall_score=round(overall, 4),  # type: ignore
            news_score=round(news_score, 4),  # type: ignore
            on_chain_score=round(on_chain_score, 4),  # type: ignore
            confidence=round(confidence, 4),  # type: ignore
            num_articles_analyzed=len(articles),
            fear_greed=fear_greed,
            dominant_sentiment=dominant,
        )

        self._history.append(result)
        if len(self._history) > 500:
            self._history = self._history[-500:]  # type: ignore

        logger.info(
            "Sentimiento %s: overall=%.2f (news=%.2f, chain=%.2f) | "
            "articulos=%d | F&G=%s | conf=%.2f",
            symbol, overall, news_score, on_chain_score,
            len(articles),
            str(fear_greed) if fear_greed is not None else "N/A",
            confidence,
        )

        return result

    def get_score(self) -> Dict[str, Any]:
        """
        Retorna el ultimo score de sentimiento como dict.
        Usa el ultimo resultado cacheado si esta disponible.
        Metodo sincrono para compatibilidad con el main loop.
        """
        if self._history:
            last = self._history[-1]
            return {
                "overall_score": last.overall_score,
                "news_score": last.news_score,
                "on_chain_score": last.on_chain_score,
                "confidence": last.confidence,
                "fear_greed": last.fear_greed,
                "dominant_sentiment": last.dominant_sentiment,
                "num_articles": last.num_articles_analyzed,
            }

        return {
            "overall_score": 0.0,
            "news_score": 0.0,
            "on_chain_score": 0.0,
            "confidence": 0.0,
            "fear_greed": None,
            "dominant_sentiment": "neutral",
            "num_articles": 0,
        }

    def get_historical_sentiment(
        self,
        symbol: str = "BTC",
        days: int = 30,
    ) -> Any:
        """Retorna el historial de sentimiento como DataFrame."""
        if not _HAS_PANDAS or not self._history:
            return None

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        records = [
            {
                "timestamp": r.timestamp,
                "overall_score": r.overall_score,
                "news_score": r.news_score,
                "on_chain_score": r.on_chain_score,
                "fear_greed": r.fear_greed,
                "confidence": r.confidence,
            }
            for r in self._history
            if r.timestamp >= cutoff
        ]

        if not records:
            return pd.DataFrame()

        return pd.DataFrame(records)

    def set_weights(self, news: float, on_chain: float) -> None:
        """Ajusta los pesos de las fuentes de sentimiento."""
        total = news + on_chain
        if total <= 0:
            raise ValueError("Los pesos deben ser positivos")
        self.news_weight = news / total
        self.on_chain_weight = on_chain / total
        logger.info(
            "Pesos actualizados: news=%.1f%%, on_chain=%.1f%%",
            self.news_weight * 100, self.on_chain_weight * 100,
        )

    def __repr__(self) -> str:
        n = len(self._history)
        last = f"last={self._history[-1].overall_score:.2f}" if n > 0 else "no data"
        return f"<SentimentEngine history={n} {last}>"
