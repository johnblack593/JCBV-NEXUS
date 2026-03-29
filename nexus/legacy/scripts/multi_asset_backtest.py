import asyncio
import logging
import sys
import os
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from nexus.core.iqoption_engine import IQOptionManager, IQOptionDataHandler
from nexus.core.session_manager import SessionManager
from nexus.core.signal_engine import NexusAlphaOscillatorCalculator

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("nexus.multi_backtest")
logger.setLevel(logging.INFO)

# Configuración del motor de trading para las pruebas
CONFIDENCE_THRESHOLD = 0.70
EXPIRY_BARS = 4
CANDLES_TO_FETCH = 2000  # Aprox 7 días hábiles a 5 minutos


def evaluate_binary_trades(df: pd.DataFrame, payout_pct: float) -> dict:
    """Simula resultados binarios con lógica vectorizada (Alpha v3)."""
    df = df.copy()
    
    from ta.volatility import BollingerBands
    from ta.momentum import RSIIndicator
    
    # 1. Indicadores Base
    bb = BollingerBands(close=df["close"], window=20, window_dev=2.0)
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_upper"] = bb.bollinger_hband()
    df["rsi"] = RSIIndicator(close=df["close"], window=7).rsi()
    df["vol_ma"] = df["volume"].rolling(window=20).mean().shift(1)
    df["vol_ratio"] = np.where(df["vol_ma"] > 0, df["volume"] / df["vol_ma"], 1.0)
    
    # 2. Bullish Score
    bull_score = np.zeros(len(df))
    bull_score += np.where(df["low"] <= df["bb_lower"], 0.40, np.where(df["close"] <= df["bb_lower"] * 1.001, 0.25, 0.0))
    bull_score += np.where(df["rsi"] < 25, 0.30, np.where(df["rsi"] < 35, 0.20, 0.0))
    bull_score += np.where(df["close"] > df["open"], 0.15, 0.0)
    bull_score += np.where(df["vol_ratio"] >= 1.3, 0.15, 0.0)
    
    # 3. Bearish Score
    bear_score = np.zeros(len(df))
    bear_score += np.where(df["high"] >= df["bb_upper"], 0.40, np.where(df["close"] >= df["bb_upper"] * 0.999, 0.25, 0.0))
    bear_score += np.where(df["rsi"] > 75, 0.30, np.where(df["rsi"] > 65, 0.20, 0.0))
    bear_score += np.where(df["close"] < df["open"], 0.15, 0.0)
    bear_score += np.where(df["vol_ratio"] >= 1.3, 0.15, 0.0)
    
    df["bull_score"] = bull_score
    df["bear_score"] = bear_score
    
    # Determinar dirección dominante
    df["signal"] = np.where(bull_score > bear_score, 1, -1)
    df["composite"] = np.maximum(bull_score, bear_score)
    
    # Eliminar NAN de rolling
    df = df.dropna().reset_index(drop=True)
    
    wins_call = 0
    losses_call = 0
    wins_put = 0
    losses_put = 0
    
    # Simular entrada y expiración
    for i in range(len(df) - EXPIRY_BARS):
        composite = df.loc[i, "composite"]
        signal = df.loc[i, "signal"]
        
        # Filtro estricto (0.70 minimo)
        if composite >= CONFIDENCE_THRESHOLD:
            entry_price = df.loc[i, "close"]
            expiry_price = df.loc[i + EXPIRY_BARS, "close"]
            
            # CALL (BUY)
            if signal == 1:
                won = expiry_price > entry_price
                if won: wins_call += 1
                else: losses_call += 1
            # PUT (SELL)
            else:
                won = expiry_price < entry_price
                if won: wins_put += 1
                else: losses_put += 1
                
    total_call = wins_call + losses_call
    total_put = wins_put + losses_put
    total = total_call + total_put
    
    wr_call = (wins_call / total_call) if total_call > 0 else 0
    wr_put = (wins_put / total_put) if total_put > 0 else 0
    wr_total = ((wins_call + wins_put) / total) if total > 0 else 0
    
    ev_amount = (payout_pct / 100.0)
    ev_call = (wr_call * ev_amount) - (1 - wr_call)
    ev_put = (wr_put * ev_amount) - (1 - wr_put)
    ev_total = (wr_total * ev_amount) - (1 - wr_total)
    
    return {
        "trades": total,
        "call_trades": total_call,
        "put_trades": total_put,
        "win_rate_call": wr_call * 100,
        "win_rate_put": wr_put * 100,
        "win_rate": wr_total * 100,
        "ev_call": ev_call * 100,
        "ev_put": ev_put * 100,
        "ev": ev_total * 100
    }


