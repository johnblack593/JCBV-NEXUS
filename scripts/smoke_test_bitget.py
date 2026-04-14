#!/usr/bin/env python3
"""
NEXUS v5.0 — Bitget Smoke Test
==============================
Validates the full Bitget pipeline path in dry-run mode.
Run before first paper trading session.

Usage:
    python scripts/smoke_test_bitget.py

Expected output: PASS for each check, FAIL with reason if broken.
"""

import asyncio
import os
import sys

# Force UTF-8 output on Windows (cp1252 cannot encode emoji/box chars)
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ensure project root is on sys.path for nexus imports
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# Force dry-run and Bitget venue for all tests
os.environ["DRY_RUN"]           = "True"
os.environ["EXECUTION_VENUE"]   = "BITGET"
os.environ["ACTIVE_STRATEGY"]   = "BITGET_TREND_SCALPER"
os.environ["ACTIVE_SESSIONS"]   = "BITGET"

PASS = "✅ PASS"
FAIL = "❌ FAIL"
results = []

def check(name: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    results.append((status, name, detail))
    print(f"{status} — {name}" + (f" | {detail}" if detail else ""))

async def run_smoke_tests() -> None:
    print("\n═══════════════════════════════════════")
    print("  NEXUS v5.0 — Bitget Smoke Test")
    print("═══════════════════════════════════════\n")

    # ── TEST 1: Strategy import and instantiation ──────────────────
    try:
        from nexus.core.strategies.bitget_trend_scalper import (
            BitgetTrendScalperStrategy,
        )
        strategy = BitgetTrendScalperStrategy()
        check("Strategy import", True, repr(strategy))
    except Exception as exc:
        check("Strategy import", False, str(exc))
        strategy = None

    # ── TEST 2: Strategy output contract ──────────────────────────
    if strategy:
        try:
            import pandas as pd
            import numpy as np

            # Minimal synthetic OHLCV (60 rows of 5m candles)
            n = 60
            close = 50000 + np.cumsum(np.random.randn(n) * 100)
            df = pd.DataFrame({
                "open":   close * 0.999,
                "high":   close * 1.002,
                "low":    close * 0.998,
                "close":  close,
                "volume": np.random.uniform(100, 500, n),
            })

            result = await strategy.analyze(df)
            has_signal = "signal" in result
            has_conf   = "confidence" in result
            has_sl     = "stop_loss"   in result
            has_tp     = "take_profit" in result
            has_atr    = result.get("indicators", {}).get("atr", 0) > 0

            check("Strategy output: signal key",     has_signal, result.get("signal"))
            check("Strategy output: confidence key", has_conf,   str(result.get("confidence")))
            check("Strategy output: stop_loss key",  has_sl,     str(result.get("stop_loss")))
            check("Strategy output: take_profit key",has_tp,     str(result.get("take_profit")))
            check("Strategy output: atr > 0",        has_atr,    str(result.get("indicators", {}).get("atr")))
        except Exception as exc:
            check("Strategy output contract", False, str(exc))

    # ── TEST 3: OpenPosition instantiation ────────────────────────
    try:
        from nexus.core.position_manager import OpenPosition
        pos = OpenPosition(
            asset="BTC/USDT:USDT",
            direction="buy",
            entry_price=50000.0,
            original_size=0.001,
            remaining_size=0.001,
            stop_loss=49250.0,
            take_profit=51500.0,
            atr_at_entry=250.0,
        )
        check("OpenPosition init",        True,        "")
        check("breakeven_active=False",   not pos.breakeven_active, "")
        check("partial_tier_1=False",     not pos.partial_tier_1,   "")
        check("partial_tier_2=False",     not pos.partial_tier_2,   "")
    except Exception as exc:
        check("OpenPosition init", False, str(exc))

    # ── TEST 4: Minimum size guard ─────────────────────────────────
    try:
        from nexus.core.position_manager import PositionManager
        # Mock engine with no connections
        class MockEngine:
            _exchange_futures = None
            _exchange_spot = None

        pm = PositionManager(MockEngine())
        os.environ["BITGET_MIN_ORDER_USDT"] = "5.0"

        # $4 notional should FAIL the guard
        fails = not pm._is_above_minimum_size(0.0001, 40000.0)   # $4 notional
        # $6 notional should PASS the guard
        passes = pm._is_above_minimum_size(0.00015, 40000.0)     # $6 notional
        check("Min size guard rejects $4 notional",  fails,  "$4 < $5 min")
        check("Min size guard accepts $6 notional",  passes, "$6 >= $5 min")
    except Exception as exc:
        check("Min size guard", False, str(exc))

    # ── TEST 5: Pipeline instantiation (BITGET venue) ─────────────
    try:
        from nexus.core.pipeline import NexusPipeline
        pipeline = NexusPipeline(venue="BITGET", strategy="BITGET_TREND_SCALPER")
        check("Pipeline init [BITGET]",       True,  "")
        check("Pipeline venue == BITGET",      pipeline.venue == "BITGET",       "")
        check("Pipeline config max_daily>=10", pipeline._config.get("max_daily_trades", 0) >= 10, "")
        check("Pipeline dry_run_mode",         pipeline.dry_run_mode,            "DRY_RUN=True")
    except Exception as exc:
        check("Pipeline init", False, str(exc))

    # ── TEST 6: Sizing math ────────────────────────────────────────
    try:
        import pandas as pd
        import numpy as np

        pipeline = NexusPipeline(venue="BITGET", strategy="BITGET_TREND_SCALPER")

        # Mock get_balance to return $100
        async def mock_balance(): return 100.0
        pipeline.execution_engine = type("MockEng", (), {
            "get_balance": mock_balance
        })()

        n = 10
        df = pd.DataFrame({
            "close": [50000.0] * n,
            "open":  [50000.0] * n,
            "high":  [50100.0] * n,
            "low":   [49900.0] * n,
            "volume":[100.0]   * n,
        })

        os.environ["BITGET_RISK_PCT_PER_TRADE"] = "0.01"
        os.environ["BITGET_LEVERAGE"]           = "5"
        os.environ["BITGET_MIN_ORDER_USDT"]     = "5.0"
        os.environ["BITGET_MAX_SIZE_USDT"]      = "50.0"

        size = await pipeline._calculate_size(0.7, df)
        # Expected: (100 * 0.01 * 5) / 50000 = 0.00001 BTC
        # But min_usdt=5, so notional=max(5, 1*5)=5, size=5/50000=0.0001
        expected = 5.0 / 50000.0
        close_enough = abs(size - expected) < 1e-8
        check("Bitget sizing math", close_enough,
              f"got={size:.8f} expected={expected:.8f}")
    except Exception as exc:
        check("Bitget sizing math", False, str(exc))

    # ── Summary ────────────────────────────────────────────────────
    print("\n═══════════════════════════════════════")
    total  = len(results)
    passed = sum(1 for r in results if r[0] == PASS)
    failed = total - passed
    print(f"  Results: {passed}/{total} passed | {failed} failed")
    if failed == 0:
        print("  🟢 System ready for paper trading on Bitget.")
    else:
        print("  🔴 Fix failing checks before paper trading.")
    print("═══════════════════════════════════════\n")
    sys.exit(0 if failed == 0 else 1)

if __name__ == "__main__":
    asyncio.run(run_smoke_tests())
