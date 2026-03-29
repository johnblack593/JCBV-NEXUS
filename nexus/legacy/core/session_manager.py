"""
NEXUS Trading System — Session Manager & VIP Asset Routing
======================================================
Maneja los horarios institucionales y devuelve qué activos
son óptimos para operar según la hora UTC y el día de la semana.

1. Sesión Oro (Londres/NY)
2. Sesión Asiática (Tokyo/Sydney)
3. Cripto 24/7
4. OTC Weekend
"""

from datetime import datetime, timezone
import logging
from typing import List, Dict

logger = logging.getLogger("nexus.session_manager")


class MarketSession:
    LONDON_NY = "LONDON_NY"
    ASIAN = "ASIAN"
    WEEKEND_OTC = "WEEKEND_OTC"


class SessionManager:
    """
    Ruta inteligentemente la estrategia de "Francotirador" hacia 
    los activos que matemáticamente ofrezcan las mejores condiciones de mercado.
    """
    
    # Categorización de pares nivel VIP (Alta Liquidez, Payout > 80%, Mínimo $1)
    # Seleccionados por volatilidad ideal para operaciones "Turbo" (1-5 min) y equilibrio "Buy/Sell"
    VIP_ASSETS = {
        MarketSession.LONDON_NY: [
            "EURUSD", "GBPUSD", "USDCHF", "USDCAD", "EURGBP", 
            "EURAUD", "EURJPY", "GBPJPY", "GBPCHF"
        ],
        MarketSession.ASIAN: [
            "USDJPY", "AUDUSD", "NZDUSD", "AUDJPY", "NZDJPY", 
            "CADJPY", "AUDCAD", "EURAUD"
        ],
        MarketSession.WEEKEND_OTC: [
            "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", 
            "AUDCAD-OTC", "EURGBP-OTC", "NZDUSD-OTC", "USDCHF-OTC"
        ],
        "CRYPTO": [
            "BTCUSD-op", "ETHUSD-op", "LTCUSD-op", "XRPUSD-op"
        ]
    }

    def __init__(self):
        pass
        
    def _is_weekend(self, dt: datetime) -> bool:
        """Sábado = 5, Domingo = 6 en weekday()"""
        # A veces el mercado FX cierra desde el viernes en la tarde, 
        # pero asumimos simplificadamente fines de semana
        return dt.weekday() >= 5
        
    def get_current_session(self, dt: datetime = None) -> str:
        if dt is None:
            dt = datetime.now(timezone.utc)
            
        if self._is_weekend(dt):
            return MarketSession.WEEKEND_OTC
            
        hour = dt.hour
        
        # London/NY Overlap generalemente va de 12:00 UTC a 16:00 UTC.
        # Ampliamos un poco: 08:00 (Apertura London) a 20:00 (NY)
        if 8 <= hour < 20:
            return MarketSession.LONDON_NY
        else:
            return MarketSession.ASIAN

    def get_vip_assets_for_current_session(self, include_crypto: bool = True) -> List[str]:
        """
        Retorna la lista de activos preferidos para operar AHORA.
        Crypto puede incluirse siempre ya que opera 24/7.
        """
        session = self.get_current_session()
        assets = list(self.VIP_ASSETS[session])
        
        if include_crypto:
            assets.extend(self.VIP_ASSETS["CRYPTO"])
            
        # Filtrar duplicados manteniendo orden
        seen = set()
        unique_assets = []
        for a in assets:
            if a not in seen:
                unique_assets.append(a)
                seen.add(a)
                
        #logger.info(f"🕒 Sesión actual: {session}. Activos VIP activos: {unique_assets}")
        return unique_assets
