# Simple LLM Agent Example (AWS Bedrock backend)
#
# Calls a small, cheap model on Bedrock via the Converse API. Configure with
# env vars:
#   BEDROCK_MODEL_ID  (default: meta.llama3-8b-instruct-v1:0)
#   AWS_REGION        (default: us-east-1)
# AWS credentials are resolved by boto3 (env vars, shared config, or IAM role).

import os

import boto3


class VllmAgent(object):
    def __init__(self):
        self.tools = [self.generate]
        # Cheap default; swap for e.g. "qwen.qwen2-5-7b-instruct-v1:0" or
        # "meta.llama3-1-8b-instruct-v1:0" if enabled in your account/region.
        self.model_id = os.environ.get(
            "BEDROCK_MODEL_ID", "meta.llama3-8b-instruct-v1:0"
        )
        region = os.environ.get("AWS_REGION", "us-east-1")
        self.client = boto3.client("bedrock-runtime", region_name=region)

    def generate(self, prompt: str) -> str:
        """Generates a response using a Bedrock model based on the given prompt."""
        print(f"VllmAgent: Received prompt: '{prompt}'")

        response = self.client.converse(
            modelId=self.model_id,
            messages=[
                {"role": "user", "content": [{"text": prompt}]},
            ],
            inferenceConfig={"maxTokens": 512, "temperature": 0.0},
        )

        return response["output"]["message"]["content"][0]["text"]


if __name__ == "__main__":
    agent = VllmAgent()
    print(agent.generate("What is the stock price?"))
