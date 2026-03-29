import asyncio
import sys
import os
import logging
import traceback

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from nexus.core.iqoption_engine import IQOptionExecutionEngine, IQOptionManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("nexus.test_buy")

async def test_execution():
    logger.info("=" * 50)
    logger.info(" PRUEBA DE ESTRÉS DE CABLEADO (IQ OPTION)")
    logger.info("=" * 50)
    
    manager = IQOptionManager.get_instance()
    
    if not await manager.connect():
        logger.error("No se pudo conectar a IQ Option.")
        return
        
    logger.info("Conectado con cuenta: " + manager.account_type)
    
    # Prueba A: Fallo por cableado o fondos (Intento de comprar 1 Millón de dólares en un activo genérico)
    logger.info(f"== PRUEBA A: Intento de compra con saldo infinito en EURUSD ==")
    check_a, id_a = await asyncio.to_thread(manager.api.buy, 1000000, "EURUSD", "call", 1)
    logger.info(f"Resultado PRUEBA A: {id_a}")
    
    # Prueba B: Fallo real por suspensión de activo (Intento de compra de $1 en BTCUSD)
    logger.info(f"== PRUEBA B: Intento normal ($1) en BTCUSD suspendido ==")
    check_b, id_b = await asyncio.to_thread(manager.api.buy, 1, "BTCUSD", "call", 1)
    logger.info(f"Resultado PRUEBA B: {id_b}\n")
    
    logger.info("Prueba de estrés de cableado finalizada.")

if __name__ == "__main__":
    asyncio.run(test_execution())
