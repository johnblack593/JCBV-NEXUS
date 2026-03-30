import os
import sys
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nexus.core.signal_engine import TechnicalSignalEngine

def test_engine():
    engine = TechnicalSignalEngine()
    
    # Create dummy data
    df = pd.DataFrame({
        "open": [100.0] * 100,
        "high": [105.0] * 100,
        "low": [95.0] * 100,
        "close": [102.0] * 100,
        "volume": [1000] * 100,
    })
    
    # Varia el close para generar algo
    for i in range(100):
        df.loc[i, "close"] = 100 + i
        df.loc[i, "high"] = df.loc[i, "close"] + 2
        df.loc[i, "low"] = df.loc[i, "close"] - 2
        
    res = engine.generate_signal(df)
    print("Result Keys:", res.keys())
    print("Signal:", res.get("signal"), type(res.get("signal")))
    print("Is signal string? ", isinstance(res.get("signal"), str))
    
    signal = res.get("signal")
    if hasattr(signal, "name"):
        print("Signal Name:", signal.name)
    elif isinstance(signal, str):
        print("Signal is string:", signal)
        
    print("Confidence:", res.get("confidence"))

if __name__ == "__main__":
    test_engine()
