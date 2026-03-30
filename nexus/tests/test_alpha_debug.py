import pandas as pd
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from nexus.core.signal_engine import NexusAlphaOscillatorCalculator

df = pd.read_csv('nexus/data/vault/EURUSDX/1m.csv', index_col=0, parse_dates=True)
print(f"Total bars: {len(df)}")

alpha = NexusAlphaOscillatorCalculator(bb_period=20, bb_dev=2.0, rsi_period=7)

# Test a few windows
signals_found = 0
for i in range(100, min(500, len(df))):
    sub = df.iloc[i-100:i+1]
    result = alpha.evaluate(sub)
    if result.direction.name != "NEUTRAL":
        signals_found += 1
        print(f"Bar {i}: {result.direction.name} score={result.value} | {result.detail}")
        if signals_found >= 10:
            break

if signals_found == 0:
    # Debug: check raw conditions
    sub = df.iloc[300:401]
    from ta.volatility import BollingerBands
    from ta.momentum import RSIIndicator
    close = sub["close"]
    bb = BollingerBands(close=close, window=20, window_dev=2.0)
    rsi = RSIIndicator(close=close, window=7).rsi()
    
    for j in range(-5, 0):
        c = float(close.iloc[j])
        o = float(sub["open"].iloc[j])
        lo = float(sub["low"].iloc[j])
        hi = float(sub["high"].iloc[j])
        bl = float(bb.bollinger_lband().iloc[j])
        bu = float(bb.bollinger_hband().iloc[j])
        r = float(rsi.iloc[j])
        print(f"  i={j}: O={o:.5f} H={hi:.5f} L={lo:.5f} C={c:.5f} | BB[{bl:.5f}, {bu:.5f}] RSI={r:.1f} | Lo<=BL: {lo<=bl} Hi>=BU: {hi>=bu} Green: {c>o}")

print(f"\nTotal signals found in first 400 bars: {signals_found}")
