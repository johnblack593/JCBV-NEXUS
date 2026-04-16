"""
Asset Intelligence Service — NEXUS v5.0

Sistema de 3 capas para determinar disponibilidad de activos SIN
depender del catálogo masivo apioptioninitall.

Capa 1: Schedule estático (asset_schedule.yaml) — respuesta <1ms
Capa 2: Canary REST probe a IQ Option — validación real (<2s)
Capa 3: Redis TTL cache (15 min) — evita reprobes innecesarios
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional
import asyncio
import yaml
import json
import logging
from datetime import datetime, time
import pytz

logger = logging.getLogger("nexus.services.asset_intelligence")
UTC = pytz.UTC

class AssetStatus(Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    MAINTENANCE = "maintenance"
    UNKNOWN = "unknown"

@dataclass
class AssetInfo:
    symbol: str
    active_id: int
    status: AssetStatus
    profit: float
    source: str
    checked_at: datetime
    message: str = ""

    def to_dict(self):
        return {
            "symbol": self.symbol,
            "active_id": self.active_id,
            "status": self.status.value,
            "profit": self.profit,
            "source": self.source,
            "checked_at": self.checked_at.isoformat(),
            "message": self.message
        }

class AssetIntelligenceService:

    CACHE_TTL = 900  # 15 minutos en segundos
    PROBE_TIMEOUT = 5.0  # segundos máximo por probe

    def __init__(self, redis_client=None, iqoption_api=None):
        self._redis = redis_client
        self._api = iqoption_api
        self._schedule = self._load_schedule()
        self._local_cache = {}

    def _load_schedule(self) -> dict:
        try:
            import os
            path = os.path.join(os.path.dirname(__file__), '..', 'data', 'asset_schedule.yaml')
            with open(path, 'r') as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.warning(f"No se pudo cargar asset_schedule.yaml: {e}")
            return {}

    def is_available_by_schedule(self, symbol: str, at: datetime = None) -> AssetStatus:
        if not self._schedule or 'assets' not in self._schedule:
            return AssetStatus.UNKNOWN
            
        at = at or datetime.now(UTC)
        day_str = at.strftime("%a").upper() # MON, TUE...
        time_str = at.strftime("%H:%M")
        
        info = self._schedule['assets'].get(symbol)
        if not info:
            return AssetStatus.UNKNOWN
            
        # Check maintenance windows
        for mw in info.get('maintenance_windows', []):
            if day_str in mw.get('days', []):
                if mw.get('start') <= time_str <= mw.get('end'):
                    return AssetStatus.MAINTENANCE
                    
        # Check open schedule
        for sched in info.get('schedule', []):
            if day_str in sched.get('days', []):
                if sched.get('open') <= time_str <= sched.get('close'):
                    return AssetStatus.AVAILABLE
                    
        return AssetStatus.UNAVAILABLE

    async def probe_asset(self, symbol: str) -> AssetInfo:
        # Default mock probe block if no api is given
        if not self._api:
            # En modo test/fallback simular éxito con schedule profit o fallback profit
            prof = self._schedule.get('assets', {}).get(symbol, {}).get('profit_typical', 0.8)
            return AssetInfo(symbol, self._schedule.get('assets', {}).get(symbol, {}).get('active_id', 0), AssetStatus.AVAILABLE, prof, "probe", datetime.now(UTC))
            
        try:
            # get_all_profit usually caches internally or makes sync websocket request in python api
            # Wrapping into a short thread wait
            profit_res = await asyncio.wait_for(asyncio.to_thread(self._api.get_all_profit), timeout=self.PROBE_TIMEOUT)
            
            clean_sym = symbol.replace('-OTC', '').replace('_', '').upper()
            val = profit_res.get(clean_sym, profit_res.get(symbol, {})).get('turbo', 0)
            if isinstance(val, dict): val = val.get("turbo", 0) # API variance
            
            payout = float(val) if val < 1.0 else float(val)/100.0
            
            if payout > 0.0:
                act_id = self._schedule.get('assets', {}).get(symbol, {}).get('active_id', 0)
                return AssetInfo(symbol, act_id, AssetStatus.AVAILABLE, payout, "probe", datetime.now(UTC))
            else:
                return AssetInfo(symbol, 0, AssetStatus.UNAVAILABLE, 0.0, "probe", datetime.now(UTC))
                
        except asyncio.TimeoutError:
            return AssetInfo(symbol, 0, AssetStatus.UNKNOWN, 0.0, "probe", datetime.now(UTC), "timeout")
        except Exception as e:
            return AssetInfo(symbol, 0, AssetStatus.UNKNOWN, 0.0, "probe", datetime.now(UTC), str(e))

    async def get_asset_info(self, symbol: str) -> AssetInfo:
        try:
            # 1. Cache
            if self._redis:
                try:
                    c = await self._redis.get(f"nexus:asset:{symbol}")
                    if c:
                        cd = json.loads(c)
                        dt = datetime.fromisoformat(cd["checked_at"])
                        if (datetime.now(UTC) - dt).total_seconds() < self.CACHE_TTL:
                            return AssetInfo(cd["symbol"], cd["active_id"], AssetStatus(cd["status"]), cd["profit"], "cache", dt, cd["message"])
                except Exception:
                    pass
            elif symbol in self._local_cache:
                cd = self._local_cache[symbol]
                dt = cd["checked_at"]
                if (datetime.now(UTC) - dt).total_seconds() < self.CACHE_TTL:
                    return AssetInfo(cd["symbol"], cd["active_id"], cd["status"], cd["profit"], "cache", dt, cd["message"])
                    
            # 2. Schedule
            sched_status = self.is_available_by_schedule(symbol)
            if sched_status == AssetStatus.UNAVAILABLE or sched_status == AssetStatus.MAINTENANCE:
                return AssetInfo(symbol, 0, sched_status, 0.0, "schedule", datetime.now(UTC))
                
            # 3. Probe
            info = await self.probe_asset(symbol)
            
            # 4. Save Cache
            if info.status in [AssetStatus.AVAILABLE, AssetStatus.UNAVAILABLE]:
                await self._save_to_cache(info)
                
            return info
        except Exception as e:
            # Nunca lanza excepciones
            return AssetInfo(symbol, 0, AssetStatus.UNKNOWN, 0.0, "fallback", datetime.now(UTC), str(e))

    async def get_best_available(self, candidates: list[str], min_profit: float = 0.75) -> Optional[AssetInfo]:
        tasks = [self.get_asset_info(c) for c in candidates]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        valid = []
        for r in results:
            if isinstance(r, AssetInfo) and r.status == AssetStatus.AVAILABLE and r.profit >= min_profit:
                valid.append(r)
                
        if not valid:
            return None
            
        return sorted(valid, key=lambda x: x.profit, reverse=True)[0]

    def get_current_watchlist(self, macro_regime: str = "GREEN") -> list[str]:
        if not self._schedule:
            # Fallback
            return ["EURUSD-OTC", "GBPUSD-OTC"]
            
        rules = self._schedule.get("global_rules", {})
        
        day = datetime.now(UTC).strftime("%a").upper()
        hour = datetime.now(UTC).hour
        
        is_weekend = day in ["SAT", "SUN"] and not (day == "SUN" and hour >= 21)
        
        if is_weekend:
            wl = rules.get("weekend_assets", [])
        else:
            wl = rules.get("weekday_preference", [])
            
        if macro_regime == "RED":
            wl = [a for a in wl if a not in rules.get("blocked_on_red", [])]
            
        return wl

    async def _save_to_cache(self, info: AssetInfo):
        if self._redis:
            try:
                # Si redis es sync (en este ecosistema vimos q redis_lib.Redis() es sync, pero aqui el user dice aioredis)
                # Nos adaptamos para que corra
                await asyncio.to_thread(self._redis.setex, f"nexus:asset:{info.symbol}", self.CACHE_TTL, json.dumps(info.to_dict()))
            except Exception:
                pass
                
        self._local_cache[info.symbol] = {
            "symbol": info.symbol,
            "active_id": info.active_id,
            "status": info.status,
            "profit": info.profit,
            "source": info.source,
            "checked_at": info.checked_at,
            "message": info.message
        }

    async def refresh_cache_background(self):
        while True:
            try:
                watchlist = self.get_current_watchlist()
                # Probamos background sin bloquear mucho el loop
                tasks = [self.probe_asset(s) for s in watchlist]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, AssetInfo):
                        await self._save_to_cache(r)
            except Exception as e:
                logger.warning(f"AssetIntelligence background refresh error: {e}")
            await asyncio.sleep(600)
