"""
NEXUS v5.0 (beta) — Macro Agent (Layer 2: LLM Macro Filter)
=============================================================
Ejecuta clasificación macroeconómica como Background Task asíncrono.
NUNCA entra en el loop de trading intradía (Regla de Oro v5.0).

Responsabilidades:
    - CronJob asíncrono cada 1h (configurable).
    - Lee Fear & Greed Index vía API pública (alternative.me).
    - Llama al LLM (Groq/Gemini) vía aiohttp (direct REST) para clasificar régimen.
    - Escribe MACRO_REGIME = "GREEN" | "YELLOW" | "RED" en Redis atómicamente.
    - Persiste cada cambio de régimen en QuestDB para análisis histórico.

LLM providers: Groq (primary) + Gemini (fallback).
Failover: LLM Router → Heurístico puro si ambos fallan.

Si Redis no está disponible, mantiene el régimen en memoria local.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import aiohttp
from dotenv import load_dotenv

logger = logging.getLogger("nexus.macro")


# ══════════════════════════════════════════════════════════════════════
#  Macro Regime Enum
# ══════════════════════════════════════════════════════════════════════

class MacroRegime(Enum):
    """Regímenes macroeconómicos determinados por el MacroAgent."""
    GREEN = "GREEN"     # Normal operation — all systems go
    YELLOW = "YELLOW"   # Reduced exposure — tighten risk params
    RED = "RED"         # Full stop — no new trades (NFP, FED, Black Swan)


# ══════════════════════════════════════════════════════════════════════
#  LLM Regime Prompt
# ══════════════════════════════════════════════════════════════════════

_MACRO_SYSTEM_PROMPT = """Eres un analista macroeconómico institucional. Tu ÚNICO trabajo es clasificar
el régimen de mercado actual en una de tres categorías basándote en los datos proporcionados.

REGLAS ESTRICTAS:
1. Responde SIEMPRE en JSON válido, sin texto adicional.
2. El campo "regime" SOLO puede ser: "GREEN", "YELLOW" o "RED".
3. "GREEN" = Mercado normal, baja volatilidad, sin eventos macro pendientes.
4. "YELLOW" = Cautela, volatilidad moderada, eventos macro en 24h.
5. "RED" = STOP total, evento de alto impacto activo (NFP, FOMC, CPI, Black Swan).
6. El campo "reasoning" explica tu razonamiento en español (max 200 chars).
7. El campo "fear_greed_adjustment" es un número entre -20 y +20 (ajuste fino).

Formato JSON:
{{"regime": "GREEN|YELLOW|RED", "reasoning": "explicacion", "fear_greed_adjustment": 0}}"""

_MACRO_USER_PROMPT = """Analiza el estado macro actual y clasifica el régimen:

== FEAR & GREED INDEX ==
Score: {fear_greed_score}/100
Classification: {fear_greed_class}

== CONTEXTO TEMPORAL ==
Hora UTC: {utc_time}
Día de la semana: {day_of_week}

== HISTORIAL DE REGÍMENES (últimas 6h) ==
{regime_history}

