# Skill: Quantitative Risk Management (quant_risk.md)

This skill provides advanced quantitative risk management techniques for high-frequency and institutional trading.

## Capabilities
- **Kelly Criterion**: Optimal position sizing based on probabilistic edge.
- **Monte Carlo Simulation**: Path-dependent capital projection and risk of ruin analysis.
- **Historical VaR (Value at Risk)**: Percentile-based risk measurement capturing fat tails (kurtosis).
- **Circuit Breakers**: Drawdown-based session termination and cool-down protocols.
- **Correlation Penalty**: Dynamic sizing reduction based on portfolio correlation matrix.
- **ATR Sizing**: Volatility-adjusted units per trade.

## Usage Guidelines
- Always prioritize capital preservation over return maximization.
- Use historical VaR instead of parametric VaR for non-normal asset distributions (Crypto/Binary).
- Enforce strict circuit breakers with persistence to avoid "revenge trading" after crashes.