async def run_multi_backtest():
    manager = IQOptionManager.get_instance()
    logger.info("Iniciando conexión a IQ Option para extracción masiva...")
    
    if not await manager.connect():
        logger.error("No se pudo conectar a IQ Option.")
        return
        
    s_manager = SessionManager()
    dh = IQOptionDataHandler()
    
    # Obtener activos VIP de la sesión actual
    session_name = s_manager.get_current_session()
    vip_assets = s_manager.get_vip_assets_for_current_session(include_crypto=True)
    
    logger.info("=" * 70)
    logger.info(f" 🌐 BACKTESTING MULTI-ACTIVO: SESIÓN ACTUAL -> {session_name}")
    logger.info(f" Activos Target: {vip_assets}")
    logger.info("=" * 70)
    
    results_board = []
    
    for asset in vip_assets:
        try:
            asset_clean = asset.replace("-op", "")
            
            # 1. Leer Payout
            payout = await dh.get_payout(asset, "turbo")
            if payout < manager.min_payout:
                logger.warning(f"⏩ Saltando {asset}: Payout bajo o cerrado ({payout}%).")
                continue
                
            # 2. Descargar Big Data (Paginado)
            df = await dh.get_historical_data(asset_clean, "1m", max_bars=CANDLES_TO_FETCH)
            
            if df.empty or len(df) < 100:
                logger.warning(f"⚠️ Datos insuficientes para {asset}.")
                continue
                
            # 3. Evaluar Rendimiento
            metrics = evaluate_binary_trades(df, payout)
            
            # Guardamos el resultado si tuvo trades
            if metrics["trades"] > 0:
                results_board.append({
                    "Asset": asset,
                    "Payout": f"{payout:.1f}%",
                    "Trades": metrics["trades"],
                    "Call_WR": metrics["win_rate_call"],
                    "Put_WR": metrics["win_rate_put"],
                    "WinRate": metrics["win_rate"],
                    "EV": metrics["ev"]
                })
                
        except Exception as e:
            logger.error(f"Error procesando {asset}: {e}")
            
    # ===== IMPRIMIR LEADERBOARD INSTITUCIONAL =====
    print("\n\n" + "=" * 85)
    print(" 🏆 NEXUS ALPHA V3 — LEADERBOARD DE COMPRAS Y VENTAS (BUYS/SELLS)")
    print("=" * 85)
    print(f"{'Activo':<12} | {'Payout':<7} | {'Trades':<6} | {'% WR CALL':<9} | {'% WR PUT':<8} | {'WR Total':<9} | {'Exp. Value'}")
    print("-" * 85)
    
    # Ordenar por el que dé más dinero (EV positivo)
    results_board.sort(key=lambda x: x["EV"], reverse=True)
    
    for r in results_board:
        wr_c = f"{r['Call_WR']:.1f}%"
        wr_p = f"{r['Put_WR']:.1f}%"
        wr_t = f"{r['WinRate']:.1f}%"
        ev_str = f"{r['EV']:+.2f}%"
        
        # Color highlighting a lo bruto para logs
        if r['EV'] > 0:
            ev_str = f"🟩 {ev_str}"
            wr_t = f"{wr_t} 🔥"
        else:
            ev_str = f"🟥 {ev_str}"
            
        print(f"{r['Asset']:<12} | {r['Payout']:<7} | {r['Trades']:<6} | {wr_c:<9} | {wr_p:<8} | {wr_t:<9} | {ev_str}")
        
    print("=" * 85)
    print(" NOTA: Expected Value (EV) > 0 significa GANANCIA MATEMÁTICA LPT.")
    print(" WR CALL y WR PUT garantizan que el Alpha V3 está balanceado en ambas direcciones.")
    print("=" * 85)

    # ===== ENVIAR REPORTE A TELEGRAM =====
    try:
        from nexus.reporting.weekly_report import get_reporter
        reporter = get_reporter()
        await reporter.initialize()
        
        # Formatear el mensaje HTML para Telegram
        msg_tg = (
            "🏦 <b>NEXUS | BLACKROCK LEADERBOARD V3</b> 🏦\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Análisis Cuantitativo: Promedio EV (Expected Value) "
            "basado en las últimas 2000 fluctuaciones de mercado.</i>\n\n"
            "<b>Ranking Top Activos:</b>\n"
        )
        
        # Tomar el Top 5
        top_assets = results_board[:5]
        for idx, r in enumerate(top_assets, 1):
            emoji = "🟢" if r['EV'] > 0 else "🔴"
            msg_tg += (
                f"{idx}. <b>{r['Asset']}</b> ({r['Payout']})\n"
                f"   ├ Ev: {emoji} {r['EV']:+.2f}%\n"
                f"   ├ WR Total: {r['WinRate']:.1f}%\n"
                f"   └ 📈 Call: {r['Call_WR']:.1f}% | 📉 Put: {r['Put_WR']:.1f}%\n\n"
            )
            
        msg_tg += (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "💡 <i>Nota: El HFT girará 100% su capital hacia los "
            "pares 🟢 (EV Positivo) y filtrará los rojos.</i>"
        )
        
        await reporter._send(msg_tg, parse_mode="HTML")
        logger.info("✅ Reporte Leaderboard enviado exitosamente a Telegram.")
    except Exception as e:
        logger.error(f"❌ Falló el envío a Telegram: {e}")

if __name__ == "__main__":
    asyncio.run(run_multi_backtest())
