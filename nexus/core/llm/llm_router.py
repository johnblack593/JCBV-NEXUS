"""
NEXUS v5.0 — LLM Router con Rotación de Claves
=================================================
Facade sobre los clientes Groq y Gemini con rotación automática
de claves API cuando se alcanza el límite diario.

Estrategia de selección:
  - Primario:  Groq  (menor latencia, ideal para análisis intra-día)
  - Fallback:  Gemini (si Groq no está disponible o todas sus claves
               están en cooldown)
  - Sin LLM:   retorna None (el caller maneja degradación elegante)

Rotación de claves:
  - Lee listas separadas por comas desde el .env:
      GROQ_API_KEYS=key1,key2,key3
      GEMINI_API_KEYS=key1,key2
  - Al detectar error 429 / quota_exceeded, la clave actual entra
    en cooldown por LLM_COOLDOWN_SECONDS (por defecto: 3600 = 1 hora).
  - Se prueba la siguiente clave disponible automáticamente.
  - Si todas las claves de un proveedor están en cooldown,
    se cae al siguiente proveedor.

v5.0.1 — Clasificación precisa de errores:
  - Errores de infraestructura (DNS/TIMEOUT/TCP/SSL) NO invalidan claves.
  - Solo HTTP 401/403 invalida claves.
  - Solo HTTP 429 pone claves en cooldown.
  - HTTP 5xx mantiene la clave y pasa al siguiente proveedor.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from typing import Literal, Optional, Set, Dict, Deque

from dotenv import load_dotenv

from .groq_client import GroqClient
from .gemini_client import GeminiClient
from .diagnostics import (
    classify_provider_error,
    is_infrastructure_error,
    CATEGORY_HTTP_401,
    CATEGORY_HTTP_429,
    CATEGORY_HTTP_5XX,
)

logger = logging.getLogger("nexus.llm.router")


class LLMRouter:
    _instance: Optional["LLMRouter"] = None

    def __init__(self) -> None:
        load_dotenv()

        # Cooldown & Invalid tracking
        self._cooldowns: Dict[str, float] = {}  # {key_hash: timestamp_until}
        self._invalid_keys: Set[str] = set()    # {key_hash}
        self._cooldown_seconds = int(os.getenv("LLM_COOLDOWN_SECONDS", "3600"))

        # Last error tracking (for diagnostics)
        self._last_errors: Dict[str, Dict] = {}  # {provider_name: classify_provider_error result}

        # Keys
        self._groq_keys: Deque[str] = deque(self._parse_keys("GROQ_API_KEYS", "GROQ_API_KEY"))
        self._gemini_keys: Deque[str] = deque(self._parse_keys("GEMINI_API_KEYS", "GEMINI_API_KEY"))

        self._groq_model = os.getenv("GROQ_MODEL", "llama3-70b-8192").strip()
        self._gemini_model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()

        if not self._groq_keys and not self._gemini_keys:
            logger.warning(
                "LLMRouter: No LLM provider configured. "
                "Macro agent will use heuristic fallback only."
            )

    @classmethod
    def get_instance(cls) -> "LLMRouter":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_model(self, provider: str, model_name: str) -> None:
        """Actualiza el modelo activo para un proveedor."""
        if provider.lower() == "groq":
            self._groq_model = model_name
        elif provider.lower() == "gemini":
            self._gemini_model = model_name

    def _parse_keys(self, env_plural: str, env_singular: str) -> list[str]:
        keys_str = os.getenv(env_plural, "").strip()
        if not keys_str:
            keys_str = os.getenv(env_singular, "").strip()
        
        if not keys_str:
            return []
            
        return [k.strip() for k in keys_str.split(",") if k.strip()]

    def _get_key_hash(self, key: str) -> str:
        return key[:8] if len(key) >= 8 else "HIDDEN"

    async def _try_provider(
        self, provider_name: str, keys: Deque[str], model: str, prompt: str, max_tokens: int
    ) -> str | None:
        tried_keys = 0
        total_keys = len(keys)
        infra_failed = False  # Track if this provider has infra issues
        
        from nexus.reporting.telegram_reporter import TelegramReporter

        while tried_keys < total_keys:
            key = keys[0]
            key_hash = self._get_key_hash(key)
            
            # Rotate queue so we try next key if we loop, or just fair load balance
            keys.rotate(-1)
            tried_keys += 1

            if key_hash in self._invalid_keys:
                continue

            # Check cooldown
            if key_hash in self._cooldowns:
                if time.time() < self._cooldowns[key_hash]:
                    continue
                else:
                    # Cooldown expired
                    del self._cooldowns[key_hash]

            # If we already know infra is down for this provider, skip remaining keys
            if infra_failed:
                logger.debug(
                    "LLMRouter: Saltando clave [%s] de %s — infraestructura fallida.",
                    key_hash, provider_name.capitalize()
                )
                continue

            # Try request
            client = None
            try:
                if provider_name == "groq":
                    client = GroqClient(api_key=key, model=model)
                else:
                    client = GeminiClient(api_key=key, model=model)
                    
                result = await client.complete(prompt, max_tokens=max_tokens)
                await client.close()
                # Clear any previous error for this provider
                self._last_errors.pop(provider_name, None)
                return result
                
            except Exception as exc:
                if client:
                    await client.close()

                # Classify the error precisely
                diag = classify_provider_error(exc)
                category = diag["category"]
                self._last_errors[provider_name] = diag

                if is_infrastructure_error(category):
                    # Infrastructure error — NOT the key's fault
                    # Don't invalidate or cooldown the key
                    # Stop trying more keys for this provider (same infra will fail)
                    infra_failed = True
                    logger.warning(
                        "LLMRouter: %s error de infraestructura [%s] con clave [%s] — "
                        "NO se invalida la clave. Causa: %s",
                        provider_name.capitalize(), category,
                        key_hash, diag["human_message"]
                    )
                    # Telegram DEV: infra error notification
                    try:
                        reporter = TelegramReporter.get_instance()
                        if reporter and hasattr(reporter, "fire_llm_infra_error"):
                            reporter.fire_llm_infra_error(
                                provider=provider_name.capitalize(),
                                category=category,
                                detail=diag["human_message"],
                                key_hash=key_hash,
                                retryable=diag["retryable"],
                                action="Clave conservada, proveedor omitido temporalmente",
                            )
                    except Exception:
                        pass

                elif category == CATEGORY_HTTP_401:
                    # Auth error — invalidate this specific key
                    self._invalid_keys.add(key_hash)
                    logger.error(
                        "Clave %s [%s] inválida (HTTP 401) — se descarta permanentemente.",
                        provider_name.capitalize(), key_hash
                    )

                elif category == CATEGORY_HTTP_429:
                    # Rate limit — cooldown this specific key
                    cooldown_until = time.time() + self._cooldown_seconds
                    self._cooldowns[key_hash] = cooldown_until
                    logger.warning(
                        "Clave %s [%s] en rate limit (HTTP 429). Cooldown: %s min.",
                        provider_name.capitalize(), key_hash,
                        self._cooldown_seconds // 60
                    )
                    try:
                        reporter = TelegramReporter.get_instance()
                        if reporter and hasattr(reporter, "fire_key_cooldown"):
                            reporter.fire_key_cooldown(
                                provider_name.capitalize(), key_hash,
                                self._cooldown_seconds // 60
                            )
                    except Exception:
                        pass

                elif category == CATEGORY_HTTP_5XX:
                    # Server error — keep key valid, try next provider
                    logger.warning(
                        "LLMRouter: %s servidor devolvió 5xx con clave [%s] — "
                        "clave conservada, pasando al siguiente proveedor.",
                        provider_name.capitalize(), key_hash
                    )
                    # Don't try more keys for this provider (server is down)
                    infra_failed = True

                else:
                    # Unknown or BAD_REQUEST — log but keep key
                    logger.warning(
                        "LLMRouter: %s error [%s] con clave [%s] — %s",
                        provider_name.capitalize(), category,
                        key_hash, diag["raw_error"][:150]
                    )
                    
        return None

    async def complete(
        self,
        prompt: str,
        max_tokens: int = 256,
        prefer: Literal["groq", "gemini"] = "groq",
    ) -> str | None:
        """
        Routes the prompt to the preferred provider with fallback.
        Returns the response string, or None if all providers fail.
        Callers must handle None — never crash on LLM unavailability.
        """
        if prefer == "gemini":
            providers = [
                ("gemini", self._gemini_keys, self._gemini_model),
                ("groq", self._groq_keys, self._groq_model),
            ]
        else:
            providers = [
                ("groq", self._groq_keys, self._groq_model),
                ("gemini", self._gemini_keys, self._gemini_model),
            ]

        for name, keys, model in providers:
            if not keys:
                continue
                
            result = await self._try_provider(name, keys, model, prompt, max_tokens)
            if result is not None:
                return result
                
            logger.warning("LLMRouter: Todas las claves de %s fallaron o están agotadas.", name.capitalize())

        # All providers failed
        logger.error("LLMRouter: All providers failed. Returning None.")
        try:
            from nexus.reporting.telegram_reporter import TelegramReporter
            reporter = TelegramReporter.get_instance()
            # Build providers_tried info for detailed notification
            providers_tried = []
            for name, keys, _ in (providers if prefer != "gemini" else reversed(providers)):
                last_err = self._last_errors.get(name, {})
                providers_tried.append({
                    "name": name.capitalize(),
                    "keys_count": len(keys),
                    "error_type": last_err.get("human_message", "Sin información"),
                    "error_category": last_err.get("category", "UNKNOWN"),
                    "retryable": last_err.get("retryable", True),
                })
            if reporter and hasattr(reporter, "fire_llm_unavailable"):
                reporter.fire_llm_unavailable(providers_tried=providers_tried)
            elif reporter and hasattr(reporter, "fire_llm_fallback"):
                reporter.fire_llm_fallback("Todos", "Providers unavailable")
        except Exception:
            pass
            
        return None

    async def close(self) -> None:
        """Closes all active client sessions."""
        # The router now creates and closes clients per request, so no persistent sessions to close.
        # This prevents session bugs across multiple keys.
        pass
