# Skill: ML Time Series Engineering (ml_timeseries.md)

This skill focuses on robust machine learning for time-series data, specifically for algorithmic trading.

## Capabilities
- **Lookahead Bias Detection**: Identifying and fixing data leakage where future info enters the past (e.g., global scalers).
- **Windowed Scaling**: Local normalization within lookback windows.
- **Walk-Forward Optimization (WFO)**: Validating models using rolling time windows.
- **Feature Engineering**: Vectorized TA indicators and volatility-adjusted returns.
- **LSTM & DQN Architecture**: Specialized neural networks for sequential data and reinforcement learning.

## Usage Guidelines
- Prefer windowed scaling over global scaling for any real-time inference model.
- Always validate that features used at time `t` were available before time `t`.
- Monitor for regime shifts (non-stationarity) in time-series data.
