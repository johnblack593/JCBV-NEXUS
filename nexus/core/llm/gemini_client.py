"""
NEXUS v5.0 — Gemini LLM Client (direct aiohttp REST)
======================================================
Async REST client for the Google Gemini API.
API reference: https://ai.google.dev/api/rest

Uses aiohttp for all HTTP transport. No SDK dependency.
"""

from __future__ import annotations

import logging
from typing import Optional

import aiohttp
from aiohttp_retry import ExponentialRetry, RetryClient

logger = logging.getLogger("nexus.llm.gemini")

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiClientError(Exception):
    """Raised on non-recoverable Gemini API failures."""
    pass


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

    async def _ensure_session(self) -> RetryClient:
        """Lazily initializes the aiohttp RetryClient on first call."""
        if self._session is None or self._session._client_session.closed:
            retry_opts = ExponentialRetry(
                attempts=3,
                start_timeout=1.0,
                factor=2.0,
                statuses={429, 503},
            )
            connector = aiohttp.TCPConnector(limit=5)
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
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": max_tokens,
            },
        }

        logger.debug("Gemini request: model=%s, max_tokens=%d", self._model, max_tokens)

        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                    logger.debug("Gemini response received (%d chars)", len(text))
                    return text
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
