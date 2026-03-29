# pyre-unsafe
"""
NEXUS Trading System — Full Project Audit
==========================================
Comprehensive test suite that validates all modules without requiring
external connections (no Binance API, no Telegram, no LLM providers).

Run:
    python tests/nexus_full_audit.py
"""

import os
import sys
import asyncio
import math
import logging
import importlib
from pathlib import Path
from datetime import datetime, timezone

# Suppress noisy logs during testing
logging.basicConfig(level=logging.WARNING)

# ── Path Setup ──
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import numpy as np  # type: ignore
import pandas as pd  # type: ignore


# ══════════════════════════════════════════════════════════════════════
#  Test Framework
# ══════════════════════════════════════════════════════════════════════

class TestRunner:
    """Simple test runner with pass/fail counting."""
    
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.errors = []
    
    def check(self, name: str, condition: bool, detail: str = ""):
        if condition:
            self.passed += 1
            print(f"  [OK]   {name}")
        else:
            self.failed += 1
            msg = f"  [FAIL] {name}: {detail}" if detail else f"  [FAIL] {name}"
            print(msg)
            self.errors.append(msg)
    
    def skip(self, name: str, reason: str = ""):
        self.skipped += 1
        print(f"  [SKIP] {name}: {reason}")
    
    def section(self, title: str):
        print(f"\n{'─' * 60}")
        print(f"  {title}")
        print(f"{'─' * 60}")

    def report(self):
        total = self.passed + self.failed + self.skipped
        print(f"\n{'═' * 60}")
        if self.failed == 0:
            print(f"  ✅ ALL TESTS PASSED: {self.passed}/{total} "
                  f"(skipped {self.skipped})")
        else:
            print(f"  ❌ FAILURES: {self.failed}/{total} "
                  f"(passed {self.passed}, skipped {self.skipped})")
            for err in self.errors:
                print(f"    {err}")
        print(f"{'═' * 60}\n")
        return self.failed == 0


# ══════════════════════════════════════════════════════════════════════
#  Helpers — Generate realistic OHLCV data
# ══════════════════════════════════════════════════════════════════════

def generate_ohlcv(n: int = 300, base: float = 40000.0) -> pd.DataFrame:
    """Generate realistic OHLCV DataFrame for testing."""
    np.random.seed(42)
    
    closes = [base]
    for _ in range(n - 1):
        ret = np.random.normal(0.0001, 0.005)
        closes.append(closes[-1] * (1 + ret))
    
    closes = np.array(closes)
    highs = closes * (1 + np.abs(np.random.normal(0, 0.002, n)))
    lows = closes * (1 - np.abs(np.random.normal(0, 0.002, n)))
    opens = np.roll(closes, 1)
    opens[0] = base
    volumes = np.random.uniform(100, 1000, n)
    
    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })
    return df


# ══════════════════════════════════════════════════════════════════════
#  SECTION 1: Import Validation
# ══════════════════════════════════════════════════════════════════════

def test_imports(t: TestRunner):
    t.section("1. IMPORT VALIDATION")
    
    modules = [
        ("core.signal_engine", "TechnicalSignalEngine"),
        ("core.risk_manager", "QuantRiskManager"),
        ("core.execution_engine", "ExecutionEngine"),
        ("core.execution_engine", "OrderRequest"),
        ("core.execution_engine", "OrderSide"),
        ("core.execution_engine", "OrderType"),
        ("core.ml_engine", "MLEngine"),
        ("core.sentiment_engine", "SentimentEngine"),
        ("core.evolutionary_agent", "EvolutionaryAgent"),
        ("agents.agent_bull", "AgentBull"),
        ("agents.agent_bear", "AgentBear"),
        ("agents.agent_arbitro", "AgentArbitro"),
        ("reporting.weekly_report", "NexusTelegramReporter"),
        ("config.settings", "trading_config"),
        ("config.settings", "risk_config"),
        ("config.settings", "ml_config"),
    ]
    
    for mod_name, cls_name in modules:
        try:
            mod = importlib.import_module(mod_name)
            obj = getattr(mod, cls_name)
            t.check(f"import {mod_name}.{cls_name}", obj is not None)
        except Exception as exc:
            t.check(f"import {mod_name}.{cls_name}", False, str(exc))


