"""
NEXUS Trading System — IQ Option Engine
=====================================
Motor completo nativo para IQ Option.
Maneja tanto la obtención de datos en tiempo real (Tick-Perfect)
como la ejecución automatizada de opciones binarias y digitales.

Basado en `iqoptionapi`.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from dotenv import load_dotenv

# Dependencia comunitaria The unofficial IQ Option wrapper
try:
    from iqoptionapi.stable_api import IQ_Option
except ImportError:
    IQ_Option = None
    logging.warning("No se encontró la librería iqoptionapi. Ejecución IQ desactivada.")

logger = logging.getLogger("nexus.iqoption")


class IQOptionManager:
    """
    Clase central que inicializa y mantiene viva la sesión WebSocket
    con IQ Option para ser consumida tanto por Datos como por Ejecución.
    """
    
    _instance = None
    
    @classmethod
    def get_instance(cls) -> "IQOptionManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        load_dotenv()
        self.email = os.getenv("IQ_OPTION_EMAIL", "")
        self.password = os.getenv("IQ_OPTION_PASSWORD", "")
        self.account_type = os.getenv("IQ_OPTION_ACCOUNT_TYPE", "PRACTICE").upper()
        self.min_payout = int(os.getenv("IQ_MIN_PAYOUT", "80"))
        
        self.api: Optional[IQ_Option] = None
        self.is_connected = False
        self._lock = asyncio.Lock()
        
    async def connect(self) -> bool:
        if self.is_connected and self.api and self.api.check_connect():
            return True
            
        if not IQ_Option:
            logger.error("iqoptionapi no instalado.")
            return False
            
        async with self._lock:
            # Checkeo doble dentro del lock
            if self.is_connected and self.api and self.api.check_connect():
                return True
                
            logger.info(f"Intentando conectar a IQ Option con {self.email}...")
            # En iqoptionapi, las peticiones pueden ser síncronas bajo el capó.
            # Lo envolvemos en to_thread si queremos, pero de momento llamamos directo,
            # ya que IQ_Option() crea threads en background
            self.api = IQ_Option(self.email, self.password)
            
            # Ejecutamos el connect asincrónicamente
            check, reason = await asyncio.to_thread(self.api.connect)
            
            if check:
                self.is_connected = True
                logger.info("✅ Conexión establecida con IQ Option.")
                
                # Seleccionar cuenta
                balance_mode = "PRACTICE" if self.account_type == "PRACTICE" else "REAL"
                await asyncio.to_thread(self.api.change_balance, balance_mode)
                
                logger.info(f"🏦 Cuenta activa: {balance_mode}")
                return True
            else:
                logger.error(f"❌ Fallo al conectar: {reason}")
                
                # Manejo simple de 2FA o contraseñas
                if reason == '2FA':
                    logger.critical("2FA ACTIVADO - Debes enviar el código SMS.")
                return False

    async def get_balance(self) -> float:
        if not await self.connect():
            return 0.0
        return await asyncio.to_thread(self.api.get_balance)


class IQOptionDataHandler:
    """
    Controlador de datos. Reemplaza la lógica ineficiente de YFinance
    usando los endpoints directos e institucionales de IQ Option.
    """
    
    def __init__(self):
        self.manager = IQOptionManager.get_instance()
    
    def _tf_to_seconds(self, tf: str) -> int:
        mapping = {
            "1m": 60,
            "2m": 120,
            "3m": 180,
            "4m": 240,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600
        }
        return mapping.get(tf, 60)

    async def get_historical_data(
        self, asset: str, tf: str, max_bars: int = 1000
    ) -> pd.DataFrame:
        """
        Descarga datos OHLCV históricos con soporte para Paginación Profunda.
        """
        if not await self.manager.connect():
            return pd.DataFrame()

        size = self._tf_to_seconds(tf)
        end_from_time = int(time.time())
        
        logger.info(f"📥 Descargando {max_bars} velas de {asset} ({tf}) con paginación...")
        
        all_candles = []
        remaining = max_bars
        
        # Paginación (IQ Option devuelve max ~1000 por request, a veces el servidor recorta a 600-800)
        while remaining > 0:
            batch_size = min(remaining, 1000)
            candles = await asyncio.to_thread(
                self.manager.api.get_candles, asset, size, batch_size, end_from_time
            )
            
            if not candles:
                break
                
            # Agregar a nuestra lista total
            all_candles.extend(reversed(candles)) # Guardamos del más reciente al más antiguo
            
            # El último elemento de la lista entregada por la API es el más antiguo
            oldest_time = candles[0]['from']
            end_from_time = oldest_time - 1
            remaining -= len(candles)
            
            # Si trajimos muy pocas, IQ option no tiene más historia para ese activo/temporalidad
            if len(candles) < (batch_size * 0.5):
                logger.warning(f"Límite del historial alcanzado para {asset}")
                break
                
            await asyncio.sleep(0.5) # Respetar rate-limit del broker
            
        if not all_candles:
            return pd.DataFrame()
            
        # Revertir todo para orden cronológico normal (antiguas -> futuras)
        all_candles.reverse()
            
        df = pd.DataFrame(all_candles)
        
        # Adaptar el DataFrame al formato NEXUS
        df['open_time'] = pd.to_datetime(df['from'], unit='s', utc=True)
        df['close_time'] = pd.to_datetime(df['to'], unit='s', utc=True)
        df.rename(columns={'min': 'low', 'max': 'high'}, inplace=True)
        
        # Evitar duplicados (a veces la paginación superpone la frontera por 1 segundo)
        df = df.drop_duplicates(subset=['open_time'])
        df = df.sort_values(by="open_time").reset_index(drop=True)
        
        df = df[['open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time']]
        
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
            
        logger.info(f"✅ Descargadas {len(df)} velas limpias para {asset}.")
        return df
        
    async def get_payout(self, asset: str, option_type: str = "turbo") -> float:
        """
        Retorna el porcentaje de ganancia (payout) activo.
        """
        if not await self.manager.connect():
            return 0.0
            
        # Obtenemos todos los payouts
        payouts = await asyncio.to_thread(self.manager.api.get_all_profit)
        
        try:
            val = payouts.get(asset, {}).get(option_type, 0)
            # A veces viene como decimal (0.85) a veces el API viejo lo da entero
            if val < 1.0 and val > 0:
                return float(val * 100)
            return float(val * 100 if val < 10 else val) # Asegurarnos que es un %
        except Exception:
            try:
                # Recurso nativo, dependiendo versión de la API
                if option_type == "turbo":
                    # iqoptionapi version profit
                    prof = self.manager.api.get_all_profit()
                    return prof.get(asset, {}).get('turbo', 0.0) * 100
            except:
                pass
            return 0.0


class IQOptionExecutionEngine:
    """
    Motor de ejecución para enviar órdenes CALL/PUT directamente.
    """
    
    def __init__(self):
        self.manager = IQOptionManager.get_instance()
    
    async def place_binary_order(
        self, 
        asset: str, 
        action: str, 
        amount: float, 
        exp_minutes: int
    ) -> Dict[str, Any]:
        """
        Envía orden de Opciones Binarias o Turbo.
        action = "call" o "put"
        exp_minutes = 1, 2, 3, 4, 5
        """
        if not await self.manager.connect():
            return {"status": "error", "message": "No connection to IQ Option"}
            
        action = action.lower()
        if action not in ["call", "put"]:
            return {"status": "error", "message": "Invalid action. Use 'call' or 'put'."}
            
        # Revisar payout antes de invertir
        dh = IQOptionDataHandler()
        payout = await dh.get_payout(asset, "turbo")
        
        if payout < self.manager.min_payout:
            logger.warning(f"⚠️ Payout bajo para {asset} ({payout}% < {self.manager.min_payout}%). Orden abortada.")
            return {"status": "rejected", "message": f"Low payout: {payout}%"}
            
        logger.info(f"🚀 Enviando orden {action.upper()} | {asset} | ${amount} | Exp: {exp_minutes}m (Payout actual: {payout}%)")
        
        # En opciones 'turbo' usualmente se usan enteros para decir "a los N minutos"
        # buy(money, active, action, expirations)
        asset_clean = asset.replace("-op", "")
        check, id_req = await asyncio.to_thread(
            self.manager.api.buy, amount, asset_clean, action, exp_minutes
        )
        
        if check:
            logger.info(f"✅ Orden Ejecutada Exitosamente! Ticket ID: {id_req}")
            return {
                "status": "filled",
                "order_id": id_req,
                "asset": asset,
                "action": action,
                "amount": amount,
                "exp": exp_minutes,
                "payout": payout,
                "timestamp": datetime.now(timezone.utc)
            }
        else:
            logger.error(f"❌ Falló ejecución de orden: {id_req}") # id contains reason usually
            return {"status": "error", "message": str(id_req)}
