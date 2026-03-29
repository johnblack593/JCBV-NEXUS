#!/usr/bin/env python3
"""
NEXUS Trading System — Post-Mortem Runner
===========================================
Ejecuta el Agente Post-Mortem sobre el historial de trades para
buscar patrones de pérdida y proponer reglas correctoras.
"""

import sys
import json
from pathlib import Path

# Ajustar PYTHONPATH
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from agents.agent_postmortem import AgentPostMortem  # type: ignore

def main():
    trades_file = _ROOT / "logs" / "paper_trades.json"
    if not trades_file.exists():
        print(f"No se encontró archivo de trades en {trades_file}")
        sys.exit(1)

    with open(trades_file, "r", encoding="utf-8") as f:
        try:
            trades = json.load(f)
        except json.JSONDecodeError:
            print(f"Error parseando {trades_file}")
            sys.exit(1)

    print(f"Cargados {len(trades)} trades en total.")
    
    agent = AgentPostMortem()
    result = agent.analyze_losses(trades)
    
    if result:
        print("\n--- ANÁLISIS POST-MORTEM ---")
        print(f"Análisis: {result.get('analysis')}")
        print(f"Regla Propuesta: {result.get('proposed_rule')}")
        print("\nLa regla ha sido guardada en rules/proposed_rules.json pendiente de revisión.")
    else:
        print("No se generó ninguna regla (posible falta de datos perdedores o error).")

if __name__ == "__main__":
    main()
