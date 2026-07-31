# VLLM Agent
#
# LLM backend for SQL candidate generation, called remotely by
# SQLGeneratorAgent. Calls AWS Bedrock (Converse API) via ventis.llm.bedrock
# so token/cost telemetry gets recorded onto this execution's
# future:<future_id>:metrics hash — same pattern as
# examples/portfolio/agents/advisor_agent.py.
# Configure with env vars:
#   BEDROCK_MODEL_ID  (default: meta.llama3-8b-instruct-v1:0)
#   AWS_REGION        (default: us-east-1)
#
# Falls back to a synthetic placeholder response if Bedrock is unavailable.

import os

try:
    from ventis.llm.bedrock import call_bedrock
except ImportError:
    from bedrock import call_bedrock


class VllmAgent(object):
    def __init__(self):
        self.tools = [self.generate]
        self.model_id = os.environ.get("BEDROCK_MODEL_ID", "meta.llama3-8b-instruct-v1:0")
        self.region = os.environ.get("AWS_REGION", "us-east-1")

    def generate(self, prompt: str) -> str:
        """Generates a response using an LLM model based on the given prompt."""
        try:
            response = call_bedrock(
                model_id=self.model_id,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inference_config={"maxTokens": 400, "temperature": 0.2},
                region=self.region,
            )
            return response["output"]["message"]["content"][0]["text"]
        except Exception as e:
            print(f"VllmAgent: Bedrock call failed ({e}); using synthetic response.")
            return self._fallback_response(prompt)

    def _fallback_response(self, prompt: str) -> str:
        return f"This is an LLM generated response to: '{prompt}'"


if __name__ == "__main__":
    agent = VllmAgent()
    print(agent.generate("What is the stock price?"))