# ══════════════════════════════════════════════════════════════════════
#  SECTION 2: TechnicalSignalEngine
# ══════════════════════════════════════════════════════════════════════

def test_signal_engine(t: TestRunner):
    from core.signal_engine import TechnicalSignalEngine, SignalDirection  # type: ignore
    
    t.section("2. TECHNICAL SIGNAL ENGINE")
    
    engine = TechnicalSignalEngine()
    df = generate_ohlcv(300)
    
    # Test 1: generate_signal returns correct structure
    result = engine.generate_signal(df)
    t.check("generate_signal returns dict", isinstance(result, dict))
    t.check("has 'signal' key", "signal" in result)
    t.check("has 'confidence' key", "confidence" in result)
    t.check("has 'reason' key", "reason" in result)
    t.check("has 'timestamp' key", "timestamp" in result)
    t.check("has 'indicators' key", "indicators" in result)
    t.check("signal is BUY/SELL/HOLD", result["signal"] in ("BUY", "SELL", "HOLD"))
    t.check("confidence is 0-1", 0.0 <= result["confidence"] <= 1.0)
    
    # Test 2: empty DataFrame
    empty = engine.generate_signal(pd.DataFrame())
    t.check("empty df → HOLD", empty["signal"] == "HOLD")
    
    # Test 3: analyze() returns SignalOutput
    analysis = engine.analyze(df)
    t.check("analyze() returns SignalOutput", hasattr(analysis, "signal"))
    
    # Test 4: Dashboard
    dash = engine.get_dashboard(df)
    t.check("dashboard has 5 indicators", len(dash) == 5)
    
    # Test 5: set_weights
    engine.set_weights({"RSI": 1.0, "MACD": 1.0, "Bollinger": 1.0, "EMA_Cross": 1.0, "Volume": 1.0})
    t.check("set_weights succeeds", True)


# ══════════════════════════════════════════════════════════════════════
#  SECTION 3: QuantRiskManager
# ══════════════════════════════════════════════════════════════════════

def test_risk_manager(t: TestRunner):
    from core.risk_manager import QuantRiskManager  # type: ignore
    
    t.section("3. QUANT RISK MANAGER")
    
    rm = QuantRiskManager(log_dir=str(_ROOT / "logs"))
    
    # Kelly Criterion
    k1 = rm.kelly_criterion(0.55, 2.0, 1.0)
    t.check("kelly(0.55, 2.0, 1.0) = 0.25 (capped)", k1 == 0.25)
    
    k2 = rm.kelly_criterion(0.50, 1.0, 1.0)
    t.check("kelly(0.50, 1.0, 1.0) = 0.0 (breakeven)", k2 == 0.0)
    
    k3 = rm.kelly_criterion(0.40, 1.0, 1.0)
    t.check("kelly(0.40, 1.0, 1.0) = 0.0 (negative)", k3 == 0.0)
    
    # Monte Carlo
    mc = rm.monte_carlo_simulation([0.01] * 30, n=1000)
    t.check("MC p50 > initial capital", mc["p50"] > 10000.0)
    t.check("MC max_dd = 0 for constant positive", mc["max_dd"] == 0.0)
    
    mixed = rm.monte_carlo_simulation([0.02, -0.01, 0.015, -0.005, 0.01], n=2000)
    t.check("MC p5 < p50 < p95", mixed["p5"] < mixed["p50"] < mixed["p95"])
    
    # Value at Risk
    var_returns = [0.01, 0.02, -0.01, 0.005, -0.015, 0.03, -0.005, 0.01, -0.02, 0.015]
    var1 = rm.value_at_risk(var_returns, confidence=0.95)
    t.check("VaR(95%) returns float", isinstance(var1, float))
    
    # Circuit Breaker
    rm_cb = QuantRiskManager(log_dir=str(_ROOT / "logs"))
    cb_off = rm_cb.circuit_breaker_check(0.05, max_dd=0.15)
    t.check("CB 5% < 15% → False", cb_off is False)
    
    cb_on = rm_cb.circuit_breaker_check(0.18, max_dd=0.15)
    t.check("CB 18% >= 15% → True", cb_on is True)
    t.check("CB active after trigger", rm_cb.is_circuit_breaker_active() is True)
    
    # ATR Sizing
    atr1 = rm.atr_position_size(10000.0, 100.0, 2000.0, 0.01, 2.0)
    t.check("ATR Sizing normal -> 10.0%", atr1 == 10.0)

    # Correlation Penalty
    try:
        from scipy import stats  # type: ignore
        pos_identical = [{"symbol": "BTC"}]
        ret_data = {
            "BTC": [0.01, 0.02, -0.01, 0.015, -0.005],
            "ETH": [0.01, 0.02, -0.01, 0.015, -0.005],
        }
        cp = rm.correlation_penalty("ETH", pos_identical, ret_data, threshold=0.70)
        t.check("Identical correlation -> Penalty 0.0", cp == 0.0)
    except ImportError:
        t.skip("Correlation penalty", "scipy not installed")


