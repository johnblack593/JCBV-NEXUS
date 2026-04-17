"""
NEXUS v5.0 — Gemini LLM Client (direct aiohttp REST)
======================================================
Async REST client for the Google Gemini API.
API reference: https://ai.google.dev/api/rest

Uses aiohttp for all HTTP transport. No SDK dependency.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import aiohttp
from aiohttp_retry import ExponentialRetry, RetryClient

logger = logging.getLogger("nexus.llm.gemini")

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiClientError(Exception):
    """Raised on non-recoverable Gemini API failures."""
    pass


class GeminiRateLimitError(GeminiClientError):
    """Raised when a specific API key is in cooling down."""
    pass


class GeminiRateLimiter:
    """
    Proactive rate limiting for Gemini free tier (2 RPM).
    Shared across all instances via class variables.
    """
    MIN_INTERVAL_S = 31  # 2 rpm -> 1 every 31s for safety
    _last_call: dict[str, float] = {}  # key_hash -> timestamp
    _cooling: dict[str, float] = {}    # key_hash -> timestamp until reactive

    @classmethod
    async def wait_if_needed(cls, api_key: str) -> None:
        key_hash = api_key[:8]
        now = time.time()
        
        # Check if cooling down
        if key_hash in cls._cooling:
            until = cls._cooling[key_hash]
            if now < until:
                wait_time = until - now
                logger.warning(f"Gemini key [{key_hash}] cooling down. Available in {wait_time:.1f}s")
                await asyncio.sleep(wait_time)
            else:
                del cls._cooling[key_hash]

        # Check interval
        if key_hash in cls._last_call:
            elapsed = now - cls._last_call[key_hash]
            if elapsed < cls.MIN_INTERVAL_S:
                wait_time = cls.MIN_INTERVAL_S - elapsed
                logger.debug(f"Gemini proactive rate limit: waiting {wait_time:.1f}s")
                await asyncio.sleep(wait_time)

    @classmethod
    def mark_call(cls, api_key: str) -> None:
        cls._last_call[api_key[:8]] = time.time()

    @classmethod
    def set_cooling(cls, api_key: str, seconds: int) -> None:
        cls._cooling[api_key[:8]] = time.time() + seconds


class GeminiClient:
    """
    Async REST client for the Google Gemini API.

    Uses aiohttp for all HTTP transport. No SDK dependency.
    Handles authentication, retries (via aiohttp-retry), and
    response parsing internally.
    """

    def __init__(self, api_key: str, model: str, timeout_s: float = 10.0) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: Optional[RetryClient] = None

    def set_model(self, model_name: str) -> None:
        """Actualiza el modelo activo. Llamado por ModelDiscoveryService."""
        self._model = model_name
        logger.info(f"[{self.__class__.__name__}] Modelo activo: {model_name}")

    async def _ensure_session(self) -> RetryClient:
        """Lazily initializes the aiohttp RetryClient on first call."""
        if self._session is None or self._session._client_session.closed:
            retry_opts = ExponentialRetry(
                attempts=3,
                start_timeout=1.0,
                factor=2.0,
                statuses={429, 503},
            )
            connector = aiohttp.TCPConnector(
                resolver=aiohttp.ThreadedResolver(),
                ttl_dns_cache=300,
                limit=5,
            )
            base_session = aiohttp.ClientSession(
                timeout=self._timeout,
                connector=connector,
            )
            self._session = RetryClient(
                client_session=base_session,
                retry_options=retry_opts,
            )
        return self._session

    async def complete(self, prompt: str, max_tokens: int = 256) -> str:
        """
        Sends a generateContent request to Gemini.
        Returns the model's text response on success.
        Raises GeminiClientError on non-recoverable failures.
        """
        session = await self._ensure_session()

        url = f"{_BASE_URL}/{self._model}:generateContent?key={self._api_key}"
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}]
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": max_tokens,
            },
        }

        logger.debug("Gemini request: model=%s, max_tokens=%d", self._model, max_tokens)

        # Proactive rate limit
        await GeminiRateLimiter.wait_if_needed(self._api_key)

        try:
            async with session.post(url, json=payload) as resp:
                GeminiRateLimiter.mark_call(self._api_key)

                if resp.status == 200:
                    data = await resp.json()
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                    logger.debug("Gemini response received (%d chars)", len(text))
                    return text
                elif resp.status == 429:
                    body = await resp.text()
                    logger.warning("Gemini rate limit (429) hit for key [%s]", self._api_key[:8])
                    # Signal cooling down for this key
                    GeminiRateLimiter.set_cooling(self._api_key, 60)
                    raise GeminiRateLimitError(f"Gemini 429: {body[:100]}")
                else:
                    body = await resp.text()
                    logger.warning(
                        "Gemini API error: status=%d, body=%s", resp.status, body[:200]
                    )
                    raise GeminiClientError(
                        f"Gemini API returned HTTP {resp.status}: {body[:200]}"
                    )
        except GeminiClientError:
            raise
        except Exception as exc:
            logger.warning("Gemini request failed: %s", exc)
            raise GeminiClientError(f"Gemini request failed: {exc}") from exc

    async def close(self) -> None:
        """Releases the aiohttp ClientSession."""
        if self._session:
            await self._session.close()
            self._session = None
