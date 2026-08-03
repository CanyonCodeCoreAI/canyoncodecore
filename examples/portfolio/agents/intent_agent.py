# Intent Agent
#
# Stage 0. Turns a free-text portfolio request into the structured input the
# rest of the pipeline needs:
#
#   "Analyze 40% Apple, 35% Microsoft and 25% Nvidia over the last 6 months"
#      ->  {"holdings": {"AAPL": 0.4, "MSFT": 0.35, "NVDA": 0.25},
#           "lookback_days": 180}
#
# Calls AWS Bedrock (Converse API) via ventis.llm.bedrock -- same pattern as
# AdvisorAgent -- so token/cost telemetry gets recorded onto this execution's
# future:<future_id>:metrics hash. Configure with env vars:
#   BEDROCK_MODEL_ID  (default: meta.llama3-8b-instruct-v1:0)
#   AWS_REGION        (default: us-east-1)
#
# If the LLM is unavailable or returns unparseable output, parse() raises:
# there is no fallback, the request fails loudly rather than guessing at the
# holdings. Weights are renormalized to 1.0.
#
# Resource profile: cheap CPU, single call per request, on the critical path
# before the fan-out.

import os
import re
import json

try:
    from ventis.llm.bedrock import call_bedrock
except ImportError:
    from bedrock import call_bedrock

DEFAULT_LOOKBACK_DAYS = 365


class IntentAgent(object):
    def __init__(self):
        self.tools = [self.parse]
        self.model_id = os.environ.get(
            "BEDROCK_MODEL_ID", "openai.gpt-5.4"
        )
        self.region = os.environ.get("AWS_REGION", "us-east-1")

    def parse(self, query: str) -> dict:
        """Parse a natural-language portfolio request into holdings + lookback."""
        response = call_bedrock(
            model_id=self.model_id,
            messages=[{"role": "user", "content": [{"text": self._build_prompt(query)}]}],
            inference_config={"maxTokens": 300, "temperature": 0.0},
            region=self.region,
        )
        text = response["output"]["message"]["content"][0]["text"]
        if not text:
            raise ValueError("IntentAgent: LLM returned no output for the request.")

        parsed = self._extract_json(text)
        if parsed is None:
            raise ValueError(
                f"IntentAgent: could not parse holdings from LLM output: {text!r}"
            )

        result = self._sanitize(parsed)
        if not result["holdings"]:
            raise ValueError(
                f"IntentAgent: no valid holdings found in request: {query!r}"
            )
        return result

    def _build_prompt(self, query: str) -> str:
        return (
            "You convert a plain-English portfolio request into JSON. Return ONLY "
            "a JSON object, no prose, with exactly two keys:\n"
            '  "holdings": an object mapping stock TICKER symbols (uppercase) to '
            "their weight as a decimal fraction (weights should sum to about 1.0), and\n"
            '  "lookback_days": an integer number of calendar days for the analysis '
            f"window (default {DEFAULT_LOOKBACK_DAYS} if unspecified; 1 month = 30 "
            "days, 1 year = 365 days).\n"
            "Resolve company names to their ticker (Apple->AAPL, Microsoft->MSFT, "
            "Nvidia->NVDA, etc.). If weights are given as percentages, convert to "
            "fractions. If a holding has no explicit weight, split the remainder "
            "equally among the unweighted holdings.\n\n"
            f'Request: "{query}"\n\n'
            "JSON:"
        )

    def _extract_json(self, text: str):
        """Pull the first JSON object out of the model's response text."""
        # Models sometimes wrap the JSON in prose or code fences; grab the
        # outermost {...} span.
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except (ValueError, TypeError):
            return None

    def _sanitize(self, parsed: dict) -> dict:
        """Validate types and renormalize weights to sum to 1.0."""
        raw = (parsed or {}).get("holdings") or {}
        holdings = {}
        for ticker, weight in raw.items():
            try:
                w = float(weight)
            except (ValueError, TypeError):
                continue
            if w > 0:
                holdings[str(ticker).upper()] = w

        total = sum(holdings.values())
        if total > 0:
            holdings = {t: round(w / total, 4) for t, w in holdings.items()}

        try:
            lookback = int((parsed or {}).get("lookback_days", DEFAULT_LOOKBACK_DAYS))
        except (ValueError, TypeError):
            lookback = DEFAULT_LOOKBACK_DAYS
        if lookback <= 0:
            lookback = DEFAULT_LOOKBACK_DAYS

        return {"holdings": holdings, "lookback_days": lookback}


if __name__ == "__main__":
    agent = IntentAgent()
    print(agent.parse(
        query="Analyze 40% Apple, 35% Microsoft and 25% Nvidia over the last 6 months"
    ))
