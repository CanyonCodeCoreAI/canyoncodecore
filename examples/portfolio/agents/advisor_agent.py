# Advisor Agent
#
# Final stage. Turns the computed portfolio metrics and risk figures into a
# short, plain-English briefing using a small, cheap model on AWS Bedrock
# (Converse API). Configure with env vars:
#   BEDROCK_MODEL_ID  (default: meta.llama3-8b-instruct-v1:0)
#   AWS_REGION        (default: us-east-1)
#
# If Bedrock is unavailable (no boto3, no creds, model not enabled), it falls
# back to a deterministic templated summary so the pipeline still returns.
#
# Resource profile: LLM-bound, single call per request, on the critical path.

import os


class AdvisorAgent(object):
    def __init__(self):
        self.tools = [self.summarize]
        self.model_id = os.environ.get(
            "BEDROCK_MODEL_ID", "meta.llama3-8b-instruct-v1:0"
        )
        self.region = os.environ.get("AWS_REGION", "us-east-1")

    def summarize(self, holdings: dict, metrics: dict, risk: dict) -> str:
        """Write a short plain-English briefing on the portfolio."""
        prompt = self._build_prompt(holdings, metrics, risk)
        try:
            import boto3

            client = boto3.client("bedrock-runtime", region_name=self.region)
            response = client.converse(
                modelId=self.model_id,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": 400, "temperature": 0.2},
            )
            return response["output"]["message"]["content"][0]["text"]
        except Exception as e:
            print(f"AdvisorAgent: Bedrock call failed ({e}); using templated summary.")
            return self._fallback_summary(metrics, risk)

    def _build_prompt(self, holdings: dict, metrics: dict, risk: dict) -> str:
        lines = ["You are a portfolio analyst. Given the figures below, write a "
                 "concise 3-4 sentence briefing covering return, risk, "
                 "diversification, and concentration. Be specific and neutral.\n"]
        lines.append("Holdings (weights): " + ", ".join(
            f"{t}={w}" for t, w in holdings.items()))
        lines.append("\nPer-ticker metrics:")
        for t, m in metrics.items():
            if "error" in m:
                lines.append(f"  {t}: {m['error']}")
                continue
            lines.append(
                f"  {t}: ann_return={m['annualized_return']:.1%}, "
                f"ann_vol={m['annualized_volatility']:.1%}, "
                f"sharpe={m['sharpe']}, max_drawdown={m['max_drawdown']:.1%}"
            )
        lines.append(
            "\nPortfolio: "
            f"ann_return={risk.get('portfolio_annualized_return', 0):.1%}, "
            f"ann_vol={risk.get('portfolio_annualized_volatility', 0):.1%}, "
            f"sharpe={risk.get('portfolio_sharpe')}, "
            f"HHI={risk.get('concentration_hhi')}, "
            f"diversification_ratio={risk.get('diversification_ratio')}"
        )
        return "\n".join(lines)

    def _fallback_summary(self, metrics: dict, risk: dict) -> str:
        ret = risk.get("portfolio_annualized_return", 0.0)
        vol = risk.get("portfolio_annualized_volatility", 0.0)
        sharpe = risk.get("portfolio_sharpe", 0.0)
        hhi = risk.get("concentration_hhi", 0.0)
        top = risk.get("top_holding", {})
        concentration = (
            "highly concentrated" if hhi > 0.5
            else "moderately concentrated" if hhi > 0.25
            else "well diversified"
        )
        return (
            f"The portfolio has an estimated annualized return of {ret:.1%} against "
            f"{vol:.1%} volatility (Sharpe {sharpe}). It appears {concentration} "
            f"(HHI {hhi}), with {top.get('ticker', 'n/a')} the largest position at "
            f"{top.get('weight', 0):.1%}. Diversification ratio is "
            f"{risk.get('diversification_ratio')}, indicating the extent to which "
            f"combining these holdings reduces standalone risk."
        )


if __name__ == "__main__":
    agent = AdvisorAgent()
    demo_metrics = {
        "AAPL": {"annualized_return": 0.18, "annualized_volatility": 0.25,
                 "sharpe": 0.72, "max_drawdown": -0.15},
        "MSFT": {"annualized_return": 0.14, "annualized_volatility": 0.22,
                 "sharpe": 0.64, "max_drawdown": -0.12},
    }
    demo_risk = {
        "portfolio_annualized_return": 0.164, "portfolio_annualized_volatility": 0.21,
        "portfolio_sharpe": 0.78, "concentration_hhi": 0.52,
        "diversification_ratio": 1.12,
        "top_holding": {"ticker": "AAPL", "weight": 0.6},
    }
    print(agent.summarize({"AAPL": 0.6, "MSFT": 0.4}, demo_metrics, demo_risk))
