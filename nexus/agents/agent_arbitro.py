# pyre-unsafe
"""
NEXUS Trading System — Agent Arbitro (Orquestador Multi-LLM)
==============================================================
Agente orquestador que usa LangChain con multiples proveedores LLM
para mediar entre el agente alcista y bajista, evaluar el riesgo
y emitir la decision final de trading.

Proveedores soportados:
  - GROQ    → Llama 3.3 70B Versatile  (ACTIVO - primario)
  - GEMINI  → Google Gemini 2.5 Flash   (ACTIVO - secundario)
  - OLLAMA  → Modelo local              (DESHABILITADO - futuro)

Nunca ejecuta una orden con confidence < 0.65.

Retorna:
{
    "decision": "BUY" | "SELL" | "HOLD",
    "confidence": 0-1,
    "reasoning": str,
    "position_size_pct": float
}
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from core.structured_logger import get_quant_logger

logger = logging.getLogger("nexus.agent_arbitro")

# ══════════════════════════════════════════════════════════════════════
#  Lazy imports — cada proveedor se importa solo cuando se necesita
# ══════════════════════════════════════════════════════════════════════

# LangChain core (compartido por todos los proveedores)
try:
    from langchain_core.messages import HumanMessage, SystemMessage  # type: ignore
    from langchain_core.output_parsers import StrOutputParser  # type: ignore
    from langchain_core.prompts import ChatPromptTemplate  # type: ignore
    _HAS_LANGCHAIN_CORE = True
except ImportError:
    _HAS_LANGCHAIN_CORE = False

# Groq
try:
    from langchain_groq import ChatGroq  # type: ignore
    _HAS_GROQ = True
except ImportError:
    _HAS_GROQ = False
    ChatGroq = None  # type: ignore

# Google Gemini
try:
    from langchain_google_genai import ChatGoogleGenerativeAI  # type: ignore
    _HAS_GEMINI = True
except ImportError:
    _HAS_GEMINI = False
    ChatGoogleGenerativeAI = None  # type: ignore

# Ollama (deshabilitado, para futura incorporacion)
try:
    from langchain_ollama import ChatOllama  # type: ignore
    _HAS_OLLAMA = True
except ImportError:
    _HAS_OLLAMA = False
    ChatOllama = None  # type: ignore


# ══════════════════════════════════════════════════════════════════════
#  Constantes
# ══════════════════════════════════════════════════════════════════════

MIN_CONFIDENCE_TO_EXECUTE = 0.65
MAX_POSITION_SIZE_PCT = 15.0      # Maximo 15% del capital por operacion
DEFAULT_POSITION_SIZE_PCT = 5.0   # Tamano por defecto


# ══════════════════════════════════════════════════════════════════════
#  LLM Provider Enum
# ══════════════════════════════════════════════════════════════════════

class LLMProvider(Enum):
    """Proveedores LLM disponibles para el arbitro."""
    GROQ = "groq"        # Llama 3.3 70B Versatile — ACTIVO
    GEMINI = "gemini"    # Google Gemini 2.5 Flash  — ACTIVO
    OLLAMA = "ollama"    # Local Ollama (llama3)    — DESHABILITADO


# Modelos por defecto para cada proveedor
_DEFAULT_MODELS = {
    LLMProvider.GROQ: "llama-3.3-70b-versatile",
    LLMProvider.GEMINI: "gemini-2.5-flash-preview-05-20",
    LLMProvider.OLLAMA: "llama3",
}


# ══════════════════════════════════════════════════════════════════════
#  Data classes
# ══════════════════════════════════════════════════════════════════════

@dataclass
class DebateLog:
    """Registro del debate entre agentes."""
    bull_argument: str
    bull_strength: float
    bear_argument: str
    bear_strength: float
    arbitro_reasoning: str
    final_decision: str
    confidence: float
    provider: str = "heuristic"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ══════════════════════════════════════════════════════════════════════
#  Prompts
# ══════════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """Eres un arbitro experto en trading de criptomonedas. Tu trabajo es analizar
los argumentos de dos agentes (alcista y bajista) junto con las metricas de riesgo del portafolio,
y tomar una decision final de trading.

REGLAS ESTRICTAS:
1. Debes responder SIEMPRE en formato JSON valido, sin texto adicional.
2. El campo "decision" solo puede ser "BUY", "SELL" o "HOLD".
3. El campo "confidence" debe ser un numero decimal entre 0.0 y 1.0.
4. El campo "reasoning" debe explicar tu razonamiento en espanol.
5. El campo "position_size_pct" debe ser un numero entre 0 y 15 (porcentaje del capital).
6. Si el riesgo del portafolio es alto (drawdown > 10% o exposicion > 80%), prefieres HOLD.
7. Si ambos agentes tienen strength < 4, la decision debe ser HOLD.
8. Se conservador: ante la duda, elige HOLD.

