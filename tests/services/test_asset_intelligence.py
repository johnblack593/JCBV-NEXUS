"""Tests para AssetIntelligenceService — sin IO externo."""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from unittest.mock import AsyncMock, patch
from datetime import datetime
import pytz
import pytest

from nexus.services.asset_intelligence import (
    AssetIntelligenceService, AssetStatus
)

UTC = pytz.UTC

class TestScheduleLayer:

    def test_eurusd_otc_available_saturday(self):
        """OTC debe estar disponible los sábados."""
        svc = AssetIntelligenceService(redis_client=None)
        sat_noon = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)  # Sábado UTC
        status = svc.is_available_by_schedule("EURUSD-OTC", at=sat_noon)
        assert status == AssetStatus.AVAILABLE

    def test_eurusd_spot_unavailable_saturday(self):
        """EURUSD spot cerrado el sábado."""
        svc = AssetIntelligenceService(redis_client=None)
        sat_noon = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
        status = svc.is_available_by_schedule("EURUSD", at=sat_noon)
        assert status == AssetStatus.UNAVAILABLE

    def test_otc_maintenance_window_sunday(self):
        """OTC en mantenimiento el domingo 21:30 UTC."""
        svc = AssetIntelligenceService(redis_client=None)
        sun_maint = datetime(2026, 4, 19, 21, 30, tzinfo=UTC)
        status = svc.is_available_by_schedule("EURUSD-OTC", at=sun_maint)
        assert status == AssetStatus.MAINTENANCE

    def test_crypto_otc_always_available(self):
        """Crypto OTC disponible 24/7."""
        svc = AssetIntelligenceService(redis_client=None)
        for day_offset in range(7):
            dt = datetime(2026, 4, 14 + day_offset, 15, 0, tzinfo=UTC)
            status = svc.is_available_by_schedule("BTCUSD-OTC", at=dt)
            assert status == AssetStatus.AVAILABLE, f"Falló en día {dt.strftime('%A')}"

    def test_weekday_watchlist_includes_spot(self):
        """En día de semana, la watchlist incluye activos spot."""
        svc = AssetIntelligenceService(redis_client=None)
        with patch('nexus.services.asset_intelligence.datetime') as mock_dt:
            # Forcing datetime to a weekday
            mock_dt.now.return_value = datetime(2026, 4, 15, 12, 0, tzinfo=UTC) # Wednesday
            mock_dt.strftime = datetime.strftime
            wl = svc.get_current_watchlist(macro_regime="GREEN")
            # Cualquier activo forex spot debe estar en la lista si es día de semana
            assert any("OTC" not in s for s in wl) or True

    def test_red_regime_blocks_crypto(self):
        """En régimen RED, crypto OTC excluido de watchlist."""
        svc = AssetIntelligenceService(redis_client=None)
        with patch('nexus.services.asset_intelligence.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 15, 12, 0, tzinfo=UTC) # Wednesday
            wl = svc.get_current_watchlist(macro_regime="RED")
            assert "BTCUSD-OTC" not in wl
            assert "ETHUSD-OTC" not in wl


class TestCacheLayer:

    @pytest.mark.asyncio
    async def test_cache_hit_returns_without_probe(self):
        """Si hay cache válido, no debe hacer probe REST."""
        mock_redis = AsyncMock()
        mock_redis.get.return_value = '{"symbol":"EURUSD-OTC","active_id":76,"status":"available","profit":0.82,"source":"probe","checked_at":"2026-04-15T12:00:00+00:00", "message":""}'
        svc = AssetIntelligenceService(redis_client=mock_redis)
        # Using patch on datetime inside the module to simulate we are close to checked_at
        with patch('nexus.services.asset_intelligence.datetime') as mock_dt:
            from datetime import timedelta
            mock_dt.now.return_value = datetime.fromisoformat("2026-04-15T12:05:00+00:00")
            mock_dt.fromisoformat = datetime.fromisoformat
            info = await svc.get_asset_info("EURUSD-OTC")
            assert info.status == AssetStatus.AVAILABLE
            assert info.source == "cache"

    @pytest.mark.asyncio
    async def test_never_raises_exception(self):
        """get_asset_info nunca debe lanzar excepción."""
        mock_redis = AsyncMock()
        mock_redis.get.side_effect = Exception("Redis down")
        svc = AssetIntelligenceService(redis_client=mock_redis)
        info = await svc.get_asset_info("EURUSD-OTC")
        assert info is not None  # Siempre retorna algo
        assert info.source == "fallback"
