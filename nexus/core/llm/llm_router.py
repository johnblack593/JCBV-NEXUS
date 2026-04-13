"""
NEXUS v5.0 — LLM Router (Groq + Gemini Facade)
=================================================
Facade over Groq and Gemini clients.

Selection strategy:
  - Primary: Groq (lower latency, suited for intra-day analysis)
  - Fallback: Gemini (if Groq unavailable or key not configured)
  - If both fail: returns None (caller handles graceful degradation)

Both clients are optional. The router initializes only the
clients whose API keys are present in the environment.

LLM providers: Groq (primary) + Gemini (fallback).
"""

from __future__ import annotations

import logging
import os
from typing import Literal, Optional

from dotenv import load_dotenv

from .groq_client import GroqClient, GroqClientError
from .gemini_client import GeminiClient, GeminiClientError

logger = logging.getLogger("nexus.llm.router")


class LLMRouter:
    """
    Facade over Groq and Gemini clients.

    Selection strategy:
      - Primary: Groq (lower latency, suited for intra-day analysis)
      - Fallback: Gemini (if Groq unavailable or key not configured)
      - If both fail: returns None (caller handles graceful degradation)

    Both clients are optional. The router initializes only the
    clients whose API keys are present in the environment.
    """

    _instance: Optional["LLMRouter"] = None

    def __init__(self) -> None:
        load_dotenv()

        self._groq: Optional[GroqClient] = None
        self._gemini: Optional[GeminiClient] = None

        # Groq
        groq_key = os.getenv("GROQ_API_KEY", "").strip()
        groq_model = os.getenv("GROQ_MODEL", "llama3-70b-8192").strip()
        if groq_key:
            self._groq = GroqClient(api_key=groq_key, model=groq_model)
            logger.info("LLMRouter: Groq client initialized (model=%s)", groq_model)

        # Gemini
        gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        gemini_model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()
        if gemini_key:
            self._gemini = GeminiClient(api_key=gemini_key, model=gemini_model)
            logger.info("LLMRouter: Gemini client initialized (model=%s)", gemini_model)

        if not self._groq and not self._gemini:
            logger.warning(
                "LLMRouter: No LLM provider configured. "
                "Macro agent will use heuristic fallback only."
            )

    @classmethod
    def get_instance(cls) -> "LLMRouter":
        """Returns the singleton LLMRouter instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

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
        # Build ordered client list based on preference
        if prefer == "gemini":
            clients = [
                ("gemini", self._gemini, GeminiClientError),
                ("groq", self._groq, GroqClientError),
            ]
        else:
            clients = [
                ("groq", self._groq, GroqClientError),
                ("gemini", self._gemini, GeminiClientError),
            ]

        for name, client, error_cls in clients:
            if client is None:
                continue
            try:
                result = await client.complete(prompt, max_tokens=max_tokens)
                return result
            except error_cls as exc:
                logger.warning("LLMRouter: %s failed — %s", name, exc)
            except Exception as exc:
                logger.warning("LLMRouter: %s unexpected error — %s", name, exc)

        logger.error("LLMRouter: All providers failed. Returning None.")
        return None

    async def close(self) -> None:
        """Closes all active client sessions."""
        if self._groq:
            await self._groq.close()
        if self._gemini:
            await self._gemini.close()
