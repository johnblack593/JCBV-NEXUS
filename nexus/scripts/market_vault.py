"""
NEXUS Trading System — The Universal Market Data Vault
=========================================================
Motor de extracción Multi-Timeframe (MTF) para Análisis Fractal.
Usa yfinance como fuente universal de datos (Crypto + Forex + Stocks).

Estructura de salida:
    nexus/data/vault/{SYMBOL}/{TIMEFRAME}.csv

Uso:
    python market_vault.py --symbol BTC-USD --years 2
    python market_vault.py --symbol BTC-USD --years 2 --mode crypto
    python market_vault.py --symbol EURUSD=X --years 2 --mode binary
"""

import os
import sys
import json
import argparse
import logging
import requests
import time
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logger = logging.getLogger("nexus.market_vault")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

try:
    import yfinance as yf
except ImportError:
    logger.error("yfinance no está instalado. Instálalo con: pip install yfinance")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════════════
#  Mapeo de temporalidades
# ══════════════════════════════════════════════════════════════════════

# Yahoo Finance restricciones REALES (con margen de seguridad de 2 días):
#   1m: max 7 días (real ~7)
#   5m/15m/30m: max 60 días (real ~58)
#   1h: max 730 días (real ~728, Yahoo rechaza en el límite exacto)
#   1d: sin límite práctico
YF_INTERVAL_MAP = {
    "1m":  {"interval": "1m",  "max_days": 5,     "label": "1 Minuto"},
    "5m":  {"interval": "5m",  "max_days": 58,    "label": "5 Minutos"},
    "15m": {"interval": "15m", "max_days": 58,    "label": "15 Minutos"},
    "30m": {"interval": "30m", "max_days": 58,    "label": "30 Minutos"},
    "1h":  {"interval": "1h",  "max_days": 710,   "label": "1 Hora"},
    "4h":  {"interval": "1h",  "max_days": 710,   "label": "4 Horas (resample from 1h)"},
    "1d":  {"interval": "1d",  "max_days": 99999, "label": "Diario"},
}

# Binance API Limits: 1000 candles per request
BINANCE_INTERVAL_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "4h": "4h", "1d": "1d"
}

CRYPTO_TIMEFRAMES = ["5m", "15m", "30m", "1h", "4h", "1d"]
BINARY_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h"]

SYMBOL_ALIASES = {
    "BTCUSDT": "BTC-USD",
    "ETHUSDT": "ETH-USD",
    "EURUSD":  "EURUSD=X",
    "GBPUSD":  "GBPUSD=X",
    "USDJPY":  "USDJPY=X",
}

# ══════════════════════════════════════════════════════════════════════
#  Market Data Vault (Multi-Provider)
# ══════════════════════════════════════════════════════════════════════