Responde SOLO con el JSON de clasificación:"""


# ══════════════════════════════════════════════════════════════════════
#  Macro Agent
# ══════════════════════════════════════════════════════════════════════

class MacroAgent:
    """
    Agente de clasificación macroeconómica v4.0.

    Corre como asyncio.Task independiente del loop de trading.
    Lee datos macro, clasifica con LLM, y escribe régimen a Redis.

    Reemplaza al agent_arbitro.py como filtro macro (Layer 2).
    El agent_arbitro.py legacy sigue existiendo para deliberación
    Bull vs Bear (Layer 3 — será refactorizado en Fase 3).
    """

    # Fear & Greed API (alternative.me — free, no auth needed)
    _FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"

    # Regime thresholds (Fear & Greed 0-100)
    _RED_THRESHOLD = 20     # Extreme Fear → RED
    _YELLOW_THRESHOLD = 35  # Fear → YELLOW
    _GREEN_THRESHOLD = 35   # Above 35 → GREEN (default)

    def __init__(
        self,
        interval_hours: float = 1.0,
        redis_client=None,
        questdb_client=None,
        llm_router=None,
    ) -> None:
        load_dotenv()
        self.interval_hours = interval_hours
        self.redis = redis_client
        self.questdb = questdb_client
        self._running = False
        self._current_regime = MacroRegime.GREEN
        self._task: Optional[asyncio.Task] = None
        self._regime_history: List[Dict[str, Any]] = []
        self._last_fear_greed: Dict[str, Any] = {}

        # ── FORCE_MACRO_REGIME override for testing ──────────────────
        self._force_regime: Optional[str] = os.getenv("FORCE_MACRO_REGIME", "").upper().strip() or None
        if self._force_regime and self._force_regime in ("GREEN", "YELLOW", "RED"):
            logger.warning(
                f"⚠️ FORCE_MACRO_REGIME={self._force_regime} — "
                f"Macro evaluation BYPASSED. Testing mode active."
            )
            self._current_regime = MacroRegime(self._force_regime)
        elif self._force_regime:
            logger.error(f"Invalid FORCE_MACRO_REGIME='{self._force_regime}'. Ignoring.")
            self._force_regime = None

        # LLM Router (injected or self-initialized)
        if llm_router is not None:
            self._llm_router = llm_router
        else:
            from nexus.core.llm.llm_router import LLMRouter
            self._llm_router = LLMRouter.get_instance()

        # Multi-key rotation (imported from settings)
        self._groq_keys: List[str] = []
        self._gemini_keys: List[str] = []
        self._groq_idx = 0
        self._gemini_idx = 0
        self._load_api_keys()

    def _load_api_keys(self) -> None:
        """Carga las API keys de LLM desde settings."""
        try:
            from nexus.config.settings import GROQ_API_KEYS, GOOGLE_API_KEYS
            self._groq_keys = GROQ_API_KEYS
            self._gemini_keys = GOOGLE_API_KEYS
        except ImportError:
            # Fallback directo al .env
            groq_raw = os.getenv("GROQ_API_KEYS", "")
            self._groq_keys = [k.strip() for k in groq_raw.split(",") if k.strip()]
            gemini_raw = os.getenv("GOOGLE_API_KEYS", "")
            self._gemini_keys = [k.strip() for k in gemini_raw.split(",") if k.strip()]

    @property
    def current_regime(self) -> MacroRegime:
        return self._current_regime

    # ══════════════════════════════════════════════════════════════════
    #  Lifecycle
    # ══════════════════════════════════════════════════════════════════

    async def start(self) -> None:
        """Inicia el CronJob asíncrono del macro agent."""
        if self._running:
            logger.warning("MacroAgent ya está corriendo.")
            return
        self._running = True
        # Run first evaluation immediately, then schedule
        self._task = asyncio.create_task(self._cron_loop())
        logger.info(
            f"🌐 MacroAgent v4.0 iniciado — Intervalo: {self.interval_hours}h "
            f"| Provider: {self._llm_provider}"
        )

    async def stop(self) -> None:
        """Detiene el CronJob."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("🌐 MacroAgent detenido.")

    async def _cron_loop(self) -> None:
        """Loop principal del macro agent."""
        while self._running:
            try:
                await self._evaluate_regime()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"MacroAgent cron error: {e}", exc_info=True)

            # Sleep until next evaluation
            await asyncio.sleep(self.interval_hours * 3600)

    # ══════════════════════════════════════════════════════════════════
    #  Core Evaluation Pipeline
    # ══════════════════════════════════════════════════════════════════

    async def _evaluate_regime(self) -> None:
        """Pipeline completo de evaluación del régimen macro."""
        # ── FORCE_MACRO_REGIME bypass ─────────────────────────────────
        if self._force_regime:
            forced = MacroRegime(self._force_regime)
            self._current_regime = forced
            await self._write_regime_to_redis(forced)
            logger.debug(f"FORCE_MACRO_REGIME={self._force_regime} written to Redis")
            return

        t_start = time.perf_counter()

        # Step 1: Fetch Fear & Greed Index
        fear_greed = await self._fetch_fear_greed()
        score = fear_greed.get("score", 50)
        classification = fear_greed.get("classification", "Neutral")

        # Step 2: Determine regime (heuristic first, LLM refines)
        heuristic_regime = self._heuristic_regime(score)

        # Step 3: Try LLM refinement (non-blocking, with timeout)
        llm_regime = await self._llm_classify(score, classification)

        # Step 4: Final regime = LLM if available, else heuristic
        if llm_regime:
            final_regime = llm_regime["regime"]
            reasoning = llm_regime.get("reasoning", "LLM classification")
        else:
            final_regime = heuristic_regime
            reasoning = f"Heuristic: F&G={score} ({classification})"

        new_regime = MacroRegime(final_regime)
        old_regime = self._current_regime

        # Step 5: Update state
        self._current_regime = new_regime
        self._last_fear_greed = fear_greed

        # Step 6: Write to Redis atomically
        await self._write_regime_to_redis(new_regime)

        # Step 7: Persist to QuestDB
        await self._persist_regime(new_regime, self._llm_provider, reasoning, score)

        # Step 8: Track history
        self._regime_history.append({
            "regime": new_regime.value,
            "score": score,
            "reasoning": reasoning,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        # Keep last 24 entries (24h at 1h interval)
        if len(self._regime_history) > 24:
            self._regime_history = self._regime_history[-24:]

        elapsed = (time.perf_counter() - t_start) * 1000

        # Step 9: Log regime change
        if new_regime != old_regime:
            logger.warning(
                f"🌐 MACRO REGIME CHANGE: {old_regime.value} → {new_regime.value} "
                f"| F&G: {score} ({classification}) | {reasoning} | {elapsed:.0f}ms"
            )
            # Telegram: Macro Shift alert (fire-and-forget)
            try:
                from nexus.reporting.telegram_reporter import TelegramReporter
                TelegramReporter.get_instance().fire_macro_shift(
                    old_regime.value, new_regime.value, reasoning
                )
            except Exception:
                pass  # Telegram failure never blocks macro evaluation
        else:
            logger.info(
                f"🌐 MacroAgent tick: {new_regime.value} "
                f"| F&G: {score} ({classification}) | {elapsed:.0f}ms"
            )

    def _heuristic_regime(self, fear_greed_score: int) -> str:
        """Clasificación heurística basada en el Fear & Greed Index."""
        if fear_greed_score <= self._RED_THRESHOLD:
            return "RED"
        elif fear_greed_score <= self._YELLOW_THRESHOLD:
            return "YELLOW"
        else:
            return "GREEN"

    # ══════════════════════════════════════════════════════════════════
    #  Data Fetching
    # ══════════════════════════════════════════════════════════════════

    async def _fetch_fear_greed(self) -> Dict[str, Any]:
        """Fetch Fear & Greed Index from alternative.me API."""
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                async with session.get(self._FEAR_GREED_URL) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        fng = data.get("data", [{}])[0]
                        return {
                            "score": int(fng.get("value", 50)),
                            "classification": fng.get("value_classification", "Neutral"),
                            "timestamp": fng.get("timestamp", ""),
                        }
        except Exception as exc:
            logger.warning(f"Fear & Greed fetch failed: {exc}")

        # Fallback: neutral
        return {"score": 50, "classification": "Neutral", "timestamp": ""}

    # ══════════════════════════════════════════════════════════════════
    #  LLM Classification (with failover)
    # ══════════════════════════════════════════════════════════════════

    async def _llm_classify(
        self, score: int, classification: str
    ) -> Optional[Dict[str, Any]]:
        """
        Llama al LLM para refinar la clasificación del régimen.
        Timeout estricto de 15s. Si falla, retorna None (heuristic fallback).
        """
        # Try Groq first, then Gemini
        providers = []
        if self._groq_keys:
            providers.append(("groq", self._groq_keys, self._groq_idx))
        if self._gemini_keys:
            providers.append(("gemini", self._gemini_keys, self._gemini_idx))

        for provider_name, keys, idx in providers:
            if not keys:
                continue

            api_key = keys[idx % len(keys)]

            try:
                result = await asyncio.wait_for(
                    self._call_llm(provider_name, api_key, score, classification),
                    timeout=15.0,
                )
                if result:
                    return result
            except asyncio.TimeoutError:
                logger.warning(f"MacroAgent LLM timeout ({provider_name}, 15s)")
            except Exception as exc:
                logger.warning(f"MacroAgent LLM error ({provider_name}): {exc}")

                # Rotate key on rate limit
                if any(kw in str(exc).lower() for kw in ["429", "rate", "quota"]):
                    if provider_name == "groq":
                        self._groq_idx = (self._groq_idx + 1) % len(self._groq_keys)
                    else:
                        self._gemini_idx = (self._gemini_idx + 1) % len(self._gemini_keys)

        return None  # All providers failed → heuristic

    async def _call_llm(
        self,
        provider: str,
        api_key: str,
        score: int,
        classification: str,
    ) -> Optional[Dict[str, Any]]:
        """Invoca un LLM provider vía LLMRouter (aiohttp direct REST)."""
        # Build the user prompt
        history_lines = []
        for entry in self._regime_history[-6:]:
            history_lines.append(
                f"  {entry['timestamp']}: {entry['regime']} (F&G={entry['score']})"
            )
        history_str = "\n".join(history_lines) if history_lines else "  Sin historial previo"

        now = datetime.now(timezone.utc)

        full_prompt = (
            f"{_MACRO_SYSTEM_PROMPT}\n\n"
            + _MACRO_USER_PROMPT.format(
                fear_greed_score=score,
                fear_greed_class=classification,
                utc_time=now.strftime("%Y-%m-%d %H:%M UTC"),
                day_of_week=now.strftime("%A"),
                regime_history=history_str,
            )
        )

        response = await self._llm_router.complete(full_prompt, max_tokens=300)
        if response is None:
            logger.debug("LLM unavailable, returning None")
            return None

        return self._parse_llm_regime(response)

    def _parse_llm_regime(self, raw: str) -> Optional[Dict[str, Any]]:
        """Parsea la respuesta JSON del LLM."""
        try:
            json_match = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
            else:
                data = json.loads(raw)

            regime = data.get("regime", "GREEN").upper()
            if regime not in ("GREEN", "YELLOW", "RED"):
                regime = "GREEN"

            return {
                "regime": regime,
                "reasoning": data.get("reasoning", "LLM classification")[:200],
                "fear_greed_adjustment": float(data.get("fear_greed_adjustment", 0)),
            }
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(f"MacroAgent LLM parse error: {exc} | Raw: {raw[:100]}")
            return None

    # ══════════════════════════════════════════════════════════════════
    #  Redis State Management
    # ══════════════════════════════════════════════════════════════════

    async def _write_regime_to_redis(self, regime: MacroRegime) -> None:
        """Escribe MACRO_REGIME a Redis atómicamente (NEXUS: prefix)."""
        if not self.redis:
            return

        try:
            # Write regime value (NEXUS: prefix — shared contract with dashboard)
            await asyncio.to_thread(
                self.redis.set, "NEXUS:MACRO_REGIME", regime.value
            )
            # Write timestamp
            await asyncio.to_thread(
                self.redis.set,
                "NEXUS:MACRO_REGIME_UPDATED",
                datetime.now(timezone.utc).isoformat(),
            )
            logger.debug(f"Redis NEXUS:MACRO_REGIME = {regime.value}")
        except Exception as exc:
            logger.error(f"Error writing regime to Redis: {exc}")

    async def get_regime_from_redis(self) -> MacroRegime:
        """Lee MACRO_REGIME desde Redis. Respeta FORCE_MACRO_REGIME."""
        # Force override takes absolute priority
        if self._force_regime:
            return MacroRegime(self._force_regime)

        if not self.redis:
            return self._current_regime

        try:
            raw = await asyncio.to_thread(self.redis.get, "NEXUS:MACRO_REGIME")
            if raw:
                value = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                return MacroRegime(value)
        except Exception as exc:
            logger.warning(f"Error reading regime from Redis: {exc}")

        return self._current_regime

    # ══════════════════════════════════════════════════════════════════
    #  QuestDB Persistence
    # ══════════════════════════════════════════════════════════════════

    async def _persist_regime(
        self, regime: MacroRegime, provider: str, reasoning: str, score: float
    ) -> None:
        """Persiste el cambio de régimen en QuestDB para análisis histórico."""
        if not self.questdb:
            return

        try:
            await self.questdb.ingest_macro_regime(
                regime=regime.value,
                provider=provider,
                reasoning=reasoning,
                fear_greed_score=float(score),
            )
        except Exception as exc:
            logger.debug(f"QuestDB regime persistence failed: {exc}")

    # ══════════════════════════════════════════════════════════════════
    #  Public API
    # ══════════════════════════════════════════════════════════════════

    def get_state(self) -> Dict[str, Any]:
        """Retorna el estado actual del macro agent."""
        return {
            "regime": self._current_regime.value,
            "fear_greed": self._last_fear_greed,
            "running": self._running,
            "history_len": len(self._regime_history),
            "interval_hours": self.interval_hours,
        }

    def get_regime_history(self, limit: int = 12) -> List[Dict[str, Any]]:
        """Retorna el historial reciente de regímenes."""
        return self._regime_history[-limit:]

    def __repr__(self) -> str:
        status = "RUNNING" if self._running else "STOPPED"
        return (
            f"<MacroAgent regime={self._current_regime.value} "
            f"status={status}>"
        )
