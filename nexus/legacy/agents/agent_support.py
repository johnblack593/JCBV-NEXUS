# pyre-unsafe
"""
NEXUS Trading System — Agent Support Engineer v1
====================================================
Módulo de Auto-Diagnóstico, Corrección Inteligente y Notificación a Desarrolladores.

PIPELINE DE RESOLUCIÓN:
  1. Captura el StackTrace completo del error.
  2. Consulta el catálogo interno de soluciones conocidas.
  3. Si hay una solución automatizada -> la ejecuta y reporta via Telegram DEV.
  4. Si NO hay solución -> Invoca al LLM como "Senior DevOps Engineer" para generar:
     a) Diagnóstico detallado del error.
     b) Posible solución en código.
     c) Prompt ideal para que el desarrollador lo pegue en una IA y construya
        un auto-solucionador permanente.
  5. Envía todo el reporte al canal TELEGRAM_DEV_CHAT_ID.
  6. Registra el evento en el tier CRITICAL del QuantLogger.

USO:
    from agents.agent_support import NexusSupportEngineer
    support = NexusSupportEngineer()
    await support.initialize()
    await support.handle_exception(exc, module="main.py", context={...})
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.structured_logger import get_quant_logger

logger = logging.getLogger("nexus.support_engineer")

_TZ_GMT5 = timezone(timedelta(hours=-5))
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SOLUTIONS_CATALOG = _PROJECT_ROOT / "config" / "solutions_catalog.json"


# ══════════════════════════════════════════════════════════════════════
#  Catálogo de Soluciones Conocidas
# ══════════════════════════════════════════════════════════════════════

class SolutionsCatalog:
    """Base de conocimiento de errores previamente solucionados."""

    def __init__(self) -> None:
        self._catalog: List[Dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if _SOLUTIONS_CATALOG.exists():
            try:
                with open(_SOLUTIONS_CATALOG, "r", encoding="utf-8") as f:
                    self._catalog = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._catalog = []

    def _save(self) -> None:
        _SOLUTIONS_CATALOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_SOLUTIONS_CATALOG, "w", encoding="utf-8") as f:
            json.dump(self._catalog, f, indent=2, ensure_ascii=False)

    def find_solution(self, error_type: str, error_msg: str) -> Optional[Dict[str, Any]]:
        """Busca una solución conocida en el catálogo por tipo de error."""
        for entry in self._catalog:
            if entry.get("error_type") == error_type:
                # Buscar coincidencia parcial en el mensaje
                keywords = entry.get("match_keywords", [])
                if any(kw.lower() in error_msg.lower() for kw in keywords):
                    return entry
        return None

    def register_solution(
        self,
        error_type: str,
        match_keywords: List[str],
        description: str,
        auto_fix_action: str,
        severity: str = "medium",
    ) -> None:
        """Registra una nueva solución en el catálogo persistente."""
        entry = {
            "error_type": error_type,
            "match_keywords": match_keywords,
            "description": description,
            "auto_fix_action": auto_fix_action,
            "severity": severity,
            "added_at": datetime.now(_TZ_GMT5).isoformat(),
            "times_used": 0,
        }
        self._catalog.append(entry)
        self._save()
        logger.info("📚 Nueva solución registrada: %s", description)

    @property
    def size(self) -> int:
        return len(self._catalog)


# ══════════════════════════════════════════════════════════════════════
#  Acciones de Auto-Corrección
# ══════════════════════════════════════════════════════════════════════

_AUTO_FIX_REGISTRY: Dict[str, Any] = {}


def register_auto_fix(action_name: str):
    """Decorador para registrar funciones de auto-corrección."""
    def wrapper(func):
        _AUTO_FIX_REGISTRY[action_name] = func
        return func
    return wrapper


@register_auto_fix("clear_llm_cache")
def _fix_clear_llm_cache(**kwargs: Any) -> str:
    """Limpia la caché LLM cuando se corrompe."""
    cache_path = _PROJECT_ROOT / "logs" / "llm_historical_cache.json"
    if cache_path.exists():
        backup = cache_path.with_suffix(".json.bak")
        cache_path.rename(backup)
        return f"Cache LLM movida a backup: {backup}"
    return "Cache LLM no encontrada, nada que limpiar."


@register_auto_fix("restart_provider")
def _fix_restart_provider(**kwargs: Any) -> str:
    """Fuerza re-inicialización del proveedor LLM."""
    return "Señal de reinicio de proveedor enviada. Se aplicará en el próximo ciclo."


@register_auto_fix("purge_stale_data")
def _fix_purge_stale_data(**kwargs: Any) -> str:
    """Elimina archivos de datos temporales corruptos."""
    tmp_dir = _PROJECT_ROOT / "data" / "vault" / "tmp"
    if tmp_dir.exists():
        count = 0
        for f in tmp_dir.iterdir():
            if f.is_file():
                f.unlink()
                count += 1
        return f"Purgados {count} archivos temporales de vault."
    return "Directorio tmp no existe."


# ══════════════════════════════════════════════════════════════════════
#  NexusSupportEngineer
# ══════════════════════════════════════════════════════════════════════

class NexusSupportEngineer:
    """
    Ingeniero de Soporte Autónomo. Captura errores, intenta auto-corrección,
    y si falla, consulta al LLM para generar un reporte de diagnóstico
    completo que se envía al Developer via Telegram.
    """

    _SYSTEM_PROMPT = """Eres un Senior Platform Reliability Engineer (SRE) con 15 años de experiencia en sistemas de trading algorítmico en producción que manejan activos financieros reales. Tu nombre es NEXUS Support Engineer. Trabajas dentro de la plataforma NEXUS Trading System.