class MarketDataVault:
    """
    Motor institucional de extracción Multi-Timeframe con Cache Inteligente.
    Providers: yfinance (Universal), binance (Crypto HFT).
    """

    def __init__(self) -> None:
        self.base_dir = os.path.join(os.path.dirname(__file__), "..", "data", "vault")
        os.makedirs(self.base_dir, exist_ok=True)

    @staticmethod
    def _resolve_yf_symbol(symbol: str) -> str:
        return SYMBOL_ALIASES.get(symbol.upper(), symbol)

    @staticmethod
    def _resolve_binance_symbol(symbol: str) -> str:
        s = symbol.upper().replace("-", "").replace("=X", "")
        if s == "BTCUSD": return "BTCUSDT"
        if s == "ETHUSD": return "ETHUSDT"
        return s

    def _download_yfinance(self, yf_symbol: str, tf_label: str, tf_info: dict, target_days: int) -> pd.DataFrame:
        actual_interval = tf_info["interval"]
        max_days = tf_info["max_days"]
        
        # CLAMP: No pedir más días de los que Yahoo permite para esta temporalidad
        if target_days > max_days:
            logger.info("   [YF] Clamp: %s solo permite %d días. Ajustando de %d → %d días.",
                        tf_label, max_days, target_days, max_days)
            target_days = max_days
            
        end_dt = datetime.utcnow()
        start_dt = end_dt - timedelta(days=target_days)
        
        if max_days >= target_days:
            ticker = yf.Ticker(yf_symbol)
            df = ticker.history(
                start=start_dt.strftime("%Y-%m-%d"),
                end=end_dt.strftime("%Y-%m-%d"),
                interval=actual_interval,
                auto_adjust=True,
            )
            return df
        else:
            all_frames = []
            current_start = start_dt
            while current_start < end_dt:
                current_end = min(current_start + timedelta(days=max_days - 1), end_dt)
                try:
                    ticker = yf.Ticker(yf_symbol)
                    chunk = ticker.history(
                        start=current_start.strftime("%Y-%m-%d"),
                        end=current_end.strftime("%Y-%m-%d"),
                        interval=actual_interval,
                        auto_adjust=True,
                    )
                    if chunk is not None and not chunk.empty:
                        all_frames.append(chunk)
                except Exception as e:
                    logger.warning("   [YF] Error en lote %s: %s", current_start.strftime("%Y-%m-%d"), e)
                current_start = current_end + timedelta(days=1)
                
            if not all_frames:
                return pd.DataFrame()
            df = pd.concat(all_frames)
            df = df[~df.index.duplicated(keep="first")]
            df.sort_index(inplace=True)
            return df

    def _download_binance(self, binance_symbol: str, tf_label: str, target_days: int) -> pd.DataFrame:
        interval = BINANCE_INTERVAL_MAP.get(tf_label)
        if not interval:
            logger.error(f"[Binance] Interval {tf_label} not supported.")
            return pd.DataFrame()
            
        end_ts = int(datetime.utcnow().timestamp() * 1000)
        start_ts = int((datetime.utcnow() - timedelta(days=target_days)).timestamp() * 1000)
        
        base_url = "https://api.binance.com/api/v3/klines"
        all_klines = []
        current_start = start_ts
        
        while current_start < end_ts:
            params = {
                "symbol": binance_symbol,
                "interval": interval,
                "startTime": current_start,
                "endTime": end_ts,
                "limit": 1000
            }
            try:
                res = requests.get(base_url, params=params, timeout=10)
                res.raise_for_status()
                data = res.json()
                if not data:
                    break
                all_klines.extend(data)
                current_start = data[-1][0] + 1  # Next timestamp after the last candle
                time.sleep(0.1) # Rate limit respect
            except Exception as e:
                logger.error(f"[Binance] Fallo en API: {e}")
                break
                
        if not all_klines:
            return pd.DataFrame()
            
        df = pd.DataFrame(all_klines, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "num_trades", "taker_base_vol", "taker_quote_vol", "ignore"
        ])
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
            
        return df

    def download_timeframe(self, symbol: str, tf_label: str, years: int = 2, provider: str = "yfinance", force: bool = False) -> str | None:
        safe_name = symbol.upper().replace("=", "").replace("-", "")
        vault_dir = os.path.join(self.base_dir, safe_name)
        os.makedirs(vault_dir, exist_ok=True)
        file_path = os.path.join(vault_dir, f"{tf_label}.csv")

        # 1. Cache Check (Evitar recargas masivas)
        if os.path.exists(file_path) and not force:
            size_mb = os.path.getsize(file_path) / (1024 * 1024)
            logger.info("⚡ [CACHE HIT] %s %s ya existe (%.2f MB). Usando local. (Usa --force para recargar)", symbol, tf_label, size_mb)
            return file_path

        target_days = years * 365
        logger.info("⛏️  [%s:%s] Extrayendo %s | Profundidad: %d años...", provider.upper(), symbol, tf_label, years)

        df = pd.DataFrame()
        needs_resample_4h = False

        if provider == "yfinance":
            tf_info = YF_INTERVAL_MAP.get(tf_label)
            if not tf_info:
                return None
            needs_resample_4h = (tf_label == "4h")
            yf_symbol = self._resolve_yf_symbol(symbol)
            df = self._download_yfinance(yf_symbol, tf_label, tf_info, target_days)
            
        elif provider == "binance":
            bin_symbol = self._resolve_binance_symbol(symbol)
            df = self._download_binance(bin_symbol, tf_label, target_days)

        if df is None or df.empty:
            logger.warning("❌ No se encontraron datos para %s en %s vía %s.", symbol, tf_label, provider)
            return None

        # Resample 1h → 4h si es yfinance
        if needs_resample_4h and provider == "yfinance" and not df.empty:
            logger.info("   Resampleando 1h → 4h...")
            df = df.resample("4h").agg({
                "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"
            }).dropna()

        # Normalization
        df.columns = [c.lower() for c in df.columns]
        df.index.name = "open_time"
        keep_cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
        df = df[keep_cols]

        df.to_csv(file_path)
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        logger.info("💾  Bóveda actualizada: %s (%d velas | %.2f MB)", file_path, len(df), size_mb)
        return file_path

    def build_fractal_matrix(self, symbol: str, years: int = 2, mode: str = "crypto", provider: str = "yfinance", force: bool = False) -> dict:
        timeframes = CRYPTO_TIMEFRAMES if mode == "crypto" else BINARY_TIMEFRAMES

        print()
        logger.info("═" * 60)
        logger.info("  EXTRACCIÓN FRACTAL: %s | %d Años | Modo: %s | Prov: %s", symbol, years, mode.upper(), provider.upper())
        logger.info("  Temporalidades: %s", ", ".join(timeframes))
        logger.info("═" * 60)

        results = {}
        for tf in timeframes:
            path = self.download_timeframe(symbol, tf, years=years, provider=provider, force=force)
            results[tf] = {"path": path, "status": "OK" if path else "SKIP/ERROR"}

        safe_name = symbol.replace("=", "").replace("-", "")
        meta_path = os.path.join(self.base_dir, safe_name, "_vault_meta.json")
        meta = {
            "symbol": symbol,
            "years": years,
            "mode": mode,
            "provider": provider,
            "downloaded_at": datetime.utcnow().isoformat(),
            "timeframes": {k: v["status"] for k, v in results.items()},
        }
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        print()
        logger.info("═" * 60)
        logger.info("  FRACTAL MATRIX COMPLETADA PARA %s", symbol)
        for tf, r in results.items():
            status_icon = "✅" if r["status"] == "OK" else "⚠️"
            logger.info("    %s %s: %s", status_icon, tf, r["status"])
        logger.info("═" * 60)

        return results

# ══════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="NEXUS Universal Market Vault — Extracción Fractal Multi-Timeframe",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--symbol", type=str, default="BTC-USD", help="Símbolo (ej: BTC-USD, BTCUSDT, EURUSD=X)")
    parser.add_argument("--years", type=int, default=2, help="Años de historia a descargar")
    parser.add_argument("--tf", type=str, default="ALL", help="Temporalidad (1m,5m,15m,30m,1h,4h,1d) o 'ALL'")
    parser.add_argument("--mode", type=str, default="crypto", choices=["crypto", "binary"], help="Set de TFs")
    parser.add_argument("--provider", type=str, default="yfinance", choices=["yfinance", "binance", "tv"], help="Fuente de datos")
    parser.add_argument("--force", action="store_true", help="Ignorar caché y re-descargar los datos")
    args = parser.parse_args()

    vault = MarketDataVault()

    if args.tf.upper() == "ALL":
        vault.build_fractal_matrix(args.symbol, args.years, args.mode, args.provider, args.force)
    else:
        vault.download_timeframe(args.symbol, args.tf.lower(), args.years, args.provider, args.force)