# ══════════════════════════════════════════════════════════════════════
#  SECTION 4: Agents (Bull, Bear, Arbitro)
# ══════════════════════════════════════════════════════════════════════

def test_agents(t: TestRunner):
    from agents.agent_bull import AgentBull  # type: ignore
    from agents.agent_bear import AgentBear  # type: ignore
    from agents.agent_arbitro import AgentArbitro  # type: ignore
    from core.signal_engine import TechnicalSignalEngine  # type: ignore
    
    t.section("4. AGENTS (BULL, BEAR, ARBITRO)")
    
    df = generate_ohlcv(300)
    engine = TechnicalSignalEngine()
    signal = engine.generate_signal(df)
    price = float(df["close"].iloc[-1])
    
    # Bull
    bull = AgentBull()
    bull_result = bull.build_argument(price, signal)
    t.check("Bull returns stance='BULL'", bull_result["stance"] == "BULL")
    t.check("Bull has argument", isinstance(bull_result["argument"], str))
    t.check("Bull strength is int", isinstance(bull_result["strength"], int))
    t.check("Bull strength in range", 0 <= bull_result["strength"] <= 10)
    
    # Bear
    bear = AgentBear()
    bear_result = bear.build_argument(price, signal)
    t.check("Bear returns stance='BEAR'", bear_result["stance"] == "BEAR")
    t.check("Bear has argument", isinstance(bear_result["argument"], str))
    t.check("Bear strength is int", isinstance(bear_result["strength"], int))
    
    # Arbitro
    arbitro = AgentArbitro()
    arbitro.initialize()
    t.check("Arbitro initialized", True)
    
    decision = arbitro.deliberate(
        bull_state=bull.get_state(),
        bear_state=bear.get_state(),
        risk_metrics={"max_drawdown": 0.05, "var_95": 0.02, "current_exposure": 0.1},
        market_context={"symbol": "BTCUSDT", "price": price},
    )
    t.check("Arbitro returns dict", isinstance(decision, dict))
    t.check("Arbitro has 'decision' key", "decision" in decision)
    t.check("Arbitro has 'confidence' key", "confidence" in decision)
    t.check("Arbitro decision in BUY/SELL/HOLD",
            decision.get("decision", "") in ("BUY", "SELL", "HOLD"))


# ══════════════════════════════════════════════════════════════════════
#  SECTION 5: ExecutionEngine (offline mode)
# ══════════════════════════════════════════════════════════════════════