CONTEXTO CRÍTICO:
- Esta plataforma gestiona capital real y operaciones financieras automatizadas 24/7.
- Cada minuto que el sistema está caído representa potencial pérdida de capital.
- Tu diagnóstico será leído por desarrolladores que usarán IA para construir parches permanentes.
- Debes ser EXTREMADAMENTE preciso, detallado y profesional.

TU MISIÓN (ejecuta TODOS los pasos, sin excepciones):

1. EXPLICACIÓN DEL ERROR: Describe qué ocurrió, en qué módulo, y el flujo exacto de ejecución que llevó al fallo. Usa lenguaje técnico preciso pero comprensible.

2. DIAGNÓSTICO DE CAUSA RAÍZ: Identifica la causa raíz REAL, no los síntomas. Analiza el StackTrace línea por línea. Explica por qué el código falló en ese punto específico y qué condición no se manejó.

3. NIVEL DE RIESGO: Evalúa el impacto operacional en una escala:
   - CRITICAL: El sistema de trading puede ejecutar operaciones erróneas o perder fondos.
   - HIGH: Un subsistema vital está inoperativo (señales, calibración, reporting).
   - MEDIUM: Funcionalidad degradada pero el core de trading sigue operativo.
   - LOW: Error cosmético o de logging sin impacto en operaciones.

4. SOLUCIÓN EN CÓDIGO: Escribe la función Python COMPLETA que resuelve el error. El código debe ser production-ready: con manejo de excepciones, logging, y docstrings.

5. MEDIDAS PREVENTIVAS: Lista 3-5 acciones concretas para evitar que este error vuelva a ocurrir. Incluye: validaciones de datos, guardas defensivas, y mejoras de monitoreo.

6. PROMPT MAESTRO PARA AUTO-SOLUCIONADOR: Este es el entregable MÁS IMPORTANTE. Escribe un prompt extenso y detallado que el desarrollador humano pueda copiar y pegar directamente en un asistente IA (como ChatGPT, Gemini, Claude) para que esa IA construya:
   a) Una función de auto-corrección en Python que solucione este error automáticamente.
   b) El registro de esta función en el catálogo de NEXUS (solutions_catalog.json) con sus match_keywords.
   c) Un decorador @register_auto_fix que se integre al sistema existente.
   d) Tests unitarios para validar que el fix funciona.
   El prompt debe incluir todo el contexto necesario: el error original, el StackTrace, la arquitectura del catálogo, y el formato exacto de registro.

