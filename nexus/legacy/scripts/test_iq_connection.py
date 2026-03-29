"""
NEXUS Trading System — IQ Option Connection Test
=====================================
Script de prueba de humo para validar la integración de grado
institucional con los servidores WebSocket de IQ Option.

Valida:
1. Login correcto
2. Lectura de Payout actual
3. Obtención de velas tick-perfect (1m)
4. (Opcional) Ejecución de órden de práctica ($1)
"""

import asyncio
import logging
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from nexus.core.iqoption_engine import IQOptionManager, IQOptionDataHandler, IQOptionExecutionEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

async def main():
    manager = IQOptionManager.get_instance()
    print("=" * 60)
    print(" 🏦 NEXUS ALPHA — IQ OPTION INTEGRATION TEST")
    print("=" * 60)
    
    if not manager.email or "example.com" in manager.email:
        print("❌ Error: Debes configurar IQ_OPTION_EMAIL e IQ_OPTION_PASSWORD en tu archivo .env")
        return
        
    print(f"⏳ Conectando a IQ Option ({manager.email})...")
    connected = await manager.connect()
    
    if not connected:
        print("❌ Prueba abortada. No se pudo conectar.")
        return
        
    balance = await manager.get_balance()
    print(f"✅ Conectado exitosamente. Balance MODO {manager.account_type}: ${balance:.2f}")
    
    # Probar DataHandler
    dh = IQOptionDataHandler()
    asset = "EURUSD"
    
    payout = await dh.get_payout(asset, "turbo")
    if payout == 0.0:
        # Los findes de semana Forex cerrado, usar OTC
        asset = "EURUSD-OTC"
        payout = await dh.get_payout(asset, "turbo")
        
    print(f"📊 Payout actual para {asset} (Turbo): {payout}%")
    
    # Obtener velas
    print(f"📥 Solicitando últimas 5 velas de 1 minuto para {asset}...")
    df = await dh.get_historical_data(asset, "1m", max_bars=5)
    
    if df.empty:
        print("❌ Fallo obteniendo datos OHLCV.")
    else:
        print("✅ Velas obtenidas (Muestra de la última vela):")
        print(df.tail(1)[['open_time', 'open', 'high', 'low', 'close', 'volume']].to_string(index=False))
        
    # Prueba de ejecución en Demo
    if manager.account_type == "PRACTICE" and payout >= manager.min_payout:
        print(f"\n🚀 Probando ejecución de orden dummy ($1) en PRACTICE para {asset}...")
        ex_engine = IQOptionExecutionEngine()
        res = await ex_engine.place_binary_order(asset, "call", 1.0, 1)
        
        if res.get("status") == "filled":
            print(f"✅ Orden en la nube ejecutada: {res}")
        elif "suspended" in str(res.get("message", "")).lower() and "-OTC" not in asset:
            print("⚠️ Activo suspendido (probablemente fin de semana). Intentando con EURUSD-OTC...")
            asset = "EURUSD-OTC"
            payout = await dh.get_payout(asset, "turbo")
            print(f"📊 Nuevo Payout para {asset}: {payout}%")
            if payout >= manager.min_payout:
                res_otc = await ex_engine.place_binary_order(asset, "call", 1.0, 1)
                if res_otc.get("status") == "filled":
                    print(f"✅ Orden en la nube ejecutada (OTC): {res_otc}")
                else:
                    print(f"⚠️ Orden OTC fallida: {res_otc}")
            else:
                print("⚠️ Payout demasiado bajo para OTC.")
        else:
            print(f"⚠️ Orden fallida o denegada: {res}")
    else:
        print("\n⚠️ Prueba de ejecución saltada: La cuenta no es PRACTICE o el payout es menor al mínimo.")

    print("=" * 60)
    print(" Prueba finalizada.")

if __name__ == "__main__":
    asyncio.run(main())
