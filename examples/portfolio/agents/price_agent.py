# Price Agent
#
# First stage of the portfolio-analysis pipeline. Fetches daily closing-price
# history for a single ticker. Uses Yahoo Finance via `yfinance` (no API key).
#
# If the network fetch fails (offline, rate-limited, bad ticker), it falls back
# to a deterministic synthetic price series so the pipeline always runs. This
# keeps the example zero-setup while being real when a connection is available.
#
# Resource profile: network/IO-bound, cheap CPU. Independent per ticker, so the
# scheduler fans these out across replicas.


class PriceAgent(object):
    def __init__(self):
        self.tools = [self.get_history]

    def get_history(self, ticker: str, lookback_days: int = 365) -> dict:
        """Fetch daily closing prices for a ticker over the lookback window."""
        try:
            import yfinance as yf

            hist = yf.Ticker(ticker).history(period=f"{lookback_days}d")
            closes = [float(c) for c in hist["Close"].tolist()]
            dates = [str(d.date()) for d in hist.index]
            if closes:
                return {
                    "ticker": ticker,
                    "dates": dates,
                    "closes": closes,
                    "source": "yfinance",
                }
        except Exception as e:
            print(f"PriceAgent: yfinance fetch failed for {ticker} ({e}); "
                  "using synthetic prices.")

        return self._synthetic(ticker, lookback_days)

    def _synthetic(self, ticker: str, lookback_days: int) -> dict:
        """Deterministic pseudo-random walk seeded by the ticker symbol."""
        import random

        rng = random.Random(sum(ord(c) for c in ticker))
        # Per-ticker drift/vol so different tickers look different but stable.
        daily_drift = (rng.random() - 0.45) * 0.002
        daily_vol = 0.01 + rng.random() * 0.02
        price = 50.0 + rng.random() * 200.0

        closes = []
        for _ in range(lookback_days):
            shock = rng.gauss(daily_drift, daily_vol)
            price = max(1.0, price * (1.0 + shock))
            closes.append(round(price, 2))

        return {
            "ticker": ticker,
            "dates": [],
            "closes": closes,
            "source": "synthetic",
        }


if __name__ == "__main__":
    agent = PriceAgent()
    h = agent.get_history("AAPL", 30)
    print(f"{h['ticker']} [{h['source']}]: {len(h['closes'])} closes, "
          f"last={h['closes'][-1] if h['closes'] else 'n/a'}")
