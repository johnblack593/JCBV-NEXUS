"""
Suite de Pruebas Unitarias para QuantRiskManager
Asegura la robustez matemática y funcional del Risk Manager sin dependencias externas.
"""

import sys
import os
import pytest
from unittest.mock import MagicMock

# Ajuste temporal del path para poder importar desde la raíz del proyecto
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from nexus.core.risk_manager import QuantRiskManager

@pytest.fixture
def risk_manager():
    """Retorna una instancia limpia del QuantRiskManager con mocks para evitar I/O externo."""
    # Usamos mock_redis y no enviamos execution_engine para probar solo la matemática / lógica local
    return QuantRiskManager(log_dir="/tmp/nexus_risk_logs", redis_client=None, execution_engine=None)

def test_circuit_breaker_activation(risk_manager):
    """
    Test 1: Verifica que un drawdown excesivo Active el Circuit Breaker.
    Simula drawdown de 20%, con un límite del 15%.
    """
    assert risk_manager.is_circuit_breaker_active() is False, "CB debería inicializar desactivado"
    
    # Simular una serie de perdidas (Drawdown = 20%, el maximo permitido es 15%)
    triggered = risk_manager.circuit_breaker_check(current_drawdown=0.20, max_dd=0.15)
    
    assert triggered is True, "El chequeo del circuit breaker debería retornar True"
    assert risk_manager.is_circuit_breaker_active() is True, "El estado del CB interno debe confirmar que está activo"
    
    # Limpiamos el estado al terminar el test para no afectar otros
    risk_manager._clear_cb_state()

def test_kelly_criterion(risk_manager):
    """
    Test 2: Verifica el cálculo del Kelly Criterion y su Cap (Max Fraction).
    Fórmula: (W/L ratio * WinRate - LossRate) / (W/L ratio)
    """
    # Caso 1: Favorable (Win rate 60%, Avg Win 2.0, Avg Loss 1.0)
    # f* = (2.0 * 0.60 - 0.40) / 2.0 = (1.20 - 0.40) / 2.0 = 0.40
    # Como el manager tiene un Cap en 0.25 (KELLY_MAX_FRACTION), retornará 0.25
    kelly_favorable = risk_manager.kelly_criterion(win_rate=0.60, avg_win=2.0, avg_loss=1.0)
    assert kelly_favorable == risk_manager.KELLY_MAX_FRACTION, f"Debería estar limitado al cap {risk_manager.KELLY_MAX_FRACTION}"
    
    # Caso 2: Favorable por debajo del cap (Win rate 55%, Avg Win 1.2, Avg Loss 1.0)
    # f* = (1.2 * 0.55 - 0.45) / 1.2 = (0.66 - 0.45) / 1.2 = 0.175
    kelly_mid = risk_manager.kelly_criterion(win_rate=0.55, avg_win=1.2, avg_loss=1.0)
    assert round(kelly_mid, 4) == 0.1750, "Debería calcular el kelly fraccionario puro (0.175)"
    
    # Caso 3: Desfavorable (Win rate 40%, Avg Win 1.0, Avg Loss 1.0)
    # No hay edge real, debe retornar 0.0
    kelly_unfavorable = risk_manager.kelly_criterion(win_rate=0.40, avg_win=1.0, avg_loss=1.0)
    assert kelly_unfavorable == 0.0, "Si la esperanza matemática es inferior a 0, kelly debe retornar 0.0"

def test_atr_position_sizing(risk_manager):
    """
    Test 3: Verifica que a mayor volatilidad (ATR), menor es el tamaño de la posición,
    y viceversa.
    """
    capital = 10000.0
    current_price = 50000.0  # e.g., BTC
    risk_per_trade = 0.01  # 1% del capital (100 USD en riesgo)
    
    # Volatilidad baja
    atr_low = 1000.0
    size_low_volatility = risk_manager.atr_position_size(
        capital=capital,
        atr=atr_low,
        current_price=current_price,
        risk_per_trade=risk_per_trade,
        atr_multiplier=2.0
    )
    
    # Volatilidad media
    atr_mid = 2500.0
    size_mid_volatility = risk_manager.atr_position_size(
        capital=capital,
        atr=atr_mid,
        current_price=current_price,
        risk_per_trade=risk_per_trade,
        atr_multiplier=2.0
    )
    
    # Volatilidad extrema (deberia golpear los limites mínimos del clamp, e.g. 1.0%)
    atr_high = 30000.0
    size_high_volatility = risk_manager.atr_position_size(
        capital=capital,
        atr=atr_high,
        current_price=current_price,
        risk_per_trade=risk_per_trade,
        atr_multiplier=2.0
    )
    
    # Afirmamos que a medida que la volatilidad (ATR) sube, la posición (%) debe reducirse
    assert size_low_volatility > size_mid_volatility, "Con ATR bajo la posición debe ser mayor que con ATR medio"
    assert size_mid_volatility > size_high_volatility, "Con ATR alto la posición debe ser más conservadora"
    assert size_high_volatility == 1.0, "La posición debe haber golpeado el suelo mínimo del 1.0%"

if __name__ == "__main__":
    # Permite correr los tests unitarios directamente sin invocar `pytest` con CLI if desired.
    pytest.main(["-v", __file__])