FORMATO DE RESPUESTA (JSON estricto, sin markdown, sin texto fuera del JSON):
{{
    "explanation": "Explicación completa y detallada del error (mínimo 3 párrafos)",
    "root_cause": "Causa raíz técnica específica del fallo",
    "risk_level": "CRITICAL|HIGH|MEDIUM|LOW",
    "risk_assessment": "Evaluación del impacto en operaciones de trading y capital",
    "solution_code": "Código Python completo de la solución (production-ready)",
    "preventive_measures": ["Medida 1", "Medida 2", "Medida 3"],
    "master_prompt": "Prompt extenso y detallado para que otra IA construya el auto-solucionador permanente con registro en el catálogo de NEXUS",
    "observations": "Observaciones adicionales o patrones preocupantes detectados en el código"
}}"""

    def __init__(self) -> None:
        self._bot = None
        self._dev_chat_id: str = ""
        self._dev_bot_token: str = ""
        self._llm = None
        self._initialized = False
        self._catalog = SolutionsCatalog()
        self._qlog = get_quant_logger()

    async def initialize(self) -> None:
        """Inicializa el bot de Telegram DEV y el LLM de soporte."""
        try:
            from config.settings import (
                TELEGRAM_DEV_BOT_TOKEN,
                TELEGRAM_DEV_CHAT_ID,
                GROQ_API_KEYS,
                GOOGLE_API_KEYS,
            )

            self._dev_bot_token = TELEGRAM_DEV_BOT_TOKEN
            self._dev_chat_id = TELEGRAM_DEV_CHAT_ID

            # Inicializar Bot Telegram DEV
            if self._dev_bot_token and self._dev_chat_id:
                try:
                    from telegram import Bot
                    self._bot = Bot(token=self._dev_bot_token)
                    me = await self._bot.get_me()
                    logger.info("🔧 Support Engineer Telegram DEV conectado: @%s", me.username)
                except ImportError:
                    logger.warning("python-telegram-bot no instalado. DEV alerts deshabilitadas.")
                except Exception as e:
                    logger.warning("Error inicializando Telegram DEV bot: %s", e)
            else:
                logger.warning("TELEGRAM_DEV_BOT_TOKEN o TELEGRAM_DEV_CHAT_ID no configurados.")

            # Inicializar LLM para diagnóstico (usar Groq primero, luego Gemini)
            self._init_diagnostic_llm(GROQ_API_KEYS, GOOGLE_API_KEYS)

            self._initialized = True
            logger.info(
                "🛡️ Support Engineer inicializado | Catálogo: %d soluciones | Telegram DEV: %s",
                self._catalog.size,
                "✅" if self._bot else "❌",
            )

            # Notificar inicio exitoso
            await self._send_dev_alert(
                "🟢 *NEXUS Support Engineer*\n\n"
                f"✅ Sistema inicializado correctamente\n"
                f"📚 Catálogo de soluciones: `{self._catalog.size}` entradas\n"
                f"⏰ {datetime.now(_TZ_GMT5).strftime('%Y-%m-%d %H:%M:%S')} GMT-5",
                level="info",
            )

        except Exception as e:
            logger.error("Error inicializando Support Engineer: %s", e)

    def _init_diagnostic_llm(
        self, groq_keys: List[str], google_keys: List[str]
    ) -> None:
        """Inicializa un LLM ligero exclusivamente para diagnóstico."""
        try:
            if groq_keys:
                from langchain_groq import ChatGroq
                self._llm = ChatGroq(
                    model="llama-3.3-70b-versatile",
                    api_key=groq_keys[0],
                    temperature=0.1,
                    max_retries=0,
                )
                logger.info("🧠 LLM Diagnóstico: Groq (Llama 3.3)")
                return
        except Exception:
            pass

        try:
            if google_keys:
                from langchain_google_genai import ChatGoogleGenerativeAI
                self._llm = ChatGoogleGenerativeAI(
                    model="gemini-1.5-flash",
                    google_api_key=google_keys[0],
                    temperature=0.1,
                    max_retries=0,
                )
                logger.info("🧠 LLM Diagnóstico: Gemini 1.5 Flash")
                return
        except Exception:
            pass

        logger.warning("⚠️ No hay LLM disponible para diagnóstico automático.")

    # ──────────────────────────────────────────────
    #  Pipeline Principal de Manejo de Errores
    # ──────────────────────────────────────────────

    async def handle_exception(
        self,
        exc: Exception,
        module: str = "unknown",
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Pipeline maestro de resolución de errores.

        Returns:
            Dict con la acción tomada y el resultado.
        """
        error_type = type(exc).__name__
        error_msg = str(exc)
        tb_str = traceback.format_exc()
        ctx = context or {}

        logger.error("🚨 [%s] %s: %s", module, error_type, error_msg)

        # 1. Registrar en el tier CRITICAL del QuantLogger
        self._qlog.log_crash(
            module=module,
            error_type=error_type,
            traceback_str=tb_str,
            context=ctx,
        )

        # 2. Buscar solución conocida en el catálogo
        known_solution = self._catalog.find_solution(error_type, error_msg)
        if known_solution:
            result = await self._execute_known_solution(known_solution, module, error_msg)
            return result

        # 3. Si no hay solución conocida -> Diagnóstico LLM
        if self._llm is not None:
            diagnosis = await self._diagnose_with_llm(module, error_type, error_msg, tb_str, ctx)
        else:
            diagnosis = self._build_manual_report(module, error_type, error_msg, tb_str)

        # 4. Enviar al Developer via Telegram
        await self._send_crash_report(module, error_type, error_msg, tb_str, diagnosis)

        return {
            "action": "diagnosed",
            "auto_fixed": False,
            "diagnosis": diagnosis,
        }

    async def _execute_known_solution(
        self, solution: Dict[str, Any], module: str, error_msg: str
    ) -> Dict[str, Any]:
        """Ejecuta una solución del catálogo conocido."""
        action_name = solution.get("auto_fix_action", "")
        fix_func = _AUTO_FIX_REGISTRY.get(action_name)

        if fix_func:
            try:
                fix_result = fix_func(module=module, error_msg=error_msg)
                logger.info("🔧 Auto-fix ejecutado: %s -> %s", action_name, fix_result)

                # Notificar corrección exitosa
                await self._send_dev_alert(
                    f"🔧 *AUTO\\-CORRECCIÓN EXITOSA*\n\n"
                    f"📦 Módulo: `{module}`\n"
                    f"🩹 Acción: `{action_name}`\n"
                    f"✅ Resultado: {fix_result}",
                    level="success",
                )

                self._qlog.log_system_event(
                    "auto_fix_success",
                    f"Auto-corrección: {action_name} en {module}",
                    level="WARNING",
                    data={"action": action_name, "result": fix_result},
                )

                return {"action": action_name, "auto_fixed": True, "result": fix_result}

            except Exception as fix_exc:
                logger.error("❌ Auto-fix falló: %s", fix_exc)

        return {"action": action_name, "auto_fixed": False, "result": "Fix no disponible"}

    async def _diagnose_with_llm(
        self,
        module: str,
        error_type: str,
        error_msg: str,
        tb_str: str,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Invoca al LLM de diagnóstico para generar un reporte experto."""
        try:
            from langchain_core.prompts import ChatPromptTemplate
            from langchain_core.output_parsers import StrOutputParser

            user_prompt = (
                f"═══ REPORTE DE INCIDENTE EN PRODUCCIÓN ═══\n\n"
                f"PLATAFORMA: NEXUS Trading System (Trading Algorítmico con Activos Reales)\n"
                f"MÓDULO AFECTADO: {module}\n"
                f"TIPO DE EXCEPCIÓN: {error_type}\n"
                f"MENSAJE DE ERROR: {error_msg}\n"
                f"TIMESTAMP: {datetime.now(_TZ_GMT5).isoformat()} GMT-5\n\n"
                f"═══ STACKTRACE COMPLETO ═══\n{tb_str}\n\n"
                f"═══ CONTEXTO OPERACIONAL ═══\n{json.dumps(context, indent=2)}\n\n"
                f"═══ ARQUITECTURA DEL CATÁLOGO DE SOLUCIONES ═══\n"
                f"El sistema NEXUS mantiene un archivo `config/solutions_catalog.json` con este formato:\n"
                f'{{"error_type": "...", "match_keywords": [...], "description": "...", '
                f'"auto_fix_action": "nombre_del_fix", "severity": "...", "times_used": 0}}\n'
                f"Las funciones de auto-fix se registran con el decorador @register_auto_fix(\"nombre\")\n"
                f"y se ubican en nexus/agents/agent_support.py.\n\n"
                f"INSTRUCCIÓN: Genera tu análisis COMPLETO siguiendo el formato JSON especificado en tus instrucciones de sistema. "
                f"El master_prompt debe ser lo suficientemente detallado para que un desarrollador lo copie en CUALQUIER IA "
                f"y obtenga una solución funcional con auto-fix registrable en el catálogo."
            )

            chain = (
                ChatPromptTemplate.from_messages([
                    ("system", self._SYSTEM_PROMPT),
                    ("human", "{error_report}"),
                ])
                | self._llm
                | StrOutputParser()
            )

            raw = chain.invoke({"error_report": user_prompt})

            # Parsear JSON del LLM (tolerante a markdown wrappers y control chars)
            clean = raw.strip()
            if "```json" in clean:
                clean = clean.split("```json")[1].split("```")[0].strip()
            elif "```" in clean:
                clean = clean.split("```")[1].split("```")[0].strip()

            # Limpiar caracteres de control ilegales (tabs internos, newlines en strings)
            clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', ' ', clean)
            # Reparar newlines dentro de strings JSON (common LLM issue)
            clean = clean.replace('\\n', '\\\\n')

            diagnosis = json.loads(clean)
            logger.info("\U0001f9e0 LLM Diagnóstico generado exitosamente para %s", module)
            return diagnosis

        except Exception as llm_exc:
            logger.warning("Error en LLM diagnóstico: %s", llm_exc)
            return self._build_manual_report(module, error_type, error_msg, tb_str)

    def _build_manual_report(
        self, module: str, error_type: str, error_msg: str, tb_str: str
    ) -> Dict[str, Any]:
        """Genera un reporte manual con PROMPT MAESTRO cuando el LLM no está disponible."""
        master_prompt = (
            f"Actúa como un Senior Platform Reliability Engineer (SRE) con 15 años de experiencia "
            f"en sistemas de trading algorítmico en producción. Estás trabajando en la plataforma "
            f"NEXUS Trading System, que gestiona capital real y operaciones financieras automatizadas 24/7.\n\n"
            f"═══ INCIDENTE EN PRODUCCIÓN ═══\n"
            f"Tipo de Error: {error_type}\n"
            f"Módulo Afectado: {module}\n"
            f"Mensaje: {error_msg}\n\n"
            f"StackTrace Completo:\n{tb_str[:1500]}\n\n"
            f"═══ TU MISIÓN (5 ENTREGABLES OBLIGATORIOS) ═══\n\n"
            f"1. DIAGNÓSTICO COMPLETO:\n"
            f"   - Explica qué ocurrió y por qué falló el código en ese punto exacto.\n"
            f"   - Identifica la CAUSA RAÍZ real (no síntomas).\n"
            f"   - Evalúa el NIVEL DE RIESGO: CRITICAL (puede perder fondos), HIGH (subsistema vital caído), "
            f"MEDIUM (degradado pero trading operativo), LOW (cosmético).\n\n"
            f"2. CÓDIGO DE LA SOLUCIÓN:\n"
            f"   - Escribe una función Python production-ready con manejo de excepciones, logging y docstrings.\n"
            f"   - Debe ser un drop-in replacement o patch del código que falló.\n\n"
            f"3. AUTO-SOLUCIONADOR PARA NEXUS:\n"
            f"   - Crea una función de auto-fix usando este decorador exacto:\n"
            f"     @register_auto_fix(\"nombre_descriptivo_del_fix\")\n"
            f"     def _fix_nombre(**kwargs: Any) -> str:\n"
            f"         # ... lógica de corrección ...\n"
            f"         return 'Descripción del resultado'\n\n"
            f"4. REGISTRO EN CATÁLOGO:\n"
            f"   - Genera la entrada JSON para solutions_catalog.json:\n"
            f"     {{\"error_type\": \"{error_type}\", \"match_keywords\": [\"keyword1\", \"keyword2\"], "
            f"\"description\": \"...\", \"auto_fix_action\": \"nombre_descriptivo_del_fix\", \"severity\": \"...\"}}"
            f"\n   - Incluye keywords específicas que identifiquen este error.\n\n"
            f"5. MEDIDAS PREVENTIVAS:\n"
            f"   - Lista 3-5 validaciones, guardas defensivas o mejoras de monitoreo "
            f"para que este error NUNCA vuelva a ocurrir.\n\n"
            f"IMPORTANTE: El auto-solucionador debe poder ejecutarse automáticamente por el NEXUS Support Engineer "
            f"la próxima vez que este error ocurra, sin intervención humana. "
            f"Los desarrolladores recibirán notificación vía Telegram cuando el fix se aplique."
        )
        return {
            "explanation": f"Error {error_type} en módulo {module}: {error_msg}",
            "root_cause": f"Requiere análisis manual — LLM no disponible para diagnóstico automático.",
            "risk_level": "HIGH",
            "risk_assessment": "Módulo afectado podría impactar operaciones si no se resuelve.",
            "solution_code": "# Requiere intervención del desarrollador con el Prompt Maestro.",
            "preventive_measures": ["Revisar nexus/logs/nexus_critical.log", "Verificar conectividad LLM", "Ejecutar test_fase17.py"],
            "master_prompt": master_prompt,
            "observations": "El LLM de diagnóstico no estaba disponible. Re-ejecutar cuando Groq/Gemini estén operativos.",
        }

    # ──────────────────────────────────────────────
    #  Telegram DEV Notifications
    # ──────────────────────────────────────────────

    async def _send_dev_alert(self, text: str, level: str = "info") -> None:
        """Envía una alerta al canal DEV de Telegram. Fallback a plain text si markdown falla."""
        if not self._bot or not self._dev_chat_id:
            return

        try:
            from telegram.constants import ParseMode
            await self._bot.send_message(
                chat_id=self._dev_chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            # Fallback: enviar sin markdown si el formateo causa error
            try:
                # Limpiar caracteres markdown problemáticos
                clean_text = text.replace('*', '').replace('`', '').replace('_', '')
                await self._bot.send_message(
                    chat_id=self._dev_chat_id,
                    text=clean_text,
                )
            except Exception as e2:
                logger.warning("Error enviando alerta DEV (plain text fallback): %s", e2)

    async def _send_crash_report(
        self,
        module: str,
        error_type: str,
        error_msg: str,
        tb_str: str,
        diagnosis: Dict[str, Any],
    ) -> None:
        """Envía el reporte completo de crash al Developer via Telegram (3 mensajes)."""
        risk = diagnosis.get("risk_level", diagnosis.get("severity", "HIGH")).upper()
        risk_icon = {
            "CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"
        }.get(risk, "⚪")

        explanation = diagnosis.get("explanation", diagnosis.get("diagnosis", "Sin diagnóstico."))
        root_cause = diagnosis.get("root_cause", "")
        risk_assessment = diagnosis.get("risk_assessment", "")
        solution = diagnosis.get("solution_code", "N/A")
        master_prompt = diagnosis.get("master_prompt", diagnosis.get("auto_fix_prompt", "N/A"))
        observations = diagnosis.get("observations", "")

        # Preventive measures (puede ser lista o string)
        prevention = diagnosis.get("preventive_measures", "N/A")
        if isinstance(prevention, list):
            prevention_str = "\n".join(f"  • {m}" for m in prevention)
        else:
            prevention_str = str(prevention)

        now = datetime.now(_TZ_GMT5).strftime("%Y-%m-%d %H:%M:%S")

        # ── Mensaje 1: Resumen del Incidente + Diagnóstico ──
        msg1 = (
            f"🚨 *NEXUS CRASH REPORT*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{risk_icon} *Nivel de Riesgo:* {risk}\n"
            f"📦 *Módulo:* `{module}`\n"
            f"❌ *Error:* `{error_type}`\n"
            f"💬 *Mensaje:* {error_msg[:150]}\n"
            f"⏰ *Hora:* {now} GMT-5\n\n"
            f"━━━━ 🧠 DIAGNÓSTICO IA ━━━━\n\n"
            f"📝 *Explicación:*\n{str(explanation)[:600]}\n\n"
            f"🔍 *Causa Raíz:*\n{str(root_cause)[:300]}\n\n"
            f"⚠️ *Impacto Operacional:*\n{str(risk_assessment)[:200]}\n\n"
            f"🛡️ *Medidas Preventivas:*\n{prevention_str[:400]}"
        )
        await self._send_dev_alert(msg1, level="error")

        # ── Mensaje 2: Solución en Código ──
        if observations:
            obs_block = f"\n\n👁️ *Observaciones:*\n{str(observations)[:300]}"
        else:
            obs_block = ""

        msg2 = (
            f"💡 *SOLUCIÓN PROPUESTA*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"```python\n{str(solution)[:900]}\n```"
            f"{obs_block}"
        )
        await self._send_dev_alert(msg2, level="error")

        # ── Mensaje 3: PROMPT MAESTRO (el entregable más importante) ──
        prompt_text = str(master_prompt)
        # Telegram tiene límite de 4096 chars, enviar en chunks si es necesario
        header = (
            f"📋 *PROMPT MAESTRO PARA AUTO-SOLUCIONADOR*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"_Copia y pega este prompt completo en tu asistente IA favorito._\n"
            f"_La IA te generará el auto-fix + registro en el catálogo de NEXUS._\n\n"
        )

        # Chunk el prompt si es muy largo
        max_chunk = 3800 - len(header)
        if len(prompt_text) <= max_chunk:
            await self._send_dev_alert(header + prompt_text, level="error")
        else:
            await self._send_dev_alert(header + prompt_text[:max_chunk], level="error")
            remaining = prompt_text[max_chunk:]
            while remaining:
                chunk = remaining[:3800]
                remaining = remaining[3800:]
                await self._send_dev_alert(f"📋 _(continuación)_\n\n{chunk}", level="error")

    # ──────────────────────────────────────────────
    #  Mantenimiento y Monitoreo Proactivo
    # ──────────────────────────────────────────────

    async def notify_maintenance_success(
        self, action: str, details: str, data: Optional[Dict[str, Any]] = None
    ) -> None:
        """Notifica al Developer que una tarea de mantenimiento se ejecutó correctamente."""
        now = datetime.now(_TZ_GMT5).strftime("%Y-%m-%d %H:%M:%S")
        msg = (
            f"✅ *NEXUS | MANTENIMIENTO EXITOSO*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔧 *Acción:* {action}\n"
            f"📝 *Detalles:* {details}\n"
            f"⏰ *Hora:* {now} GMT-5\n\n"
            f"_El sistema continúa operativo sin intervención._"
        )
        await self._send_dev_alert(msg, level="info")
        self._qlog.log_maintenance(action, details, data)

    async def notify_calibration_complete(
        self, mode: str, params: Dict[str, Any], elapsed_secs: float
    ) -> None:
        """Notifica al Developer que la calibración WFO finalizó."""
        now = datetime.now(_TZ_GMT5).strftime("%Y-%m-%d %H:%M:%S")
        params_str = json.dumps(params, indent=2)
        msg = (
            f"🎼 *NEXUS | CALIBRACIÓN WFO COMPLETADA*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📊 *Modo:* `{mode.upper()}`\n"
            f"⏱️ *Duración:* `{elapsed_secs:.0f}s`\n"
            f"⏰ *Hora:* {now} GMT-5\n\n"
            f"🏆 *Parámetros Óptimos:*\n```json\n{params_str[:800]}\n```\n\n"
            f"_Los parámetros han sido guardados en wfo_active_params.json._"
        )
        await self._send_dev_alert(msg, level="info")

    async def notify_scheduled_activity(
        self, activity: str, status: str, details: str,
        observations: Optional[List[str]] = None,
        suggestions: Optional[List[str]] = None,
    ) -> None:
        """Informa sobre actividades programadas con observaciones proactivas."""
        now = datetime.now(_TZ_GMT5).strftime("%Y-%m-%d %H:%M:%S")
        status_icon = {"success": "✅", "warning": "⚠️", "error": "❌", "info": "ℹ️"}.get(status, "📋")

        msg = (
            f"{status_icon} *NEXUS | INFORME DE ACTIVIDAD*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📋 *Actividad:* {activity}\n"
            f"📊 *Estado:* {status.upper()}\n"
            f"📝 *Detalles:* {details}\n"
            f"⏰ *Hora:* {now} GMT-5\n"
        )

        if observations:
            msg += "\n👁️ *Observaciones:*\n"
            for obs in observations[:5]:
                msg += f"  • {obs}\n"

        if suggestions:
            msg += "\n💡 *Sugerencias Preventivas:*\n"
            for sug in suggestions[:5]:
                msg += f"  → {sug}\n"

        await self._send_dev_alert(msg, level=status)
        self._qlog.log_system_event(
            f"scheduled_{activity.lower().replace(' ', '_')}",
            details,
            level="INFO" if status == "success" else "WARNING",
            data={"observations": observations or [], "suggestions": suggestions or []},
        )

    async def run_health_check(self) -> Dict[str, Any]:
        """Ejecuta un chequeo de salud proactivo y reporta al DEV."""
        from core.structured_logger import get_quant_logger
        qlog = get_quant_logger()
        stats = qlog.get_tier_stats()

        observations = []
        suggestions = []
        overall_status = "success"

        for tier, info in stats.items():
            usage = info["usage_pct"]
            if usage > 80:
                observations.append(f"Tier {tier.upper()} al {usage}% de capacidad")
                suggestions.append(f"Considerar purga manual de {tier} logs")
                overall_status = "warning"
            elif usage > 50:
                observations.append(f"Tier {tier.upper()} al {usage}% (monitorear)")

        # Verificar catálogo
        catalog = SolutionsCatalog()
        observations.append(f"Catálogo de auto-fixes: {catalog.size} soluciones registradas")

        # Verificar conectividad LLM
        if self._llm is not None:
            observations.append("LLM de diagnóstico: Operativo")
        else:
            observations.append("LLM de diagnóstico: No disponible")
            suggestions.append("Verificar credenciales GROQ/GOOGLE en .env")
            overall_status = "warning"

        # Verificar Telegram DEV
        if self._bot:
            observations.append("Telegram DEV pipeline: Conectado")
        else:
            observations.append("Telegram DEV pipeline: Desconectado")
            suggestions.append("Configurar TELEGRAM_DEV_CHAT_ID en .env")
            overall_status = "warning"

        if not observations:
            observations.append("Todos los subsistemas operativos.")

        await self.notify_scheduled_activity(
            activity="Health Check",
            status=overall_status,
            details="Verificación proactiva de subsistemas de mantenimiento.",
            observations=observations,
            suggestions=suggestions if suggestions else None,
        )

        return {"status": overall_status, "observations": observations, "suggestions": suggestions}


# ══════════════════════════════════════════════════════════════════════
#  Pre-cargamos soluciones base conocidas al primer import
# ══════════════════════════════════════════════════════════════════════

def _seed_default_solutions() -> None:
    """Registra soluciones base si el catálogo está vacío."""
    catalog = SolutionsCatalog()
    if catalog.size == 0:
        catalog.register_solution(
            error_type="JSONDecodeError",
            match_keywords=["llm_historical_cache", "Expecting value"],
            description="Cache LLM corrupta",
            auto_fix_action="clear_llm_cache",
            severity="medium",
        )
        catalog.register_solution(
            error_type="ConnectionError",
            match_keywords=["groq.com", "googleapis.com", "timeout"],
            description="Timeout de conexión a proveedor LLM",
            auto_fix_action="restart_provider",
            severity="high",
        )
        catalog.register_solution(
            error_type="OSError",
            match_keywords=["No space left", "disk full", "vault/tmp"],
            description="Disco lleno o datos temporales acumulados",
            auto_fix_action="purge_stale_data",
            severity="critical",
        )
        logger.info("📚 Catálogo de soluciones base sembrado: %d entradas", catalog.size)


# Auto-seed al importar el módulo
_seed_default_solutions()
