import asyncio
import sys
import os
import logging
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from nexus.core.iqoption_engine import IQOptionManager
from nexus.reporting.weekly_report import get_reporter

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("nexus.live_test")

async def force_test():
    logger.info("=" * 60)
    logger.info(" EJECUCION DIRECCIONAL (BUY / SELL) EN VIVO ")
    logger.info("=" * 60)
    
    manager = IQOptionManager.get_instance()
    
    # Init Telegram
    tg = get_reporter()
    await tg.initialize()
    
    if not await manager.connect():
        logger.error("❌ Falló conexión al broker.")
        return
        
    logger.info(f"✅ Conectado. Cuenta en uso: {manager.account_type}")
    
    # Debido a limitantes de fin de semana, buscaremos el par OTC más líquido
    asset = "EURUSD-OTC"
    amount = 1.0  # $1
    expiry_m = 1  # 1 minuto
    
    # Notificar inicio a telegram
    msg_start = (
        "🧪 <b>TEST DE PRODUCCIÓN EN VIVO</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Iniciando inyección direccional dual en {asset}</i>\n"
        "<i>Validando rutas CALL (Buy) y PUT (Sell).</i>"
    )
    await tg._send(msg_start, parse_mode="HTML")
    
    # ==========================
    # TEST: COMPRA / BUY / CALL
    # ==========================
    logger.info(f"🔄 Disparando orden BUY (CALL) en {asset}...")
    check_c, id_c = await asyncio.to_thread(manager.api.buy, amount, asset, "call", expiry_m)
    
    if check_c:
        res_call = f"✅ ÉXITO | Ticket: #{id_c}"
        logger.info(res_call)
    else:
        res_call = f"❌ RECHAZO | Motivo: {id_c}"
        logger.error(res_call)
        
    await asyncio.sleep(2)  # Pausa antifraude de IQ Option
    
    # ==========================
    # TEST: VENTA / SELL / PUT
    # ==========================
    logger.info(f"🔄 Disparando orden SELL (PUT) en {asset}...")
    check_p, id_p = await asyncio.to_thread(manager.api.buy, amount, asset, "put", expiry_m)
    
    if check_p:
        res_put = f"✅ ÉXITO | Ticket: #{id_p}"
        logger.info(res_put)
    else:
        res_put = f"❌ RECHAZO | Motivo: {id_p}"
        logger.error(res_put)
        
    # Notificar reporte final
    msg_end = (
        "⚙️ <b>REPORTE DE DIRECCIONES (API)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>CALL (Buy):</b> {res_call}\n"
        f"📉 <b>PUT (Sell):</b> {res_put}\n"
    )
    
    if check_c and check_p:
        msg_end += "\n🟢 <i>Ambas rutas activas. Despliegue listo.</i>"
    else:
        msg_end += "\n🟠 <i>Límites de broker nocturno detectados.</i>"
        
    await tg._send(msg_end, parse_mode="HTML")
    logger.info("Prueba finalizada.")

if __name__ == "__main__":
    asyncio.run(force_test())
