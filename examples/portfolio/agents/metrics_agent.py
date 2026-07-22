# Metrics Agent
#
# Second stage. For a single ticker, pulls its price history from PriceAgent
# (remote call, resolved via .value()) and computes standard return/risk
# metrics. Pure-Python math (no numpy) to keep the example zero-setup.
#
# The daily-returns series is returned alongside the summary stats so the
# downstream RiskAgent can build the portfolio covariance.
#
# Resource profile: cheap CPU, high fan-out — one compute() call per holding.

import math

from price_agent import PriceAgent

TRADING_DAYS = 252


class MetricsAgent(object):
    def __init__(self):
        self.tools = [self.compute]
        self.price = PriceAgent()

    def compute(self, ticker: str, lookback_days: int = 365) -> dict:
        """Compute return/volatility/Sharpe/drawdown metrics for one ticker."""
        history = self.price.get_history(
            ticker=ticker, lookback_days=lookback_days
        ).value()
        closes = history.get("closes", [])

        if len(closes) < 2:
            return {"ticker": ticker, "error": "insufficient price data"}

        # Simple daily returns.
        returns = [
            (closes[i] / closes[i - 1]) - 1.0 for i in range(1, len(closes))
        ]

        mean_daily = sum(returns) / len(returns)
        var_daily = sum((r - mean_daily) ** 2 for r in returns) / len(returns)
        std_daily = math.sqrt(var_daily)

        ann_return = mean_daily * TRADING_DAYS
        ann_vol = std_daily * math.sqrt(TRADING_DAYS)
        sharpe = (ann_return / ann_vol) if ann_vol > 0 else 0.0

        total_return = (closes[-1] / closes[0]) - 1.0
        max_drawdown = self._max_drawdown(closes)

        return {
            "ticker": ticker,
            "source": history.get("source"),
            "last_price": round(closes[-1], 2),
            "n_days": len(closes),
            "total_return": round(total_return, 4),
            "annualized_return": round(ann_return, 4),
            "annualized_volatility": round(ann_vol, 4),
            "sharpe": round(sharpe, 3),
            "max_drawdown": round(max_drawdown, 4),
            # Raw daily returns for portfolio-level covariance downstream.
            "returns": [round(r, 6) for r in returns],
        }

    def _max_drawdown(self, closes: list) -> float:
        """Largest peak-to-trough decline over the period (negative number)."""
        peak = closes[0]
        worst = 0.0
        for price in closes:
            peak = max(peak, price)
            drawdown = (price / peak) - 1.0
            worst = min(worst, drawdown)
        return worst


if __name__ == "__main__":
    # Assumes the PriceAgent stub (Future-returning) is on the path, as it is
    # inside the deployed pipeline.
    agent = MetricsAgent()
    print(agent.compute("AAPL", 90))
