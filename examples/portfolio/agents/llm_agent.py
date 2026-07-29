# LLM Agent
#
# Shared inference node. Owns all the AWS Bedrock (Converse API) plumbing so no
# other agent has to carry boto3 boilerplate — they just call complete(prompt)
# and get text back. Configure with env vars:
#   BEDROCK_MODEL_ID  (default: meta.llama3-8b-instruct-v1:0)
#   AWS_REGION        (default: us-east-1)
#
# On any failure (no boto3, no creds, model not enabled) it returns an empty
# string; callers decide how to degrade (templated summary, regex parse, etc.).
#
# Resource profile: LLM-bound. This is the only node that talks to Bedrock, so
# it's the natural place to scale inference capacity independently.

import os


class LLMAgent(object):
    def __init__(self):
        self.tools = [self.complete]
        self.model_id = os.environ.get(
            "BEDROCK_MODEL_ID", "meta.llama3-8b-instruct-v1:0"
        )
        self.region = os.environ.get("AWS_REGION", "us-east-1")

    def complete(
        self, prompt: str, max_tokens: int = 400, temperature: float = 0.2
    ) -> str:
        """Run a single-turn completion on Bedrock; '' on any failure."""
        try:
            import boto3

            client = boto3.client("bedrock-runtime", region_name=self.region)
            response = client.converse(
                modelId=self.model_id,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={
                    "maxTokens": max_tokens,
                    "temperature": temperature,
                },
            )
            return response["output"]["message"]["content"][0]["text"]
        except Exception as e:
            print(f"LLMAgent: Bedrock call failed ({e}).")
            return ""


if __name__ == "__main__":
    agent = LLMAgent()
    print(agent.complete("Say hello in one short sentence.", max_tokens=50))