"""
NEXUS Trading System — Agent Post-Mortem
==========================================
Analiza las operaciones perdedoras de un periodo y propone una regla estricta
para evitar cometer el mismo error.
"""

import os
import json
import logging
import re
from typing import Any, Dict, List, Optional
from langchain_groq import ChatGroq  # type: ignore
from langchain_core.prompts import ChatPromptTemplate  # type: ignore
from langchain_core.output_parsers import StrOutputParser  # type: ignore
from config.settings import GROQ_API_KEY  # type: ignore

logger = logging.getLogger("nexus.agent_postmortem")

_SYSTEM_PROMPT = """Eres el Agente Post-Mortem de un fondo cuantitativo institucional.
Tu tarea es analizar un lote de operaciones perdedoras recientes generadas por el sistema NEXUS de paper trading algorítmico.
Debes encontrar el DENOMINADOR COMÚN (el error recurrente o causa estructural) que causó estas pérdidas consistentes en este conjunto de datos.
Luego, debes proponer EXACTAMENTE UNA (1) REGLA de trading estricta, programática y clara de máximo 2 líneas.
Esta regla será propuesta para ser insertada dinámicamente en el prompt del LLM principal para que NEXUS detecte el patrón a tiempo y no vuelva a cometer este error.

Salida esperada (DEBES EMITIR SOLO UN JSON VÁLIDO. SIN TEXTO ADICIONAL, SIN ETIQUETAS DE MARKDOWN):
{
    "analysis": "breve y técnica explicacion del patron de fallo encontrado",
    "proposed_rule": "NUNCA [ACCION] SI [condicion] Y [contexto_de_mercado]"
}
"""

class AgentPostMortem:
    """Agente Reflexivo: Audita trades pasados en busca de fallos crónicos."""
    
    def __init__(self) -> None:
        self._llm = ChatGroq(
            api_key=GROQ_API_KEY,
            model="llama-3.3-70b-versatile",
            temperature=1.0  # High temperature for creative out-of-the-box pattern recognition
        )
        self._chain = (
            ChatPromptTemplate.from_messages([
                ("system", _SYSTEM_PROMPT),
                ("human", "Analiza los siguientes trades perdedores y dame la regla correctora. Recuerda: SOLO JSON, NADA DE MARKDOWN:\n{trades_json}")
            ])
            | self._llm
            | StrOutputParser()
        )

    def analyze_losses(self, all_trades: List[Dict[str, Any]]) -> Optional[Dict[str, str]]:
        """Busca trades cerrados con pérdidas y manda a analizar si hay al menos 5."""
        
        # Filtrar trades cerrados con perdida
        losing_trades = [t for t in all_trades if t.get("pnl", 0) < 0 and t.get("status", "") != "OPEN"]
        
        if len(losing_trades) < 5:
            logger.info("Insuficientes trades perdedores (%d) para un análisis Post-Mortem estadístico.", len(losing_trades))
            return None

        # Tomar los últimos 15 trades perdedores (evitar saturar la ventana de contexto)
        recent_losses = losing_trades[-15:]  # type: ignore
        
        # Filtrar campos enormes e inútiles para el prompt (como el log completo o timestamps si no ayudan)
        clean_losses = []
        for t in recent_losses:
            clean_t = {
                "symbol": t.get("symbol"),
                "side": t.get("side"),
                "entry_price": t.get("entry_price"),
                "pnl": t.get("pnl"),
                "reason": t.get("status"), # CLOSED_SL, CLOSED_TP, etc
            }
            clean_losses.append(clean_t)

        trades_str = json.dumps(clean_losses, indent=2)

        try:
            logger.info("🔍 Iniciando análisis Post-Mortem profundo de %d trades perdedores consecutivos...", len(clean_losses))
            raw_response = self._chain.invoke({"trades_json": trades_str})
            
            # Limpiar por si el LLM pone markdown
            json_match = re.search(r'\{[^{}]*\}', raw_response, re.DOTALL)
            if json_match:
                cleaned = json_match.group()
            else:
                cleaned = raw_response.strip()
                
            result = json.loads(cleaned)
            
            rule = result.get("proposed_rule")
            analysis = result.get("analysis")
            
            logger.info("🧠 Post-Mortem Completado.")
            logger.info("   -> Análisis: %s", analysis)
            logger.info("   -> Regla Propuesta: %s", rule)
            
            self._save_proposed_rule(rule, analysis)
            
            return result
        except Exception as exc:
            logger.error("❌ Error en AgentPostMortem LLM parsing: %s", exc)
            return None

    def _save_proposed_rule(self, rule: str, analysis: str) -> None:
        """Guarda la nueva regla propuesta en un archivo para revisión del usuario."""
        # Se guarda en rules/proposed_rules.json
        rules_dir = os.path.join(os.path.dirname(__file__), "..", "rules")
        os.makedirs(rules_dir, exist_ok=True)
        
        file_path = os.path.join(rules_dir, "proposed_rules.json")
        
        existing = []
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                pass
                
        existing.append({
            "analysis": analysis,
            "rule": rule,
            "status": "PENDING_REVIEW"
        })
        
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=4, ensure_ascii=False)
            
        logger.info("Regla guardada en %s en estado PENDING_REVIEW.", file_path)