def test_execution_engine(t: TestRunner):
    from core.execution_engine import (  # type: ignore
        ExecutionEngine, OrderRequest, OrderSide, OrderType, OrderStatus,
        TWAPConfig, BinanceConnector,
    )
    
    t.section("5. EXECUTION ENGINE (OFFLINE)")
    
    # Can instantiate with empty keys (offline mode)
    ee = ExecutionEngine(api_key="", api_secret="")
    t.check("ExecutionEngine created (offline)", ee is not None)
    t.check("Not initialized without API keys", ee._initialized is False)
    
    # OrderRequest dataclass
    req = OrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=0.001,
    )
    t.check("OrderRequest created", req.symbol == "BTCUSDT")
    t.check("OrderRequest side", req.side == OrderSide.BUY)
    
    # TWAPConfig
    tc = TWAPConfig(total_quantity=1.0, num_slices=5)
    t.check("TWAPConfig created", tc.num_slices == 5)
    
    # Order history (empty)
    history = ee.get_order_history()
    t.check("Empty order history is DataFrame", isinstance(history, pd.DataFrame))
    t.check("Empty order history is empty", len(history) == 0)


# ══════════════════════════════════════════════════════════════════════
#  SECTION 6: MLEngine
# ══════════════════════════════════════════════════════════════════════

def test_ml_engine(t: TestRunner):
    from core.ml_engine import MLEngine  # type: ignore
    
    t.section("6. ML ENGINE")
    
    ml = MLEngine()
    t.check("MLEngine created", ml is not None)
    
    df = generate_ohlcv(200)
    
    # Predict (uses fallback if TensorFlow not installed)
    prediction = ml.predict(df)
    t.check("predict() returns dict", isinstance(prediction, dict))
    t.check("prediction has 'lstm' key", "lstm" in prediction)
    t.check("prediction has 'dqn' key", "dqn" in prediction)
    t.check("lstm has predicted_price", "predicted_price" in prediction.get("lstm", {}))
    t.check("combined_direction present", "combined_direction" in prediction)
    
    # should_retrain
    t.check("should_retrain() returns bool", isinstance(ml.should_retrain(), bool))
    
    # model metrics
    metrics = ml.get_model_metrics()
    t.check("get_model_metrics() returns dict", isinstance(metrics, dict))
    t.check("metrics has has_tensorflow", "has_tensorflow" in metrics)


# ══════════════════════════════════════════════════════════════════════
#  SECTION 7: SentimentEngine
# ══════════════════════════════════════════════════════════════════════

def test_sentiment_engine(t: TestRunner):
    from core.sentiment_engine import SentimentEngine  # type: ignore
    
    t.section("7. SENTIMENT ENGINE")
    
    se = SentimentEngine()
    t.check("SentimentEngine created", se is not None)
    
    # Analyze (uses fallback - no API keys required)
    async def _test_sentiment():
        try:
            result = await se.analyze("BTC")
            return result
        except Exception as exc:
            return exc
    
    result = asyncio.run(_test_sentiment())
    if isinstance(result, Exception):
        t.check("sentiment analyze() runs", False, str(result))
    else:
        t.check("sentiment analyze() runs", True)
        t.check("has overall_score", hasattr(result, "overall_score"))


# ══════════════════════════════════════════════════════════════════════
#  SECTION 8: EvolutionaryAgent
# ══════════════════════════════════════════════════════════════════════

