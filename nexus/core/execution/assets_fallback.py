"""
Lista de activos de fallback para cuando el catálogo de IQ Option
no está disponible o el apioptioninitall aún no terminó.
Estos son los pares más estables y frecuentemente disponibles.
"""

import logging

logger = logging.getLogger("nexus.execution.assets")

FALLBACK_ASSETS = {
    "EURUSD-OTC": {
        "id": 76,
        "profit": 0.82,
        "min_bet": 1,
        "expiration_times": [1, 5, 15],
        "provider": "OTC",
    },
    "EURUSD": {
        "id": 1,
        "profit": 0.75,
        "min_bet": 1,
        "expiration_times": [1, 5, 15],
        "provider": "feed",
    },
    "GBPUSD-OTC": {
        "id": 77,
        "profit": 0.82,
        "min_bet": 1,
        "expiration_times": [1, 5, 15],
        "provider": "OTC",
    },
    "BTCUSD-OTC": {
        "id": 1941,
        "profit": 0.80,
        "min_bet": 1,
        "expiration_times": [1, 5, 15],
        "provider": "OTC",
    },
}

def get_asset_info(symbol: str, catalog: dict = None) -> dict:
    """
    Retorna info del activo. Usa el catálogo real si está disponible,
    sino usa el fallback.
    """
    if catalog and symbol in catalog:
        return catalog[symbol]
    if symbol in FALLBACK_ASSETS:
        logger.warning(f"Usando info de fallback para {symbol} — catálogo no disponible")
        return FALLBACK_ASSETS[symbol]
    raise ValueError(f"Activo {symbol} no encontrado en catálogo ni en fallback")
