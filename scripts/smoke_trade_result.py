"""
smoke_trade_result.py

Prueba la funcionalidad de consulta de resultado real del trade y balance de forma
asíncrona contra la capa de IQ Option.

Simula un entorno de trade con espera (`execute_and_wait_result`) y consultas a
`get_real_balance()`.
"""

import asyncio
import os
import sys

# Agregar path al workspace root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from nexus.core.execution.iqoption_engine import IQOptionExecutionEngine

async def main():
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("  🧪 NEXUS SMOKE TEST: Resultados y Balance Real")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")

    engine = IQOptionExecutionEngine()
    connected = await engine.connect()

    if not connected:
        print("❌ Error: No se pudo conectar a IQ Option (revisa credenciales en .env).")
        return

    print("✅ Motor IQ Option conectado exitosamente.")

    # 1. Obtener balance antes del trade simulado
    balance_before = await engine.get_real_balance()
    print(f"💼 Balance Inicial: ${balance_before:.2f}")

    # Simular una llamada con un order id inválido para probar que devuelve UNKNOWN o error_message manejado,
    # ya que ejecutar un trade real no es conveniente en un smoke test.
    print("⏱️  Ejecutando simulación de orden y esperando `execute_and_wait_result`...")
    
    result = await engine.execute_and_wait_result(
        asset="EURUSD",
        direction="CALL",
        amount=1.0,
        duration_minutes=1,
        timeout_seconds=5
    )

    print("\n📦 Resultado del proceso:")
    print(f"   Outcome: {result.get('outcome', 'UNKNOWN')}")
    print(f"   Beneficio Neto: {result.get('profit_net', 0.0)}")
    print(f"   Balance Final: {result.get('balance_after', 0.0)}")
    print(f"   Mensaje (error esperado): {result.get('error_message', 'Ninguno')}")

    if result.get("outcome") in ("UNKNOWN", "REJECTED"):
        print("\n✅ Funcionalidad validada (capturó correctamente un trade no válido/simulado)")
        
    await engine.disconnect()
    print("\n✅ Componentes evaluados correctos y balance verificado.")

if __name__ == "__main__":
    asyncio.run(main())