def test_evolutionary_agent(t: TestRunner):
    from core.evolutionary_agent import EvolutionaryAgent  # type: ignore
    
    t.section("8. EVOLUTIONARY AGENT")
    
    agent = EvolutionaryAgent()
    t.check("EvolutionaryAgent created", agent is not None)
    
    # Sharpe calculation
    eq_up = list(np.linspace(10000, 12000, 200))
    sharpe_up = agent._calculate_sharpe(eq_up)
    t.check("Sharpe(up equity) > 0", sharpe_up > 0)
    
    eq_down = list(np.linspace(10000, 8000, 200))
    sharpe_down = agent._calculate_sharpe(eq_down)
    t.check("Sharpe(down equity) < 0", sharpe_down < 0)
    
    eq_flat = [10000.0] * 50
    sharpe_flat = agent._calculate_sharpe(eq_flat)
    t.check("Sharpe(flat equity) = 0", sharpe_flat == 0.0)
    
    # Conservative mode
    from core.risk_manager import QuantRiskManager  # type: ignore
    rm = QuantRiskManager(log_dir=str(_ROOT / "logs"))
    agent._risk_manager = rm
    
    original_kelly = rm.KELLY_MAX_FRACTION
    agent._enter_conservative_mode()
    t.check("Conservative: Kelly reduced 50%",
            rm.KELLY_MAX_FRACTION == original_kelly * 0.5)
    
    agent._exit_conservative_mode()
    t.check("Exit conservative: Kelly restored",
            rm.KELLY_MAX_FRACTION == original_kelly)
    
    # Suggestions
    agent._degradation.weekly_returns = [-0.02, -0.03, -0.01]
    agent._degradation.bh_returns = [0.02, 0.01, 0.03]
    suggestions = agent._suggest_parameter_adjustments()
    t.check("3 suggestions generated", len(suggestions) == 3)
    
    # State
    state = agent.get_state()
    t.check("State has expected keys",
            all(k in state for k in ["conservative_mode", "consecutive_losses"]))


# ══════════════════════════════════════════════════════════════════════
#  SECTION 9: Telegram Reporter (offline)
# ══════════════════════════════════════════════════════════════════════

def test_telegram_reporter(t: TestRunner):
    from reporting.weekly_report import NexusTelegramReporter  # type: ignore
    
    t.section("9. TELEGRAM REPORTER (OFFLINE)")
    
    reporter = NexusTelegramReporter(bot_token="", chat_id="")
    t.check("Reporter created (offline)", reporter is not None)
    t.check("Reporter not initialized", reporter._initialized is False)
    
    # track_trade
    reporter.track_trade({"symbol": "BTCUSDT", "side": "BUY", "entry": 40000})
    t.check("track_trade works", len(reporter._weekly_trades) == 1)
    
    # track_equity
    reporter.track_equity(10500.0)
    t.check("track_equity works", len(reporter._weekly_equity) == 1)


# ══════════════════════════════════════════════════════════════════════
#  SECTION 10: PaperTrader (main.py)
# ══════════════════════════════════════════════════════════════════════

def test_paper_trader(t: TestRunner):
    from main import PaperTrader  # type: ignore
    
    t.section("10. PAPER TRADER")
    
    pt = PaperTrader(initial_capital=10000.0)
    t.check("PaperTrader created", pt is not None)
    t.check("Initial capital correct", pt.capital == 10000.0)
    
    # Execute order
    trade = pt.execute_order(
        symbol="BTCUSDT",
        side="BUY",
        price=40000.0,
        quantity=0.1,
        stop_loss=39000.0,
        take_profit=42000.0,
        confidence=0.75,
    )
    t.check("Trade executed", trade["status"] == "OPEN")
    t.check("Trade recorded", len(pt.trade_history) == 1)
    t.check("Commission deducted", pt.capital < 10000.0)
    
    # Update positions with TP hit
    pt.update_positions("BTCUSDT", 42000.0)
    closed = [p for p in pt.positions if p["status"] != "OPEN"]
    t.check("Position closed on TP", len(closed) == 1)
    t.check("PnL positive", closed[0]["pnl"] > 0)
    
    # Summary
    summary = pt.get_summary()
    t.check("Summary has win_rate", "win_rate" in summary)
    t.check("Summary has return_pct", "return_pct" in summary)


# ══════════════════════════════════════════════════════════════════════
#  SECTION 11: Configuration
# ══════════════════════════════════════════════════════════════════════

