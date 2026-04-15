"""
NEXUS v5.0 — LLM Diagnostics (Error Classification + Preflight)
=================================================================
Módulo compartido de clasificación de errores y diagnóstico de conectividad
para los proveedores LLM (Groq, Gemini).

Centraliza la lógica para que:
 - Un error DNS NUNCA se clasifique como "límite agotado"
 - Una clave API NO sea invalidada por un fallo de red
 - El reporte sea preciso y accionable en logs y Telegram DEV

Categorías de error:
    DNS       — DNS resolution failed
    TIMEOUT   — Request timed out
    TCP       — TCP connection refused / unreachable
    SSL       — TLS/SSL handshake or certificate error
    HTTP_401  — Authentication error (invalid key)
    HTTP_429  — Rate limit / quota exhausted
    HTTP_5XX  — Provider server error
    BAD_REQUEST — 400 / invalid payload / malformed
    UNKNOWN   — Anything else
"""

from __future__ import annotations

import logging
import re
import socket
import time
from typing import Any, Dict, Optional

import aiohttp
import asyncio

logger = logging.getLogger("nexus.llm.diagnostics")


# ══════════════════════════════════════════════════════════════════════
#  Error Category Constants
# ══════════════════════════════════════════════════════════════════════

CATEGORY_DNS = "DNS"
CATEGORY_TIMEOUT = "TIMEOUT"
CATEGORY_TCP = "TCP"
CATEGORY_SSL = "SSL"
CATEGORY_HTTP_401 = "HTTP_401"
CATEGORY_HTTP_429 = "HTTP_429"
CATEGORY_HTTP_5XX = "HTTP_5XX"
CATEGORY_BAD_REQUEST = "BAD_REQUEST"
CATEGORY_UNKNOWN = "UNKNOWN"

# Categories that indicate infrastructure problems (NOT key problems)
INFRA_CATEGORIES = frozenset({
    CATEGORY_DNS, CATEGORY_TIMEOUT, CATEGORY_TCP, CATEGORY_SSL,
})

# Categories where the key should be invalidated or cooled down
KEY_CATEGORIES = frozenset({
    CATEGORY_HTTP_401, CATEGORY_HTTP_429,
})

# Human-readable messages in Spanish
_HUMAN_MESSAGES = {
    CATEGORY_DNS: "Error de resolución DNS — no se puede contactar el servidor",
    CATEGORY_TIMEOUT: "Tiempo de espera agotado — el servidor no respondió",
    CATEGORY_TCP: "Error de conexión TCP — servidor inalcanzable",
    CATEGORY_SSL: "Error de certificado SSL/TLS — handshake fallido",
    CATEGORY_HTTP_401: "Clave API inválida o no autorizada",
    CATEGORY_HTTP_429: "Límite de solicitudes alcanzado (rate limit)",
    CATEGORY_HTTP_5XX: "Error del servidor del proveedor (5xx)",
    CATEGORY_BAD_REQUEST: "Solicitud inválida — payload o modelo incorrecto",
    CATEGORY_UNKNOWN: "Error desconocido",
}

# Suggested actions in Spanish
_SUGGESTED_ACTIONS = {
    CATEGORY_DNS: "Verificar conexión a internet, DNS del servidor, firewall, VPN/proxy",
    CATEGORY_TIMEOUT: "Verificar latencia de red y estado del proveedor",
    CATEGORY_TCP: "Verificar que el servidor puede alcanzar el endpoint del proveedor",
    CATEGORY_SSL: "Verificar certificados SSL y fecha/hora del sistema",
    CATEGORY_HTTP_401: "Verificar claves API en el archivo .env",
    CATEGORY_HTTP_429: "Esperar renovación de cuota o rotar a claves de respaldo",
    CATEGORY_HTTP_5XX: "El proveedor tiene problemas temporales, reintentar más tarde",
    CATEGORY_BAD_REQUEST: "Verificar modelo configurado y formato del prompt",
    CATEGORY_UNKNOWN: "Revisar logs para detalles del error",
}


