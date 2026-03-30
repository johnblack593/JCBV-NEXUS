import asyncio
import os
import sys

# Ajustar PYTHONPATH
sys.path.insert(0, os.path.abspath("."))

from nexus.config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from nexus.reporting.weekly_report import NexusTelegramReporter

async def main():
    print(f"Token length: {len(TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else 0}")
    print(f"Chat ID: {TELEGRAM_CHAT_ID}")
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or "tu_token" in TELEGRAM_BOT_TOKEN:
        print("❌ Error: Token o Chat ID vacío o conserva el valor por defecto en .env")
        return
        
    reporter = NexusTelegramReporter(bot_token=TELEGRAM_BOT_TOKEN, chat_id=TELEGRAM_CHAT_ID)
    await reporter.initialize()
    
    if reporter._initialized:
        print("✅ Conectado a Telegram, enviando mensaje de prueba...")
        try:
            await reporter._send("🟢 *NEXUS TEST*\n\nSi recibiste esto, NEXUS tiene acceso a tu Telegram correctamente.")
            print("🚀 Mensaje enviado con éxito.")
        except Exception as e:
            print(f"❌ Error al enviar mensaje: {e}")
    else:
        print("❌ Fallo crítico de inicialización del Bot de Telegram.")

if __name__ == "__main__":
    asyncio.run(main())
