#!/usr/bin/env python3
"""
NEXUS v5.0 — Smoke Test de Conectividad LLM
=============================================
Script standalone para diagnosticar conectividad con proveedores LLM.

Uso:
    python scripts/smoke_llm_connectivity.py

Exit codes:
    0 — Al menos un proveedor funciona
    1 — Ningún proveedor funciona

Verifica:
    - Resolución DNS
    - Conexión TCP
    - Autenticación (clave API válida)
    - Latencia
    - Clasifica errores con precisión
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

# Force UTF-8 output on Windows consoles
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Asegurar que el directorio raíz del proyecto esté en el path
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.insert(0, project_root)

from dotenv import load_dotenv

load_dotenv(os.path.join(project_root, ".env"))

from nexus.core.llm.diagnostics import (
    preflight_groq,
    preflight_gemini,
    classify_provider_error,
    CATEGORY_DNS,
    CATEGORY_TIMEOUT,
    CATEGORY_TCP,
    CATEGORY_SSL,
    CATEGORY_HTTP_401,
    CATEGORY_HTTP_429,
    CATEGORY_HTTP_5XX,
)


# ══════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════

def parse_keys(env_plural: str, env_singular: str) -> list[str]:
    """Lee claves API del .env."""
    keys_str = os.getenv(env_plural, "").strip()
    if not keys_str:
        keys_str = os.getenv(env_singular, "").strip()
    if not keys_str:
        return []
    return [k.strip() for k in keys_str.split(",") if k.strip()]


def status_icon(result: dict) -> str:
    """Icono según resultado del preflight."""
    if result["ok"]:
        return "✅ OK"
    cat = result.get("error_category", "UNKNOWN")
    return f"❌ ERROR {cat}"


def format_latency(ms: float | None) -> str:
    """Formatea latencia en ms."""
    if ms is None:
        return "—"
    return f"{ms:.0f} ms"


_CATEGORY_COLORS = {
    CATEGORY_DNS: "🌐",
    CATEGORY_TIMEOUT: "⏱️",
    CATEGORY_TCP: "🔌",
    CATEGORY_SSL: "🔒",
    CATEGORY_HTTP_401: "🔑",
    CATEGORY_HTTP_429: "⚠️",
    CATEGORY_HTTP_5XX: "💥",
}


# ══════════════════════════════════════════════════════════════════════
#  Main Smoke Test
# ══════════════════════════════════════════════════════════════════════

async def run_smoke_test() -> int:
    """
    Ejecuta smoke test de conectividad para todos los proveedores LLM.
    Returns 0 si al menos uno funciona, 1 si ninguno.
    """
    groq_keys = parse_keys("GROQ_API_KEYS", "GROQ_API_KEY")
    gemini_keys = parse_keys("GEMINI_API_KEYS", "GEMINI_API_KEY")
    groq_model = os.getenv("GROQ_MODEL", "llama3-70b-8192").strip()
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()

    separator = "━" * 55

    print()
    print(separator)
    print("  🤖 NEXUS v5.0 — Smoke Test de Conectividad LLM")
    print(separator)

    # Platform detection: ThreadedResolver is critical on Windows
    if sys.platform == "win32":
        print("  ℹ️  Sistema: Windows — usando ThreadedResolver para DNS")
    else:
        print(f"  ℹ️  Sistema: {sys.platform}")
    print()

    any_ok = False
    first_ok_provider = None
    dominant_error = None
    results_summary: list[dict] = []

    # ── Groq ──────────────────────────────────────────────────────────
    if groq_keys:
        print(f"  📦 Groq ({len(groq_keys)} claves configuradas)")
        print(f"     Modelo: {groq_model}")
        print()

        for i, key in enumerate(groq_keys, 1):
            key_hint = key[:8] + "..."
            print(f"     [{i}] Clave {key_hint} ... ", end="", flush=True)

            result = await preflight_groq(key, model=groq_model)

            if result["ok"]:
                print(f"✅ OK  ({format_latency(result['latency_ms'])})")
                any_ok = True
                if first_ok_provider is None:
                    first_ok_provider = "Groq"
                # Skip remaining Groq keys
                for j in range(i + 1, len(groq_keys) + 1):
                    remaining_hint = groq_keys[j - 1][:8] + "..."
                    print(f"     [{j}] Clave {remaining_hint} ... ⏭️ OMITIDA (ya hay una válida)")
                break
            else:
                cat = result.get("error_category", "UNKNOWN")
                detail = result.get("error_detail", "")
                emoji = _CATEGORY_COLORS.get(cat, "❓")
                print(f"❌ ERROR {cat}  {emoji}")
                print(f"         Detalle: {detail}")
                if cat == "UNKNOWN" and "raw_error" in result:
                    raw = str(result["raw_error"]).replace("\n", " ")
                    print(f"         Raw: {raw}")
                if dominant_error is None:
                    dominant_error = cat

            results_summary.append(result)

        print()
    else:
        print("  ⚠️ Groq: Sin claves configuradas")
        print()

    # ── Gemini ────────────────────────────────────────────────────────
    if gemini_keys:
        print(f"  📦 Gemini ({len(gemini_keys)} claves configuradas)")
        print(f"     Modelo: {gemini_model}")
        print()

        for i, key in enumerate(gemini_keys, 1):
            key_hint = key[:8] + "..."
            print(f"     [{i}] Clave {key_hint} ... ", end="", flush=True)

            result = await preflight_gemini(key, model=gemini_model)

            if result["ok"]:
                print(f"✅ OK  ({format_latency(result['latency_ms'])})")
                any_ok = True
                if first_ok_provider is None:
                    first_ok_provider = "Gemini"
                for j in range(i + 1, len(gemini_keys) + 1):
                    remaining_hint = gemini_keys[j - 1][:8] + "..."
                    print(f"     [{j}] Clave {remaining_hint} ... ⏭️ OMITIDA (ya hay una válida)")
                break
            else:
                cat = result.get("error_category", "UNKNOWN")
                detail = result.get("error_detail", "")
                emoji = _CATEGORY_COLORS.get(cat, "❓")
                print(f"❌ ERROR {cat}  {emoji}")
                print(f"         Detalle: {detail}")
                if cat == "UNKNOWN" and "raw_error" in result:
                    raw = str(result["raw_error"]).replace("\n", " ")
                    print(f"         Raw: {raw}")
                if dominant_error is None:
                    dominant_error = cat

            results_summary.append(result)

        print()
    else:
        print("  ⚠️ Gemini: Sin claves configuradas")
        print()

    # ── No keys at all ────────────────────────────────────────────────
    if not groq_keys and not gemini_keys:
        print("  ❌ No hay proveedores LLM configurados.")
        print("     Configurar GROQ_API_KEYS o GEMINI_API_KEYS en .env")
        print()

    # ── Resumen Final ─────────────────────────────────────────────────
    print(separator)

    if any_ok:
        print(f"  ✅ Proveedor operativo: {first_ok_provider}")
        print(f"  🧠 Modo macro sugerido: LLM")
    else:
        print(f"  ❌ Resultado final: SIN LLM DISPONIBLE")
        print(f"  🧠 Modo macro sugerido: HEURÍSTICO")
        if dominant_error:
            emoji = _CATEGORY_COLORS.get(dominant_error, "❓")
            print(f"  {emoji} Causa dominante: {dominant_error}")

            # Acciones sugeridas
            actions = {
                CATEGORY_DNS: "Revisar internet/firewall/VPN/DNS del servidor",
                CATEGORY_TIMEOUT: "Verificar latencia de red y estado del proveedor",
                CATEGORY_TCP: "Verificar que el servidor puede alcanzar los endpoints",
                CATEGORY_SSL: "Verificar certificados SSL y fecha/hora del sistema",
                CATEGORY_HTTP_401: "Verificar claves API en el archivo .env",
                CATEGORY_HTTP_429: "Esperar renovación de cuota o agregar más claves",
                CATEGORY_HTTP_5XX: "El proveedor tiene problemas temporales",
            }
            action = actions.get(dominant_error, "Revisar logs para detalles")
            print(f"  📋 Acción sugerida: {action}")

    print(separator)
    print()

    # Seccion final: ERRORES NO CLASIFICADOS
    unknown_results = [r for r in results_summary if r.get("error_category") == "UNKNOWN" and r.get("raw_error")]
    if unknown_results:
        print()
        print("  ⚠️  ERRORES NO CLASIFICADOS (detalles técnicos):")
        print("  " + "─" * 45)
        for i, r in enumerate(unknown_results, 1):
            prov = r.get("provider", "Unknown")
            print(f"  [{prov}] Clave {i}: {r.get('raw_error')}")
            print()

    return 0 if any_ok else 1


# ══════════════════════════════════════════════════════════════════════
#  Entry Point
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    exit_code = asyncio.run(run_smoke_test())
    sys.exit(exit_code)