# ══════════════════════════════════════════════════════════════════════
#  classify_provider_error
# ══════════════════════════════════════════════════════════════════════

def classify_provider_error(
    exc: Exception | str,
    status_code: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Clasifica un error de proveedor LLM en una categoría estandarizada.

    Args:
        exc: La excepción o mensaje de error a clasificar.
        status_code: Código HTTP si está disponible.

    Returns:
        Dict con estructura uniforme:
        {
            "category": str,
            "human_message": str,
            "raw_error": str,
            "retryable": bool,
            "suggested_action": str,
        }
    """
    raw = str(exc)
    lower = raw.lower()

    category = CATEGORY_UNKNOWN

    # ── 1. DNS errors ─────────────────────────────────────────────────
    dns_patterns = [
        "name or service not known",
        "temporary failure in name resolution",
        "nodename nor servname provided",
        "could not contact dns",
        "getaddrinfo failed",
        "name resolution",
        "dns",
    ]
    if isinstance(exc, socket.gaierror) or any(p in lower for p in dns_patterns):
        category = CATEGORY_DNS

    # ── 2. Timeout errors ─────────────────────────────────────────────
    elif isinstance(exc, (asyncio.TimeoutError,)):
        category = CATEGORY_TIMEOUT
    elif any(p in lower for p in ["timeout", "timed out", "timeouterror"]):
        category = CATEGORY_TIMEOUT

    # ── 3. TCP connection errors ──────────────────────────────────────
    elif isinstance(exc, (ConnectionRefusedError, ConnectionResetError, OSError)):
        if "ssl" not in lower and "certificate" not in lower:
            category = CATEGORY_TCP
        else:
            category = CATEGORY_SSL
    elif any(p in lower for p in [
        "connection refused", "network unreachable", "cannot connect to host",
        "connect call failed", "network is unreachable",
    ]):
        category = CATEGORY_TCP

    # ── 4. SSL/TLS errors ────────────────────────────────────────────
    elif any(p in lower for p in [
        "certificate verify failed", "ssl", "tls", "handshake",
    ]):
        category = CATEGORY_SSL

    # ── 5. HTTP status code based ─────────────────────────────────────
    elif status_code is not None:
        if status_code == 401 or status_code == 403:
            category = CATEGORY_HTTP_401
        elif status_code == 429:
            category = CATEGORY_HTTP_429
        elif 500 <= status_code < 600:
            category = CATEGORY_HTTP_5XX
        elif status_code == 400:
            category = CATEGORY_BAD_REQUEST
    else:
        # Try to extract HTTP status from error message
        if "401" in raw or "unauthorized" in lower or "invalid api key" in lower:
            category = CATEGORY_HTTP_401
        elif "403" in raw or "forbidden" in lower:
            category = CATEGORY_HTTP_401
        elif "429" in raw or any(p in lower for p in [
            "rate_limit", "quota", "exhausted", "too many requests",
            "limit exceeded", "tokens per day",
        ]):
            category = CATEGORY_HTTP_429
        elif any(f"http {c}" in lower or f"returned {c}" in lower or f"status={c}" in lower
               for c in ["500", "502", "503", "504"]):
            category = CATEGORY_HTTP_5XX
        elif "400" in raw or "invalid payload" in lower or "malformed" in lower or "invalid model" in lower:
            category = CATEGORY_BAD_REQUEST

    # Determine retryability
    retryable = category in {
        CATEGORY_DNS, CATEGORY_TIMEOUT, CATEGORY_TCP, CATEGORY_SSL, CATEGORY_HTTP_5XX,
    }

    return {
        "category": category,
        "human_message": _HUMAN_MESSAGES.get(category, _HUMAN_MESSAGES[CATEGORY_UNKNOWN]),
        "raw_error": raw[:500],
        "retryable": retryable,
        "suggested_action": _SUGGESTED_ACTIONS.get(category, _SUGGESTED_ACTIONS[CATEGORY_UNKNOWN]),
    }


def is_infrastructure_error(category: str) -> bool:
    """Retorna True si el error es de infraestructura (no culpa de la clave API)."""
    return category in INFRA_CATEGORIES


def is_key_error(category: str) -> bool:
    """Retorna True si el error es culpa de la clave API."""
    return category in KEY_CATEGORIES


# ══════════════════════════════════════════════════════════════════════
#  Preflight Connectivity Checks
# ══════════════════════════════════════════════════════════════════════

async def preflight_groq(
    api_key: str,
    model: str = "llama3-70b-8192",
    timeout_s: float = 6.0,
) -> Dict[str, Any]:
    """
    Prueba de conectividad liviana para Groq.
    Hace un request mínimo (1 token) para verificar DNS, TCP, auth, y latencia.

    Returns:
        {
            "provider": "Groq",
            "endpoint": str,
            "ok": bool,
            "dns_ok": bool,
            "tcp_ok": bool,
            "auth_ok": bool | None,
            "latency_ms": float | None,
            "error_category": str | None,
            "error_detail": str | None,
            "retryable": bool,
        }
    """
    endpoint = "https://api.groq.com/openai/v1/chat/completions"
    result = {
        "provider": "Groq",
        "endpoint": endpoint,
        "ok": False,
        "dns_ok": False,
        "tcp_ok": False,
        "auth_ok": None,
        "latency_ms": None,
        "error_category": None,
        "error_detail": None,
        "retryable": True,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0.0,
    }

    t_start = time.perf_counter()

    try:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            result["dns_ok"] = True  # If we get past connect, DNS was ok
            async with session.post(endpoint, json=payload, headers=headers) as resp:
                result["tcp_ok"] = True
                elapsed = (time.perf_counter() - t_start) * 1000
                result["latency_ms"] = round(elapsed, 1)

                if resp.status == 200:
                    result["ok"] = True
                    result["auth_ok"] = True
                    return result

                body = await resp.text()
                diag = classify_provider_error(
                    Exception(f"HTTP {resp.status}: {body[:200]}"),
                    status_code=resp.status,
                )
                result["error_category"] = diag["category"]
                result["error_detail"] = diag["human_message"]
                result["retryable"] = diag["retryable"]

                if resp.status in (401, 403):
                    result["auth_ok"] = False
                elif resp.status == 429:
                    result["auth_ok"] = True  # Key is valid, just rate limited

    except Exception as exc:
        elapsed = (time.perf_counter() - t_start) * 1000
        result["latency_ms"] = round(elapsed, 1)
        diag = classify_provider_error(exc)
        result["error_category"] = diag["category"]
        result["error_detail"] = diag["human_message"]
        result["retryable"] = diag["retryable"]

        if diag["category"] == CATEGORY_DNS:
            result["dns_ok"] = False
        elif diag["category"] in (CATEGORY_TCP, CATEGORY_SSL):
            result["dns_ok"] = True  # DNS resolved but connection failed
            result["tcp_ok"] = False

    return result


async def preflight_gemini(
    api_key: str,
    model: str = "gemini-1.5-flash",
    timeout_s: float = 6.0,
) -> Dict[str, Any]:
    """
    Prueba de conectividad liviana para Gemini.
    Hace un request mínimo (1 token) para verificar DNS, TCP, auth, y latencia.
    """
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    result = {
        "provider": "Gemini",
        "endpoint": endpoint,
        "ok": False,
        "dns_ok": False,
        "tcp_ok": False,
        "auth_ok": None,
        "latency_ms": None,
        "error_category": None,
        "error_detail": None,
        "retryable": True,
    }

    url = f"{endpoint}?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": "ping"}]}],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 1,
        },
    }

    t_start = time.perf_counter()

    try:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            result["dns_ok"] = True
            async with session.post(url, json=payload) as resp:
                result["tcp_ok"] = True
                elapsed = (time.perf_counter() - t_start) * 1000
                result["latency_ms"] = round(elapsed, 1)

                if resp.status == 200:
                    result["ok"] = True
                    result["auth_ok"] = True
                    return result

                body = await resp.text()
                diag = classify_provider_error(
                    Exception(f"HTTP {resp.status}: {body[:200]}"),
                    status_code=resp.status,
                )
                result["error_category"] = diag["category"]
                result["error_detail"] = diag["human_message"]
                result["retryable"] = diag["retryable"]

                if resp.status in (400,) and "api key not valid" in body.lower():
                    result["auth_ok"] = False
                    result["error_category"] = CATEGORY_HTTP_401
                    result["error_detail"] = _HUMAN_MESSAGES[CATEGORY_HTTP_401]
                elif resp.status in (401, 403):
                    result["auth_ok"] = False
                elif resp.status == 429:
                    result["auth_ok"] = True

    except Exception as exc:
        elapsed = (time.perf_counter() - t_start) * 1000
        result["latency_ms"] = round(elapsed, 1)
        diag = classify_provider_error(exc)
        result["error_category"] = diag["category"]
        result["error_detail"] = diag["human_message"]
        result["retryable"] = diag["retryable"]

        if diag["category"] == CATEGORY_DNS:
            result["dns_ok"] = False
        elif diag["category"] in (CATEGORY_TCP, CATEGORY_SSL):
            result["dns_ok"] = True
            result["tcp_ok"] = False

    return result


async def run_preflight_all(
    groq_keys: list[str],
    gemini_keys: list[str],
    groq_model: str = "llama3-70b-8192",
    gemini_model: str = "gemini-1.5-flash",
) -> Dict[str, Any]:
    """
    Ejecuta preflight para todos los proveedores y claves configurados.
    Retorna un snapshot estructurado listo para infrastructure_report.

    Returns:
        {
            "Groq": {
                "status": "ok|error",
                "keys_tried": [...],
                "error_type": str,
                "error_category": str,
                "latency_ms": float | None,
                "endpoint": str,
                "retryable": bool,
                "details": [...]  # per-key results
            },
            "Gemini": { ... }
        }
    """
    snapshot: Dict[str, Any] = {}

    # ── Groq ──────────────────────────────────────────────────────────
    if groq_keys:
        groq_details = []
        best_result = None
        for key in groq_keys:
            check = await preflight_groq(key, model=groq_model)
            groq_details.append(check)
            if check["ok"] and best_result is None:
                best_result = check
                break  # One success is enough

        first = groq_details[0] if groq_details else {}
        effective = best_result or first

        snapshot["Groq"] = {
            "status": "ok" if effective.get("ok") else "error",
            "keys_tried": [k[:8] for k in groq_keys[:len(groq_details)]],
            "error_type": effective.get("error_detail", ""),
            "error_category": effective.get("error_category", ""),
            "latency_ms": effective.get("latency_ms"),
            "endpoint": effective.get("endpoint", ""),
            "retryable": effective.get("retryable", True),
            "details": groq_details,
        }

    # ── Gemini ────────────────────────────────────────────────────────
    if gemini_keys:
        gemini_details = []
        best_result = None
        for key in gemini_keys:
            check = await preflight_gemini(key, model=gemini_model)
            gemini_details.append(check)
            if check["ok"] and best_result is None:
                best_result = check
                break

        first = gemini_details[0] if gemini_details else {}
        effective = best_result or first

        snapshot["Gemini"] = {
            "status": "ok" if effective.get("ok") else "error",
            "keys_tried": [k[:8] for k in gemini_keys[:len(gemini_details)]],
            "error_type": effective.get("error_detail", ""),
            "error_category": effective.get("error_category", ""),
            "latency_ms": effective.get("latency_ms"),
            "endpoint": effective.get("endpoint", ""),
            "retryable": effective.get("retryable", True),
            "details": gemini_details,
        }

    return snapshot