def test_configuration(t: TestRunner):
    from config.settings import (  # type: ignore
        trading_config, risk_config, ml_config, sentiment_config, backtest_config,
    )
    
    t.section("11. CONFIGURATION")
    
    t.check("trading_config.symbols", len(trading_config.symbols) >= 1)
    t.check("trading_config.timeframes", len(trading_config.timeframes) >= 1)
    t.check("risk_config.max_drawdown", 0 < risk_config.max_drawdown < 1)
    t.check("risk_config.kelly_fraction", 0 < risk_config.kelly_fraction <= 1)
    t.check("ml_config.lstm_lookback", ml_config.lstm_lookback > 0)
    t.check("backtest_config.initial_capital > 0", backtest_config.initial_capital > 0)


# ══════════════════════════════════════════════════════════════════════
#  SECTION 12: Cross-Module Integration
# ══════════════════════════════════════════════════════════════════════

def test_integration(t: TestRunner):
    from core.signal_engine import TechnicalSignalEngine  # type: ignore
    from core.risk_manager import QuantRiskManager  # type: ignore
    from agents.agent_bull import AgentBull  # type: ignore
    from agents.agent_bear import AgentBear  # type: ignore
    from agents.agent_arbitro import AgentArbitro  # type: ignore
    from main import PaperTrader  # type: ignore
    
    t.section("12. CROSS-MODULE INTEGRATION")
    
    df = generate_ohlcv(300)
    
    # Full pipeline: signal → agents → decision → risk → paper trade
    signal_engine = TechnicalSignalEngine()
    risk_manager = QuantRiskManager(log_dir=str(_ROOT / "logs"))
    bull = AgentBull()
    bear = AgentBear()
    arbitro = AgentArbitro()
    arbitro.initialize()
    paper = PaperTrader(10000.0)
    
    # Step 1: Signal
    signal = signal_engine.generate_signal(df)
    price = float(df["close"].iloc[-1])
    t.check("Pipeline: signal generated", signal is not None)
    
    # Step 2: Agents
    bull_arg = bull.build_argument(price, signal)
    bear_arg = bear.build_argument(price, signal)
    t.check("Pipeline: agents argued", bull_arg is not None and bear_arg is not None)
    
    # Step 3: Arbitro Decision
    decision = arbitro.deliberate(
        bull_state=bull.get_state(),
        bear_state=bear.get_state(),
        risk_metrics={"max_drawdown": 0.03},
        market_context={"symbol": "BTCUSDT", "price": price},
    )
    t.check("Pipeline: arbitro decided", decision is not None)
    
    # Step 4: Risk checks
    kelly = risk_manager.kelly_criterion(0.55, 1.5, 1.0)
    cb_ok = not risk_manager.circuit_breaker_check(0.03)
    t.check("Pipeline: risk checks pass", cb_ok)
    
    # Step 5: Paper trade
    if decision.get("decision") != "HOLD":
        side = "BUY" if decision.get("decision") == "BUY" else "SELL"
        atr = float(np.mean(df["high"].values[-14:] - df["low"].values[-14:]))
        sl = price - 2 * atr if side == "BUY" else price + 2 * atr
        tp = price + 3 * atr if side == "BUY" else price - 3 * atr
        qty = (10000 * kelly) / price
        
        trade = paper.execute_order(
            symbol="BTCUSDT", side=side, price=price,
            quantity=qty, stop_loss=sl, take_profit=tp,
            confidence=decision.get("confidence", 0),
        )
        t.check("Pipeline: paper trade executed", trade is not None)
    else:
        t.check("Pipeline: HOLD decision (no trade)", True)
    
    t.check("Full pipeline completed", True)


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
    
    print("\n" + "═" * 60)
    print("  NEXUS TRADING SYSTEM — FULL PROJECT AUDIT")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("═" * 60)
    
    t = TestRunner()
    
    test_imports(t)
    test_signal_engine(t)
    test_risk_manager(t)
    test_agents(t)
    test_execution_engine(t)
    test_ml_engine(t)
    test_sentiment_engine(t)
    test_evolutionary_agent(t)
    test_telegram_reporter(t)
    test_paper_trader(t)
    test_configuration(t)
    test_integration(t)
    
    success = t.report()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