Formato de respuesta JSON:
{{
    "decision": "BUY" | "SELL" | "HOLD",
    "confidence": 0.0 - 1.0,
    "reasoning": "explicacion del razonamiento",
    "position_size_pct": 0.0 - 15.0
}}"""

_USER_PROMPT_TEMPLATE = """Analiza la siguiente situacion y toma una decision:

== CONTEXTO DE MERCADO (REGIMEN) ==
{market_context}

== AGENTE ALCISTA (BULL) ==
Strength: {bull_strength}/10
Argumento:
{bull_argument}

== AGENTE BAJISTA (BEAR) ==
Strength: {bear_strength}/10
Argumento:
{bear_argument}

== METRICAS DE RIESGO ==
{risk_summary}

Responde SOLO con el JSON de decision:"""


# ══════════════════════════════════════════════════════════════════════
#  AgentArbitro
# ══════════════════════════════════════════════════════════════════════

class AgentArbitro:
    """
    Arbitro Multi-LLM: orquesta la toma de decisiones usando multiples
    proveedores LLM como backend.

    Proveedores activos:
      - GROQ   (Llama 3.3 70B)  → primario
      - GEMINI (Gemini 2.5 Flash) → secundario / failover

    Flujo de failover:
      1. Intenta con el proveedor primario
      2. Si falla → intenta con el secundario
      3. Si ambos fallan → fallback heuristico (sin LLM)

    Nunca ejecuta una orden con confidence < 0.65.
    """

    # Orden de failover: primario → secundario
    _FAILOVER_ORDER = [LLMProvider.GROQ, LLMProvider.GEMINI]

    def __init__(
        self,
        provider: LLMProvider = LLMProvider.GROQ,
        model_name: Optional[str] = None,
        temperature: float = 0.1,
        # Ollama legacy params (deshabilitado)
        base_url: str = "http://localhost:11434",
        trading_mode: str = "standard",
    ) -> None:
        self.provider = provider
        self.model_name = model_name or _DEFAULT_MODELS.get(provider, "llama3")
        self.temperature = temperature
        self.base_url = base_url  # Solo para Ollama
        self.trading_mode = trading_mode

        self._llm: Any = None
        self._chain: Any = None
        self._debate_history: List[DebateLog] = []
        self._initialized = False
        self._active_provider: Optional[LLMProvider] = None

        # Almacen de LLMs secundarios para failover
        self._fallback_llms: Dict[LLMProvider, Any] = {}

        # ── LLM Optimization Cache ────────────────────────────────────────
        self._decision_cache: Dict[str, Dict[str, Any]] = {}
        self._CACHE_TTL_SECS = 3600  # 1 hora maximo de vida para el cache
        self._PRICE_TOLERANCE = 0.003 # 0.3% maximo de variacion permitida

        # ── Global WFO Timestamp Cache ────────────────────────────────────
        self._global_wfo_cache_path = os.path.join(
            os.path.dirname(__file__), "..", "logs", "llm_historical_cache.json"
        )
        self._global_wfo_cache: Dict[str, Any] = {}
        self._load_global_cache()

        # ── Multi-Key Accounts ──────────────────────────────────────────
        from config.settings import GROQ_API_KEYS, GOOGLE_API_KEYS
        self._groq_keys = GROQ_API_KEYS
        self._gemini_keys = GOOGLE_API_KEYS
        self._groq_idx = 0
        self._gemini_idx = 0

    def _load_global_cache(self) -> None:
        """Carga la caché global de Backtesting desde el disco para evitar re-inferencias WFO."""
        if os.path.exists(self._global_wfo_cache_path):
            try:
                with open(self._global_wfo_cache_path, "r", encoding="utf-8") as f:
                    self._global_wfo_cache = json.load(f)
            except Exception as e:
                logger.error("Error leyendo caché global: %s", e)
                self._global_wfo_cache = {}

    def _save_global_cache(self, timestamp: str, decision: Dict[str, Any]) -> None:
        """Guarda permanentemente una decisión del LLM en disco ligada a un Timestamp histórico."""
        self._global_wfo_cache[timestamp] = decision
        try:
            os.makedirs(os.path.dirname(self._global_wfo_cache_path), exist_ok=True)
            with open(self._global_wfo_cache_path, "w", encoding="utf-8") as f:
                json.dump(self._global_wfo_cache, f, indent=2)
        except Exception as e:
            logger.error("Error escribiendo caché global: %s", e)

    # ── Lifecycle ─────────────────────────────────────────────────────

    def initialize(self) -> None:
        """
        Configura el LLM segun el proveedor seleccionado.
        Intenta inicializar tambien el proveedor secundario para failover.
        """
        if not _HAS_LANGCHAIN_CORE:
            logger.warning(
                "LangChain core no disponible. AgentArbitro usara fallback heuristico."
            )
            self._initialized = False
            return

        # Inicializar proveedor primario
        success = self._init_provider(self.provider)

        if success:
            self._active_provider = self.provider
            self._initialized = True
            logger.info(
                "AgentArbitro inicializado con %s (%s)",
                self.provider.value,
                self.model_name,
            )

        # Inicializar proveedores secundarios para failover
        for fallback in self._FAILOVER_ORDER:
            if fallback != self.provider:
                self._init_fallback_provider(fallback)

        if not self._initialized:
            logger.warning(
                "Ningun proveedor LLM disponible. Usando fallback heuristico."
            )

    def _init_provider(self, provider: LLMProvider) -> bool:
        """Inicializa un proveedor LLM especifico. Retorna True si tuvo exito."""
        try:
            llm = self._create_llm(provider)
            if llm is None:
                return False

            chain = (
                ChatPromptTemplate.from_messages([
                    ("system", _SYSTEM_PROMPT),
                    ("human", _USER_PROMPT_TEMPLATE),
                ])
                | llm
                | StrOutputParser()
            )

            self._llm = llm
            self._chain = chain
            return True

        except Exception as exc:
            logger.error("Error inicializando %s: %s", provider.value, exc)
            return False

    def _init_fallback_provider(self, provider: LLMProvider) -> None:
        """Inicializa un proveedor secundario para failover."""
        try:
            llm = self._create_llm(provider)
            if llm is not None:
                self._fallback_llms[provider] = llm
                logger.info(
                    "Failover %s listo (%s)",
                    provider.value,
                    _DEFAULT_MODELS.get(provider, "unknown"),
                )
        except Exception as exc:
            logger.debug("Failover %s no disponible: %s", provider.value, exc)

    def _create_llm(self, provider: LLMProvider) -> Any:
        """
        Crea una instancia del LLM segun el proveedor.

        Returns:
            Instancia del ChatModel o None si no disponible.
        """
        model = _DEFAULT_MODELS.get(provider, self.model_name)

        # ── GROQ (Llama 3.3 70B) ────────────────────────────────
        if provider == LLMProvider.GROQ:
            if not _HAS_GROQ:
                logger.warning("langchain-groq no instalado.")
                return None

            api_key = self._groq_keys[self._groq_idx] if self._groq_keys else ""
            if not api_key:
                logger.warning("GROQ_API_KEYS no configurada.")
                return None

            return ChatGroq(  # type: ignore
                model=model,
                api_key=api_key,
                temperature=self.temperature,
                max_tokens=512,
                max_retries=0,
            )

        # ── GEMINI (2.5 Flash) ──────────────────────────────────
        elif provider == LLMProvider.GEMINI:
            if not _HAS_GEMINI:
                logger.warning("langchain-google-genai no instalado.")
                return None

            api_key = self._gemini_keys[self._gemini_idx] if self._gemini_keys else ""
            if not api_key:
                logger.warning("GOOGLE_API_KEYS no configurada.")
                return None

            return ChatGoogleGenerativeAI(  # type: ignore
                model=model,
                google_api_key=api_key,
                temperature=self.temperature,
                max_output_tokens=512,
                max_retries=0,
            )

        # ── OLLAMA (deshabilitado) ──────────────────────────────
        elif provider == LLMProvider.OLLAMA:
            logger.warning(
                "Ollama esta DESHABILITADO. Reservado para futuras incorporaciones. "
                "Use GROQ o GEMINI como proveedor."
            )
            return None

        else:
            logger.error("Proveedor desconocido: %s", provider)
            return None

    # ── Cambio de proveedor en caliente ──────────────────────────────

    def switch_provider(self, provider: LLMProvider) -> bool:
        """
        Cambia el proveedor LLM en tiempo de ejecucion.
        Util para A/B testing o failover manual.

        Args:
            provider: Nuevo proveedor a usar.

        Returns:
            True si el cambio fue exitoso.
        """
        if provider == LLMProvider.OLLAMA:
            logger.warning("Ollama esta deshabilitado. No se puede cambiar a este proveedor.")
            return False

        old_provider = self._active_provider
        model = _DEFAULT_MODELS.get(provider, self.model_name)

        success = self._init_provider(provider)
        if success:
            self.provider = provider
            self.model_name = model
            self._active_provider = provider
            self._initialized = True
            logger.info(
                "Proveedor cambiado: %s → %s (%s)",
                old_provider.value if old_provider else "none",
                provider.value,
                model,
            )
            return True
        else:
            logger.error("Error cambiando a %s — manteniendo %s", provider.value,
                         old_provider.value if old_provider else "heuristic")
            return False

    # ── Metodo principal ──────────────────────────────────────────────

    def deliberate(
        self,
        bull_state: Dict[str, Any],
        bear_state: Dict[str, Any],
        risk_metrics: Optional[Dict[str, Any]] = None,
        market_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Evalua los argumentos de ambos agentes y genera la decision final.

        Args:
            bull_state:     Output de AgentBull.get_state()
            bear_state:     Output de AgentBear.get_state()
            risk_metrics:   Metricas de riesgo del portafolio (opcional).
            market_context: Contexto adicional del mercado (opcional).

        Returns:
            {
                "decision": "BUY" | "SELL" | "HOLD",
                "confidence": 0-1,
                "reasoning": str,
                "position_size_pct": float
            }
        """
        risk = risk_metrics or {}
        bull_strength = bull_state.get("strength", 0)
        bear_strength = bear_state.get("strength", 0)
        bull_argument = bull_state.get("argument", "Sin argumento")
        bear_argument = bear_state.get("argument", "Sin argumento")

        # ── Extraccion del Market Context ─────────────────────────────
        symbol = market_context.get("symbol", "UNKNOWN") if market_context else "UNKNOWN"
        current_price = market_context.get("price", 0.0) if market_context else 0.0

        # Extraccion del Timestamp del backtest (inyectado por NexusStrategy)
        dt_str = market_context.get("timestamp") if market_context else None
        
        # ── Global WFO Timestamp Cache ────────────────────────────────
        if dt_str and dt_str in self._global_wfo_cache:
            decision_data = self._global_wfo_cache[dt_str]
            logger.info("💾 [Global Cache Hit] %s: Reutilizando razonamiento LLM estatico", dt_str)
            return decision_data

        # ── LLM Optimization: Stateful Context Cache ──────────────────
        if symbol != "UNKNOWN":
            cached = self._decision_cache.get(symbol)
            if cached and cached["decision"]["decision"] == "HOLD":
                elapsed = time.time() - cached["timestamp"]
                price_diff = abs(current_price - cached["price"]) / cached["price"] if cached["price"] > 0 else 0
                
                # Si las condiciones fisicas son casi identicas y no ha pasado mucho tiempo
                if (
                    elapsed < self._CACHE_TTL_SECS
                    and price_diff < self._PRICE_TOLERANCE
                    and abs(cached["bull_strength"] - bull_strength) <= 1
                    and abs(cached["bear_strength"] - bear_strength) <= 1
                ):
                    logger.info("⚡ Arbitro Cache Hit [%s]: Reutilizando HOLD (elapsed=%ds, price_diff=%.2f%%)", symbol, int(elapsed), price_diff * 100)
                    return cached["decision"]

        # ── Fast-path: si ambos agentes tienen strength <= 4, HOLD ─────
        if bull_strength <= 4 and bear_strength <= 4:
            result = self._hold_decision(
                "Ambos agentes con conviccion baja "
                f"(bull={bull_strength}, bear={bear_strength}). Sin operacion."
            )
            self._log_debate(bull_state, bear_state, result)
            return result

        # ── Intentar deliberacion con LLM (con failover) ──────────────
        used_provider = "heuristic"
        context_str = json.dumps(market_context or {}, indent=2)

        if self.trading_mode == "fattah":
            fattah_state_path = os.path.join(os.path.dirname(__file__), "..", "data", "fattah_state.json")
            if os.path.exists(fattah_state_path):
                try:
                    with open(fattah_state_path, "r", encoding="utf-8") as f:
                        f_state = json.load(f)
                    fear_idx = f_state.get("fear_index", {})
                    context_str += (
                        f"\n\n== MODO FATTAH (MACRO-HEDGE) ==\n"
                        f"FEAR REGIME: {fear_idx.get('regime', 'UNKNOWN')}\n"
                        f"FEAR SCORE: {fear_idx.get('score', 0)}/100\n"
                        f"SENTIMENT REASON: {fear_idx.get('sentiment_reason', 'No data')}\n"
                        f"INSTRUCCIÓN: Prioriza la rotación a activos refugio si Fear Score > 60."
                    )
                except Exception as e:
                    logger.warning(f"No se pudo cargar fattah_state.json: {e}")

        if self._initialized and self._chain is not None:
            try:
                result = self._deliberate_with_llm(
                    bull_argument, bull_strength,
                    bear_argument, bear_strength,
                    risk,
                    context_str,
                )
                used_provider = self._active_provider.value if self._active_provider else "llm"  # type: ignore
            except Exception as exc:
                logger.warning(
                    "Error en %s: %s — intentando failover...",
                    self._active_provider.value if self._active_provider else "llm",  # type: ignore
                    exc,
                )
                # Intentar failover
                result = self._try_failover(
                    bull_argument, bull_strength,
                    bear_argument, bear_strength,
                    risk,
                    context_str,
                )
                if result is not None:
                    used_provider = "failover"
                else:
                    logger.warning("Todos los LLMs fallaron, usando fallback heuristico")
                    result = self._deliberate_heuristic(
                        bull_strength, bear_strength, risk
                    )
        else:
            logger.warning("LLM no inicializado, usando fallback heuristico")
            result = self._deliberate_heuristic(
                bull_strength, bear_strength, risk
            )

        # ── Aplicar regla de confianza minima ─────────────────────────
        result = self._apply_confidence_gate(result)

        # ── Aplicar override de riesgo ────────────────────────────────
        result = self._apply_risk_override(result, risk)

        # ── Registrar debate ──────────────────────────────────────────
        self._log_debate(bull_state, bear_state, result, provider=used_provider, symbol=symbol)  # type: ignore

        logger.info(
            "Arbitro [%s]: %s (conf=%.2f, size=%.1f%%) | %s",
            used_provider,
            result["decision"],
            result["confidence"],
            result["position_size_pct"],
            result["reasoning"][:100],
        )

        # ── Guardar en Cache Global (WFO) ─────────────────────────────
        if dt_str:
            self._save_global_cache(dt_str, result)

        # ── Guardar en Cache si la decision fue HOLD ──────────────────
        if symbol != "UNKNOWN" and result["decision"] == "HOLD":
            self._decision_cache[symbol] = {
                "timestamp": time.time(),
                "price": current_price,
                "bull_strength": bull_strength,
                "bear_strength": bear_strength,
                "decision": result
            }

        return result

    # ══════════════════════════════════════════════════════════════════
    #  Rotacion de Cuentas y Deliberacion con LLM
    # ══════════════════════════════════════════════════════════════════

    def _rotate_api_key(self, provider: LLMProvider) -> bool:
        """Rota la API Key y reconstruye la cadena LLM activa si existen multiples tokens."""
        if provider == LLMProvider.GROQ and len(self._groq_keys) > 1:
            self._groq_idx = (self._groq_idx + 1) % len(self._groq_keys)
            key_preview = f"...{self._groq_keys[self._groq_idx][-4:]}" if self._groq_keys[self._groq_idx] else "???"
            logger.info("🔑 Rotando cuenta GROQ a slot %d (Token: %s)", self._groq_idx, key_preview)
        elif provider == LLMProvider.GEMINI and len(self._gemini_keys) > 1:
            self._gemini_idx = (self._gemini_idx + 1) % len(self._gemini_keys)
            key_preview = f"...{self._gemini_keys[self._gemini_idx][-4:]}" if self._gemini_keys[self._gemini_idx] else "???"
            logger.info("🔑 Rotando cuenta GEMINI a slot %d (Token: %s)", self._gemini_idx, key_preview)
        else:
            logger.warning("No hay cuentas alternativas para %s en el .env. Esperando enfriamiento forzoso (5s)...", provider.value)
            time.sleep(5)
            return True
            
        return self._init_provider(provider)

    def _deliberate_with_llm(
        self,
        bull_argument: str,
        bull_strength: float,
        bear_argument: str,
        bear_strength: float,
        risk: Dict[str, Any],
        context_str: str,
    ) -> Dict[str, Any]:
        """Invoca al LLM primario iterativamente para generar la decision y asegurar JSON valido.
           Implementa Failover dinamico de Cuentas (Rotacion de Token).
        """
        risk_summary = self._format_risk_summary(risk)
        last_error = ""

        max_attempts = max(3, len(self._groq_keys) if self._active_provider == LLMProvider.GROQ else len(self._gemini_keys))

        for attempt in range(max_attempts):
            try:
                # Modificar el prompt en tiempo de ejecucion si hay error de formato
                human_prompt = _USER_PROMPT_TEMPLATE
                if last_error and "rate" not in last_error.lower() and "429" not in last_error:
                    human_prompt += f"\n\n[SISTEMA]: TU RESPUESTA ANTERIOR GENERO ESTE ERROR: {last_error}. CORRIGE TU FORMATO A JSON EXCLUSIVAMENTE VALIDO O PERDEREMOS DINERO."

                chain = (
                    ChatPromptTemplate.from_messages([
                        ("system", _SYSTEM_PROMPT),
                        ("human", human_prompt),
                    ])
                    | self._llm
                    | StrOutputParser()
                )

                raw_response = chain.invoke({
                    "bull_strength": bull_strength,
                    "bull_argument": bull_argument,
                    "bear_strength": bear_strength,
                    "bear_argument": bear_argument,
                    "risk_summary": risk_summary,
                    "market_context": context_str,
                })

                return self._parse_llm_response(raw_response, raise_error=True)

            except Exception as exc:
                last_error = str(exc)
                logger.warning("Error LLM (intento %d/%d): %s", attempt + 1, max_attempts, exc)
                
                # Check for rate limiting to trigger rotation
                if "429" in last_error or "rate limit" in last_error.lower() or "quota" in last_error.lower():
                    logger.warning("⚠️ Límite de tokens detectado. Activando Rotador de Cuentas...")
                    provider_to_rotate = self._active_provider
                    if provider_to_rotate is not None:
                        self._rotate_api_key(provider_to_rotate)

        return self._hold_decision(f"Error parseo JSON y cuotas tras {max_attempts} reintentos y rotaciones: {last_error}")

    def _try_failover(
        self,
        bull_argument: str,
        bull_strength: float,
        bear_argument: str,
        bear_strength: float,
        risk: Dict[str, Any],
        context_str: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Intenta usar los proveedores secundarios en orden de failover.
        Retorna None si todos fallan.
        """
        risk_summary = self._format_risk_summary(risk)

        for provider, llm in self._fallback_llms.items():
            try:
                logger.info("Intentando failover con %s...", provider.value)

                chain = (
                    ChatPromptTemplate.from_messages([
                        ("system", _SYSTEM_PROMPT),
                        ("human", _USER_PROMPT_TEMPLATE),
                    ])
                    | llm
                    | StrOutputParser()
                )

                raw_response = chain.invoke({
                    "bull_strength": bull_strength,
                    "bull_argument": bull_argument,
                    "bear_strength": bear_strength,
                    "bear_argument": bear_argument,
                    "risk_summary": risk_summary,
                    "market_context": context_str,
                })

                result = self._parse_llm_response(raw_response)
                logger.info("Failover exitoso con %s", provider.value)
                return result

            except Exception as exc:
                logger.warning("Failover %s fallo: %s", provider.value, exc)
                continue

        return None

    def _parse_llm_response(self, raw: str, raise_error: bool = False) -> Dict[str, Any]:
        """Parsea la respuesta del LLM y valida la estructura."""
        try:
            # Intentar extraer JSON del response
            json_match = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
            else:
                data = json.loads(raw)

            # Validar campos requeridos
            decision = data.get("decision", "HOLD").upper()
            if decision not in ("BUY", "SELL", "HOLD"):
                decision = "HOLD"

            confidence = float(data.get("confidence", 0.0))
            confidence = max(0.0, min(1.0, confidence))

            position_size = float(data.get("position_size_pct", DEFAULT_POSITION_SIZE_PCT))
            position_size = max(0.0, min(MAX_POSITION_SIZE_PCT, position_size))

            reasoning = data.get("reasoning", "Sin razonamiento del LLM")

            return {
                "decision": decision,
                "confidence": round(confidence, 4),  # type: ignore
                "reasoning": reasoning,
                "position_size_pct": round(position_size, 2),  # type: ignore
            }

        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            if raise_error:
                raise ValueError(f"Data extract failed: {exc} | Raw block: {raw[:150]}")  # type: ignore
            logger.warning("Error parseando respuesta LLM: %s | Raw: %s", exc, raw[:200])  # type: ignore
            return self._hold_decision(f"Error parseando respuesta del LLM: {exc}")

    # ══════════════════════════════════════════════════════════════════
    #  Fallback heuristico (sin LLM)
    # ══════════════════════════════════════════════════════════════════

    def _deliberate_heuristic(
        self,
        bull_strength: float,
        bear_strength: float,
        risk: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Deliberacion basada en reglas cuando el LLM no esta disponible.
        Incluye desempate por VaR cuando bull y bear estan empatados.
        """
        diff = bull_strength - bear_strength
        max_strength = max(bull_strength, bear_strength)
        var_95 = risk.get("var_95", 0)

        # Calcular confianza base
        if max_strength == 0:
            confidence = 0.0
        else:
            confidence = abs(diff) / 10.0 * 0.7 + (max_strength / 10.0) * 0.3

        # ── Caso 1: Bull domina claramente ─────────────────────────────
        if diff >= 3 and bull_strength >= 6:
            decision = "BUY"
            reasoning = (
                f"Heuristico: Bull({bull_strength}) supera a Bear({bear_strength}) "
                f"por {diff:.1f} puntos con conviccion alta"
            )

        # ── Caso 2: Bear domina claramente ─────────────────────────────
        elif diff <= -3 and bear_strength >= 6:
            decision = "SELL"
            reasoning = (
                f"Heuristico: Bear({bear_strength}) supera a Bull({bull_strength}) "
                f"por {abs(diff):.1f} puntos con conviccion alta"
            )

        # ── Caso 3: Empate o diferencia <3 → desempate por VaR ────────
        elif abs(diff) < 3 and max_strength >= 5:
            if var_95 > 0.03:
                if bear_strength >= 5:
                    decision = "SELL"
                    confidence = confidence * 0.8
                    reasoning = (
                        f"Desempate por VaR: bull={bull_strength}, bear={bear_strength} "
                        f"(diff={diff:.1f}). VaR={var_95:.1%} alto -> favorece posicion defensiva"
                    )
                else:
                    decision = "HOLD"
                    confidence = 0.0
                    reasoning = (
                        f"Empate sin conviccion: bull={bull_strength}, bear={bear_strength}. "
                        f"VaR={var_95:.1%} alto -> sin operacion"
                    )
            elif var_95 <= 0.03 and bull_strength > bear_strength:
                decision = "BUY"
                confidence = confidence * 0.7
                reasoning = (
                    f"Desempate por VaR: bull={bull_strength} > bear={bear_strength} "
                    f"(diff={diff:.1f}). VaR={var_95:.1%} bajo -> permite posicion alcista"
                )
            else:
                decision = "HOLD"
                confidence = 0.0
                reasoning = (
                    f"Empate: bull={bull_strength}, bear={bear_strength} "
                    f"(diff={diff:.1f}). Sin senal clara -> HOLD"
                )
        else:
            decision = "HOLD"
            reasoning = (
                f"Heuristico: Sin diferencia clara "
                f"(bull={bull_strength}, bear={bear_strength}, diff={diff:.1f})"
            )
            confidence = 0.0

        # Calcular tamano de posicion proporcional a confianza
        position_size = min(
            confidence * 10.0,
            MAX_POSITION_SIZE_PCT,
        ) if decision != "HOLD" else 0.0

        return {
            "decision": decision,
            "confidence": round(confidence, 4),  # type: ignore
            "reasoning": reasoning,
            "position_size_pct": round(position_size, 2),  # type: ignore
        }

    # ══════════════════════════════════════════════════════════════════
    #  Validaciones y overrides
    # ══════════════════════════════════════════════════════════════════

    def _apply_confidence_gate(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Bloquea la ejecucion si la confianza es menor a 0.65."""
        if result["decision"] in ("BUY", "SELL"):
            if result["confidence"] < MIN_CONFIDENCE_TO_EXECUTE:
                logger.info(
                    "Confianza %.2f < %.2f -- forzando HOLD",
                    result["confidence"],
                    MIN_CONFIDENCE_TO_EXECUTE,
                )
                result = {
                    "decision": "HOLD",
                    "confidence": result["confidence"],
                    "reasoning": (
                        f"Decision original: {result['decision']} (conf={result['confidence']:.2f}). "
                        f"Bloqueada por umbral minimo de confianza ({MIN_CONFIDENCE_TO_EXECUTE}). "
                        f"Razon original: {result['reasoning']}"
                    ),
                    "position_size_pct": 0.0,
                }
        return result

    def _apply_risk_override(
        self, result: Dict[str, Any], risk: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Reduce o bloquea operaciones si el riesgo del portafolio es excesivo."""
        if result["decision"] == "HOLD":
            return result

        # Override por drawdown excesivo
        current_drawdown = risk.get("max_drawdown", 0)
        if current_drawdown > 0.10:
            logger.warning(
                "Drawdown %.1f%% > 10%% -- forzando HOLD",
                current_drawdown * 100,
            )
            return {
                "decision": "HOLD",
                "confidence": result["confidence"],
                "reasoning": (
                    f"Override de riesgo: Drawdown actual ({current_drawdown:.1%}) "
                    f"excede el limite del 10%. Decision original: {result['decision']}. "
                    f"{result['reasoning']}"
                ),
                "position_size_pct": 0.0,
            }

        # Override por exposicion excesiva
        exposure = risk.get("current_exposure", 0)
        if exposure > 0.80:
            logger.warning(
                "Exposicion %.1f%% > 80%% -- forzando HOLD",
                exposure * 100,
            )
            return {
                "decision": "HOLD",
                "confidence": result["confidence"],
                "reasoning": (
                    f"Override de riesgo: Exposicion actual ({exposure:.1%}) "
                    f"excede el limite del 80%. Decision original: {result['decision']}. "
                    f"{result['reasoning']}"
                ),
                "position_size_pct": 0.0,
            }

        # Reducir tamano si hay riesgo moderado
        var_95 = risk.get("var_95", 0)
        if var_95 > 0.05:
            original_size = result["position_size_pct"]
            reduced_size = round(original_size * 0.5, 2)
            logger.info(
                "VaR alto (%.1f%%) -- reduciendo posicion %.1f%% -> %.1f%%",
                var_95 * 100,
                original_size,
                reduced_size,
            )
            result["position_size_pct"] = reduced_size
            result["reasoning"] += f" (tamano reducido por VaR alto: {var_95:.1%})"

        return result

    # ══════════════════════════════════════════════════════════════════
    #  Helpers
    # ══════════════════════════════════════════════════════════════════

    def _hold_decision(self, reasoning: str) -> Dict[str, Any]:
        """Retorna una decision HOLD estandar."""
        return {
            "decision": "HOLD",
            "confidence": 0.0,
            "reasoning": reasoning,
            "position_size_pct": 0.0,
        }

    def _format_risk_summary(self, risk: Dict[str, Any]) -> str:
        """Formatea las metricas de riesgo para el prompt."""
        if not risk:
            return "No hay metricas de riesgo disponibles."

        lines = []
        if "max_drawdown" in risk:
            lines.append(f"- Drawdown maximo: {risk['max_drawdown']:.1%}")
        if "var_95" in risk:
            lines.append(f"- VaR (95%): {risk['var_95']:.1%}")
        if "current_exposure" in risk:
            lines.append(f"- Exposicion actual: {risk['current_exposure']:.1%}")
        if "sharpe_ratio" in risk:
            lines.append(f"- Sharpe Ratio: {risk['sharpe_ratio']:.2f}")
        if "sortino_ratio" in risk:
            lines.append(f"- Sortino Ratio: {risk['sortino_ratio']:.2f}")

        return "\n".join(lines) if lines else "Metricas de riesgo no disponibles."

    def _log_debate(
        self,
        bull_state: Dict[str, Any],
        bear_state: Dict[str, Any],
        result: Dict[str, Any],
        provider: str = "heuristic",
        symbol: str = "UNKNOWN",
    ) -> None:
        """Registra el debate en el historial."""
        log = DebateLog(
            bull_argument=bull_state.get("argument", ""),
            bull_strength=bull_state.get("strength", 0),
            bear_argument=bear_state.get("argument", ""),
            bear_strength=bear_state.get("strength", 0),
            arbitro_reasoning=result.get("reasoning", ""),
            final_decision=result.get("decision", "HOLD"),
            confidence=result.get("confidence", 0),
            provider=provider,
        )
        self._debate_history.append(log)

        # JSONL Export for Dashboard (Phase 6)
        try:
            qlogger = get_quant_logger()
            qlogger.log_agent_decision(
                agent_name="AgentArbitro",
                symbol=symbol,
                confidence=log.confidence,
                decision=log.final_decision,
                reasoning=log.arbitro_reasoning,
                metadata={
                    "bull_strength": log.bull_strength,
                    "bear_strength": log.bear_strength,
                    "provider": log.provider
                }
            )
        except Exception as e:
            logger.error("Error exportando telemetria: %s", e)

        # Mantener solo los ultimos 50 debates
        if len(self._debate_history) > 50:
            self._debate_history = self._debate_history[-50:]  # type: ignore

    # ══════════════════════════════════════════════════════════════════
    #  Consultas
    # ══════════════════════════════════════════════════════════════════

    def get_state(self) -> Dict[str, Any]:
        """Retorna el estado actual del arbitro."""
        return {
            "provider": self._active_provider.value if self._active_provider else "none",  # type: ignore
            "model": self.model_name,
            "initialized": self._initialized,
            "fallbacks": [p.value for p in self._fallback_llms.keys()],
            "debates": len(self._debate_history),
        }

    def get_debate_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Retorna el historial reciente de deliberaciones."""
        recent = self._debate_history[-limit:]  # type: ignore
        return [
            {
                "bull_strength": d.bull_strength,
                "bear_strength": d.bear_strength,
                "decision": d.final_decision,
                "confidence": d.confidence,
                "provider": d.provider,
                "reasoning": d.arbitro_reasoning[:200],
                "timestamp": d.timestamp.isoformat(),
            }
            for d in recent
        ]

    def explain_decision(self, result: Dict[str, Any]) -> str:
        """Genera una explicacion legible para reportes."""
        return (
            f"Decision: {result['decision']} | "
            f"Confianza: {result['confidence']:.0%} | "
            f"Tamano: {result['position_size_pct']:.1f}% del capital\n"
            f"Razonamiento: {result['reasoning']}"
        )

    def __repr__(self) -> str:
        status = "READY" if self._initialized else "NOT_INITIALIZED"
        provider = self._active_provider.value if self._active_provider else "none"  # type: ignore
        return (
            f"<AgentArbitro provider={provider} model={self.model_name} "
            f"status={status} debates={len(self._debate_history)}>"
        )
