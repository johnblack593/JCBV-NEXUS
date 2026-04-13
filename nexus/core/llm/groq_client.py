"""
NEXUS v5.0 — Groq LLM Client (direct aiohttp REST)
=====================================================
Async REST client for the Groq API.
API reference: https://console.groq.com/docs/openai

Uses aiohttp for all HTTP transport. No SDK dependency.
Handles authentication, retries (via aiohttp-retry), and
response parsing internally.
"""

from __future__ import annotations

import logging
from typing import Optional

import aiohttp
from aiohttp_retry import ExponentialRetry, RetryClient

logger = logging.getLogger("nexus.llm.groq")

_BASE_URL = "https://api.groq.com/openai/v1/chat/completions"


class GroqClientError(Exception):
    """Raised on non-recoverable Groq API failures."""
    pass


class GroqClient:
    """
    Async REST client for the Groq LLM API.

    Uses aiohttp for all HTTP transport. No SDK dependency.
    Handles authentication, retries (via aiohttp-retry), and
    response parsing internally.
    """

    def __init__(self, api_key: str, model: str, timeout_s: float = 8.0) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: Optional[RetryClient] = None

    async def _ensure_session(self) -> RetryClient:
        """Lazily initializes the aiohttp RetryClient on first call."""
        if self._session is None or self._session.closed:
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
        Sends a completion request to Groq.
        Returns the model's text response on success.
        Raises GroqClientError on non-recoverable failures.
        """
        session = await self._ensure_session()

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.1,
        }

        logger.debug("Groq request: model=%s, max_tokens=%d", self._model, max_tokens)

        try:
            async with session.post(_BASE_URL, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    text = data["choices"][0]["message"]["content"]
                    logger.debug("Groq response received (%d chars)", len(text))
                    return text
                else:
                    body = await resp.text()
                    logger.warning(
                        "Groq API error: status=%d, body=%s", resp.status, body[:200]
                    )
                    raise GroqClientError(
                        f"Groq API returned HTTP {resp.status}: {body[:200]}"
                    )
        except GroqClientError:
            raise
        except Exception as exc:
            logger.warning("Groq request failed: %s", exc)
            raise GroqClientError(f"Groq request failed: {exc}") from exc

    async def close(self) -> None:
        """Releases the aiohttp ClientSession."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
