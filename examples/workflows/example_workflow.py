# Example workflow deployed as a REST API endpoint.
# The workflow uses generated stubs to call finance and market agents via gRPC.
#
# Start agents first:   python src/controller/global_controller.py
# Then run this file:    python examples/workflow.py
# Test:
#   curl -X POST http://localhost:8080/main -H 'Content-Type: application/json' -d '{"ticker": "AAPL"}'
#   curl http://localhost:8080/status/<request_id>

import sys
import os

# Add src directory so `import ventis` works
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
# Add stubs directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "stubs"))
# Add grpc_stubs for protobuf modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "grpc_stubs"))

from deploy import deploy
from finance_agent_stub import FinanceAgent
from market_agent_stub import MarketResearchAgent


def main(ticker: str = "AAPL"):
    finance = FinanceAgent()
    market = MarketResearchAgent()

    # Call finance agent functions
    price = finance.get_stock_price(ticker=ticker)
    company = finance.get_company_name(ticker=ticker)

    # Call market agent functions
    trend = market.get_market_trend(sector="tech")
    analysis = market.get_sector_analysis(sector="tech")

    # Chain agent outputs: pass the Future from FinanceAgent directly
    # into MarketResearchAgent. The framework will resolve the Future's
    # value before executing get_competitor_list.
    competitors = market.get_competitor_list(company=company)

    print(f"Competitors: {competitors.value()}")

    return {
        "stock_price": price.value(),
        "company_name": company.value(),
        "market_trend": trend.value(),
        "sector_analysis": analysis.value(),
        "competitors": competitors.value(),
    }


deploy(main, port=8080)
