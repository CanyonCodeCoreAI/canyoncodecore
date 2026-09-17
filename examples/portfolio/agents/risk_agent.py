# Risk Agent
#
# Aggregation stage. Consumes the per-ticker metrics for the whole portfolio
# and rolls them up into portfolio-level risk figures. This is the barrier in
# the pipeline: it needs every ticker's metrics before it can run.
#
# Computes:
#   - portfolio annualized return   (weight-weighted)
#   - portfolio annualized vol       (from the daily-return covariance matrix)
#   - concentration (HHI)            (sum of squared weights)
#   - diversification ratio          (weighted-avg vol / portfolio vol)
#
# Pure-Python math, no numpy. Return series are aligned to their shortest
# common length before building the covariance (a pragmatic simplification —
# a production version would align by trading date).
#
# Resource profile: cheap CPU, single call per request.

import math

TRADING_DAYS = 252


class RiskAgent(object):
    def __init__(self):
        self.tools = [self.assess]

    def assess(self, holdings: dict, metrics: dict) -> dict:
        """Roll per-ticker metrics up into portfolio-level risk figures."""
        # Keep only tickers that produced valid metrics.
        valid = {t: m for t, m in metrics.items() if "error" not in m}
        if not valid:
            return {"error": "no valid ticker metrics"}

        weights = self._normalize_weights(holdings, valid.keys())

        port_return = sum(
            weights[t] * valid[t]["annualized_return"] for t in valid
        )

        port_vol = self._portfolio_volatility(weights, valid)
        weighted_avg_vol = sum(
            weights[t] * valid[t]["annualized_volatility"] for t in valid
        )
        diversification = (
            round(weighted_avg_vol / port_vol, 3) if port_vol > 0 else None
        )

        hhi = sum(w ** 2 for w in weights.values())
        top_ticker = max(weights, key=weights.get)

        return {
            "n_holdings": len(valid),
            "weights": {t: round(w, 4) for t, w in weights.items()},
            "portfolio_annualized_return": round(port_return, 4),
            "portfolio_annualized_volatility": round(port_vol, 4),
            "portfolio_sharpe": round(port_return / port_vol, 3) if port_vol > 0 else 0.0,
            "concentration_hhi": round(hhi, 4),
            "diversification_ratio": diversification,
            "top_holding": {"ticker": top_ticker, "weight": round(weights[top_ticker], 4)},
        }

    def _normalize_weights(self, holdings: dict, tickers) -> dict:
        """Restrict weights to valid tickers and renormalize to sum to 1."""
        raw = {t: float(holdings.get(t, 0.0)) for t in tickers}
        total = sum(raw.values())
        if total <= 0:
            # Fall back to equal weighting.
            n = len(raw)
            return {t: 1.0 / n for t in raw}
        return {t: w / total for t, w in raw.items()}

    def _portfolio_volatility(self, weights: dict, metrics: dict) -> float:
        """Annualized portfolio volatility from the daily-return covariance."""
        tickers = list(metrics.keys())
        series = {t: metrics[t].get("returns", []) for t in tickers}

        # Align all series to the shortest common length (take the tail).
        min_len = min((len(s) for s in series.values()), default=0)
        if min_len < 2:
            # Not enough overlap: fall back to weighted-average vol.
            return sum(
                weights[t] * metrics[t]["annualized_volatility"] for t in tickers
            )
        aligned = {t: series[t][-min_len:] for t in tickers}
        means = {t: sum(aligned[t]) / min_len for t in tickers}

        # Daily covariance matrix, then w' Σ w.
        daily_var = 0.0
        for i in tickers:
            for j in tickers:
                cov = sum(
                    (aligned[i][k] - means[i]) * (aligned[j][k] - means[j])
                    for k in range(min_len)
                ) / min_len
                daily_var += weights[i] * weights[j] * cov

        daily_var = max(daily_var, 0.0)
        return math.sqrt(daily_var * TRADING_DAYS)


if __name__ == "__main__":
    agent = RiskAgent()
    demo_metrics = {
        "AAPL": {"annualized_return": 0.18, "annualized_volatility": 0.25,
                 "returns": [0.01, -0.02, 0.015, 0.0, -0.01]},
        "MSFT": {"annualized_return": 0.14, "annualized_volatility": 0.22,
                 "returns": [0.005, -0.01, 0.02, -0.005, 0.01]},
    }
    print(agent.assess({"AAPL": 0.6, "MSFT": 0.4}, demo_metrics))
