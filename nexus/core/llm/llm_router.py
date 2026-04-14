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

logger = logging.getLogger("nexus.llm.router")


class LLMRouter:
    _instance: Optional["LLMRouter"] = None

    def __init__(self) -> None:
        load_dotenv()

        # Cooldown & Invalid tracking
        self._cooldowns: Dict[str, float] = {}  # {key_hash: timestamp_until}
        self._invalid_keys: Set[str] = set()    # {key_hash}
        self._cooldown_seconds = int(os.getenv("LLM_COOLDOWN_SECONDS", "3600"))

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

    def _parse_keys(self, env_plural: str, env_singular: str) -> list[str]:
        keys_str = os.getenv(env_plural, "").strip()
        if not keys_str:
            keys_str = os.getenv(env_singular, "").strip()
        
        if not keys_str:
            return []
            
        return [k.strip() for k in keys_str.split(",") if k.strip()]

    def _get_key_hash(self, key: str) -> str:
        return key[:8] if len(key) >= 8 else "HIDDEN"

    def _is_rate_limit(self, exc: Exception) -> bool:
        exc_str = str(exc).lower()
        
        # 429 status code implies rate limit
        if "429" in exc_str:
            return True
        
        rate_limit_keywords = [
            "rate_limit", "quota", "exhausted", "limit exceeded",
            "daily", "tokens per day", "too many requests"
        ]
        return any(kw in exc_str for kw in rate_limit_keywords)
        
    def _is_auth_error(self, exc: Exception) -> bool:
        exc_str = str(exc).lower()
        return "401" in exc_str or "403" in exc_str or "invalid api key" in exc_str or "unauthorized" in exc_str or "forbidden" in exc_str

    async def _try_provider(
        self, provider_name: str, keys: Deque[str], model: str, prompt: str, max_tokens: int
    ) -> str | None:
        tried_keys = 0
        total_keys = len(keys)
        
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

            # Try request
            client = None
            try:
                if provider_name == "groq":
                    client = GroqClient(api_key=key, model=model)
                else:
                    client = GeminiClient(api_key=key, model=model)
                    
                result = await client.complete(prompt, max_tokens=max_tokens)
                await client.close()
                return result
                
            except Exception as exc:
                if client:
                    await client.close()
                    
                if self._is_auth_error(exc):
                    self._invalid_keys.add(key_hash)
                    logger.error("Clave %s [%s] inválida — se descarta.", provider_name.capitalize(), key_hash)
                
                elif self._is_rate_limit(exc):
                    cooldown_until = time.time() + self._cooldown_seconds
                    self._cooldowns[key_hash] = cooldown_until
                    logger.warning("Clave %s [%s] en rate limit. Cooldown: %s min.", 
                                   provider_name.capitalize(), key_hash, self._cooldown_seconds // 60)
                    
                    try:
                        reporter = TelegramReporter.get_instance()
                        if reporter and hasattr(reporter, "fire_key_cooldown"):
                            reporter.fire_key_cooldown(provider_name.capitalize(), key_hash, self._cooldown_seconds // 60)
                    except Exception:
                        pass
                else:
                    logger.warning("LLMRouter: %s error inesperado con clave [%s] — %s", provider_name.capitalize(), key_hash, exc)
                    
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

        logger.error("LLMRouter: All providers failed. Returning None.")
        try:
            from nexus.reporting.telegram_reporter import TelegramReporter
            reporter = TelegramReporter.get_instance()
            if reporter and hasattr(reporter, "fire_llm_fallback"):
                reporter.fire_llm_fallback("Todos", "Limits exhausted or unavailable")
        except Exception:
            pass
            
        return None

    async def close(self) -> None:
        """Closes all active client sessions."""
        # The router now creates and closes clients per request, so no persistent sessions to close.
        # This prevents session bugs across multiple keys.
        pass
