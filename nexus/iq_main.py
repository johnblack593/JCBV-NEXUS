"""
NEXUS Trading System — IQ Option HFT Daemon
===================================================
El Núcleo Institucional: Funciona conectando concurrentemente 
IQ Option (Datos/Ejecución), Risk Manager (Kelly/Drawdown) y 
Telegram Reporter en un loop asíncrono infatigable.
"""

import asyncio
import logging
import sys
import os
import time
from datetime import datetime
import pandas as pd
import numpy as np

# Aterrizaje del root path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from nexus.core.iqoption_engine import IQOptionManager, IQOptionDataHandler, IQOptionExecutionEngine
from nexus.core.session_manager import SessionManager, MarketSession
from nexus.core.risk_manager import QuantRiskManager
from nexus.reporting.weekly_report import NexusTelegramReporter
from nexus.config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("nexus.hft_daemon")


class HFTDaemon:
    # ── Parámetros de Riesgo y Operación Institucional ──
    CYCLE_SECONDS = 60          # Cadencia de escaneo (1 minuto)
    MAX_CAPITAL_RISK = 0.02     # Tope Duro: 2% del balance total por trade
    CIRCUIT_BREAKER_DD = 0.10   # Cortacircuitos: si el DD supera el 10%, apagón total
    MIN_CONFIDENCE = 0.70       # Confianza mínima del modelo Vectorizado Alpha V3
    EXPIRY_TIME = 1             # Expiración en minutos por trade (Opciones Turbo)
    
    def __init__(self):
        # ── Subsistemas ──
        self.session_mgr = SessionManager()
        self.data_handler = IQOptionDataHandler()
        self.exec_engine = IQOptionExecutionEngine()
        self.risk_manager = QuantRiskManager(log_dir="logs")
        self.telegram = NexusTelegramReporter(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        
        self.running = False
        self.initial_balance = 0.0
        self.peak_balance = 0.0
        self.active_trades = {}
        self.suspended_assets = set()  # Activos bloqueados temporalmente por el broker
        
    async def initialize(self):
        logger.info("=" * 60)
        logger.info(" 🏛️ INICIALIZANDO NEXUS HFT DAEMON (IQ OPTION)")
        logger.info("=" * 60)
        
        manager = IQOptionManager.get_instance()
        
        # Conexión al Broker
        if not await manager.connect():
            logger.critical("Falla de Autenticación Crítica al levantar Daemon.")
            sys.exit(1)
            
        logger.info("✅ Conectores Websocket Activos.")
        logger.info(f"Modo: {manager.account_type}")
        
        # Conexión a Telegram
        await self.telegram.initialize()
        await self.telegram._send(
            "🏛️ <b>NEXUS HFT DAEMON INITIALIZED</b> 🏛️\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "• <b>Status  :</b> ONLINE 🟢\n"
            "• <b>Mode    :</b> AUTONOMOUS\n"
            f"• <b>Target  :</b> Opciones Binarias\n"
            f"• <b>Risk Cap:</b> {self.MAX_CAPITAL_RISK*100:.1f}% / Trade\n"
            "━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode="HTML"
        )
        
        # Obtener Balance Inicial
        balance = await manager.get_balance()
        self.initial_balance = balance
        self.peak_balance = balance
        logger.info(f"💰 Balance Capital de Trabajo: ${balance:.2f}")

    def evaluate_alpha_v3_vectorized(self, df: pd.DataFrame) -> dict:
        """ Evalúa la señal más reciente (última vela) usando Alpha v3. """
        if len(df) < 25:
            return {"signal": 0, "composite": 0.0}
            
        from ta.volatility import BollingerBands
        from ta.momentum import RSIIndicator
        
        bb = BollingerBands(close=df["close"], window=20, window_dev=2.0)
        rsi = RSIIndicator(close=df["close"], window=7).rsi()
        
        curr_close = float(df["close"].iloc[-1])
        curr_open = float(df["open"].iloc[-1])
        curr_low = float(df["low"].iloc[-1])
        curr_high = float(df["high"].iloc[-1])
        
        curr_lower = float(bb.bollinger_lband().iloc[-1])
        curr_upper = float(bb.bollinger_hband().iloc[-1])
        curr_rsi = float(rsi.iloc[-1])
        
        # Vol Ratio
        avg_vol = float(df["volume"].iloc[-21:-1].mean())
        curr_vol = float(df["volume"].iloc[-1])
        vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 1.0
        
        # BULLISH
        bull_score = 0.0
        if curr_low <= curr_lower: bull_score += 0.40
        elif curr_close <= curr_lower * 1.001: bull_score += 0.25
        if curr_rsi < 25: bull_score += 0.30
        elif curr_rsi < 35: bull_score += 0.20
        if curr_close > curr_open: bull_score += 0.15
        if vol_ratio >= 1.3: bull_score += 0.15
            
        # BEARISH
        bear_score = 0.0
        if curr_high >= curr_upper: bear_score += 0.40
        elif curr_close >= curr_upper * 0.999: bear_score += 0.25
        if curr_rsi > 75: bear_score += 0.30
        elif curr_rsi > 65: bear_score += 0.20
        if curr_close < curr_open: bear_score += 0.15
        if vol_ratio >= 1.3: bear_score += 0.15
            
        signal = 0
        composite = max(bull_score, bear_score)
        if bull_score > bear_score:
            signal = 1
        elif bear_score > bull_score:
            signal = -1
            
        return {"signal": signal, "composite": composite}

    async def _process_asset(self, asset: str) -> None:
        """ Procesa el activo: Descarga, Evalúa, y si hay ventaja, dispara. """
        if asset in self.active_trades:
            # Si hay un trade activo en este par (o esperando expirar), no spameamos.
            return
            
        # 1. Payout Check
        payout = await self.data_handler.get_payout(asset, "turbo")
        if payout < IQOptionManager.get_instance().min_payout:
            logger.debug(f"Saltando {asset}: Payout de {payout}% es menor al permitido.")
            return
            
        # 2. Obtener últimas velas (bastan 100 para la señal instantánea de Alpha v3)
        asset_clean = asset.replace("-op", "")
        df = await self.data_handler.get_historical_data(asset_clean, "1m", max_bars=100)
        
        if df.empty:
            return
            
        # 3. Alpha V3 Check (CPU-bound → offloaded al thread pool para no bloquear el event loop)
        eval_res = await asyncio.to_thread(self.evaluate_alpha_v3_vectorized, df)
        composite = eval_res["composite"]
        signal = eval_res["signal"]
        
        if composite < self.MIN_CONFIDENCE or signal == 0:
            return  # No hay edge estadístico suficiente
            
        action = "call" if signal == 1 else "put"
        current_price = float(df["close"].iloc[-1])
        
        logger.info(f"🎯 Alpha V3 DETECTÓ SEÑAL EN {asset} | Dirección: {action.upper()} | Confianza: {composite*100:.1f}%")
        
        # 4. KELLY CRITERION (Risk Management)
        balance = await IQOptionManager.get_instance().get_balance()
        if balance > self.peak_balance:
            self.peak_balance = balance
            
        drawdown = (self.peak_balance - balance) / self.peak_balance if self.peak_balance > 0 else 0.0
        
        # 4a. Verificar Circuit Breaker
        if self.risk_manager.circuit_breaker_check(drawdown, max_dd=self.CIRCUIT_BREAKER_DD):
            logger.critical("Circuit Breaker activo. Bot bloqueado por alto DD.")
            return

        # 4b. Calcular tamaño óptimo (Kelly puro)
        win_rate_est = 0.58  # Histórico estimado para Alpha V3
        avg_win = float(payout) / 100.0
        avg_loss = 1.0
        
        kelly_fraction = self.risk_manager.kelly_criterion(win_rate_est, avg_win, avg_loss)
        if kelly_fraction <= 0.0:
            logger.warning("Fórmula Kelly dio negativo (No hay ventaja matemática). Abortando entrada.")
            return
            
        # Tope duro institucional (ej. 2% del capital)
        trade_risk_pct = min(kelly_fraction, self.MAX_CAPITAL_RISK)
        trade_amount = round(balance * trade_risk_pct, 2)
        
        # Limitación mínima de entrada (Broker restringe $1 USD)
        if trade_amount < 1.0:
            if balance > 1.0:
                logger.info(f"Capital ({balance}) insuficiente para arriesgar solo 2% (${trade_amount}). Escalando a la apuesta mínima permitida de $1.00 por tratarse de simulación.")
                trade_amount = 1.0
            else:
                logger.warning(f"Capital agotado (${balance}). Abortando.")
                return

        # 5. EXECUTION & TELEGRAM
        logger.info(f"🚀 ENTRANDO AL MERCADO (HFT): {asset} | {action.upper()} | Monto: ${trade_amount:.2f}")
        
        res = await self.exec_engine.place_binary_order(asset, action, trade_amount, self.EXPIRY_TIME)
        
        if res.get("status") == "filled":
            ticket = res.get("order_id", "Unknown")
            # Enviar mensaje a la gerencia
            msg = (
                f"⚡ <b>NEXUS INSTITUTIONAL EXECUTION</b> ⚡\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📈 <b>ASSET :</b> {asset}\n"
                f"🎯 <b>DIR.  :</b> {action.upper()}\n"
                f"💰 <b>SIZE  :</b> ${trade_amount:.2f} ({(trade_risk_pct*100):.1f}%)\n"
                f"🔥 <b>PAYOUT:</b> {payout}%\n"
                f"🧠 <b>CONF. :</b> {composite*100:.1f}% (Alpha v3)\n"
                f"🏷 <b>PRICE :</b> {current_price:.5f}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<i>Ticket: #{ticket} | Exp: {self.EXPIRY_TIME}m</i>"
            )
            await self.telegram._send(msg, parse_mode="HTML")
            
            # Registrar el trade activo para chequear ganancia
            self.active_trades[asset] = {
                "id": ticket,
                "amount": trade_amount,
                "action": action,
                "payout": payout,
                "entry_time": time.time()
            }
        else:
            logger.error(f"Fallo de ejecución en {asset}: {res}")
            
            # Si el broker suspendió el activo temporalmente (Ej. mantenimiento o cierre abrupto de liquidez)
            error_msg = res.get("message", "").lower()
            if "suspended" in error_msg or "closed" in error_msg:
                self.suspended_assets.add(asset)
                
                # Reporte Inteligente: Calcular qué queda en pie para que gerencia sepa la ruta.
                session_assets = self.session_mgr.get_vip_assets_for_current_session(include_crypto=True)
                available = [a for a in session_assets if a not in self.suspended_assets]
                
                msg_suspend = (
                    f"⚠️ <b>NEXUS ROTATION PROTOCOL</b> ⚠️\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⛔ <b>Activo Suspendido:</b> {asset}\n"
                    f"<i>(El broker cortó liquidez. Removido del pool)</i>\n\n"
                    f"📡 <b>Monitoreando Disponibles:</b>\n"
                    f"<code>{', '.join(available) if available else 'NINGUNO'}</code>\n\n"
                    f"<i>Pivotando operaciones.</i>"
                )
                await self.telegram._send(msg_suspend, parse_mode="HTML")

    async def _check_active_trades(self):
        """ Revisa los tickets activos expirados y reporta ganancias. """
        now = time.time()
        to_delete = []
        
        manager = IQOptionManager.get_instance()
        
        for asset, data in self.active_trades.items():
            elapsed = now - data["entry_time"]
            
            # Si pasaron los minutos de expiración más 15 segundos de buffer para que el broker resuelva
            if elapsed > (self.EXPIRY_TIME * 60) + 15:
                # El API de iqoption no ofrece un check-win_status confiable directo para un único ticket, 
                # así que actualizaremos el balance global para intuir y removeremos el trade de tracking.
                new_balance = await manager.get_balance()
                
                # Reporte simple de cierre 
                resultado = "GANANCIA 🟩" if new_balance > self.initial_balance else "PÉRDIDA 🟥 / NEUTRAL"
                msg_cierre = (
                    f"🏁 <b>TRADE COMPLETADO</b> 🏁\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Activo :</b> {asset}\n"
                    f"<b>Ticket :</b> #{data['id']}\n"
                    f"<b>Balance:</b> ${new_balance:.2f}\n"
                    f"<b>Estado :</b> {resultado}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━"
                )
                await self.telegram._send(msg_cierre, parse_mode="HTML")
                
                logger.info(f"⏳ Cierre intuido de {asset}. Nuevo Balance: ${new_balance:.2f}")
                self.initial_balance = new_balance # Update for next track
                to_delete.append(asset)
                
        for asset in to_delete:
            del self.active_trades[asset]

    async def run(self):
        self.running = True
        logger.info(f"🔄 Daemon de Alta Frecuencia arrancado. Escaneando cada {self.CYCLE_SECONDS}s.")
        
        try:
            while self.running:
                start_time = time.time()
                
                # Check Circuit Breaker activo
                if self.risk_manager.is_circuit_breaker_active():
                    logger.warning("Circuit Breaker activo. Bot en cooldown temporal.")
                    await asyncio.sleep(self.CYCLE_SECONDS)
                    continue
                
                session = self.session_mgr.get_current_session()
                vip_assets = self.session_mgr.get_vip_assets_for_current_session(include_crypto=True)
                
                # REGLA BLACKROCK: Si es Fin de Semana, eliminar pares defectuosos OTC por completo
                if session == MarketSession.WEEKEND_OTC:
                    vip_assets = [a for a in vip_assets if "OTC" not in a.upper()]
                    
                # Filtro Dinámico: Eliminar los activos suspendidos de esta sesión para no perder tiempo
                vip_assets = [a for a in vip_assets if a not in self.suspended_assets]
                    
                # Evaluar cada activo
                tasks = [self._process_asset(asset) for asset in vip_assets]
                await asyncio.gather(*tasks)
                
                # Evaluar expiración
                await self._check_active_trades()
                
                # Mantenimiento de Loop Preciso
                elapsed = time.time() - start_time
                sleep_time = max(1.0, self.CYCLE_SECONDS - elapsed)
                await asyncio.sleep(sleep_time)

        except BaseException as e:
            logger.exception(f"Error fatal en el loop: {e}")
            await self.telegram._send(f"🚨 <b>[CRITICAL DUMP]</b>\nEl Daemon HFT colapsó: {e}", parse_mode="HTML")
            self.running = False


if __name__ == "__main__":
    daemon = HFTDaemon()
    asyncio.run(daemon.initialize())
    asyncio.run(daemon.run())
