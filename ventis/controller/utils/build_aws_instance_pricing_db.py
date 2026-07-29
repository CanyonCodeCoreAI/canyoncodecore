"""Manual-only build script for the static AWS EC2 instance pricing SQLite DB.

Run by hand to (re)generate ventis/controller/utils/aws_instance_pricing.db.
Not imported or invoked anywhere else in the codebase.
"""

import os
import sqlite3

# Static on-demand hourly Linux pricing (USD, us-east-1), by instance type.
# Verified against instances.vantage.sh (ec2instances.info) per-instance pages, 2026-07-28.
HOURLY_PRICING = {
    # t3 - general purpose burstable
    "t3.nano": 0.0052,
    "t3.micro": 0.0104,
    "t3.small": 0.0208,
    "t3.medium": 0.0416,
    "t3.large": 0.0832,
    "t3.xlarge": 0.1664,
    "t3.2xlarge": 0.3328,
    # t4g - general purpose burstable (Graviton)
    "t4g.nano": 0.0042,
    "t4g.micro": 0.0084,
    "t4g.small": 0.0168,
    "t4g.medium": 0.0336,
    "t4g.large": 0.0672,
    "t4g.xlarge": 0.1344,
    "t4g.2xlarge": 0.2688,
    # m5 - general purpose
    "m5.large": 0.096,
    "m5.xlarge": 0.192,
    "m5.2xlarge": 0.384,
    "m5.4xlarge": 0.768,
    "m5.8xlarge": 1.536,
    "m5.12xlarge": 2.304,
    "m5.16xlarge": 3.072,
    "m5.24xlarge": 4.608,
    # m6i - general purpose
    "m6i.large": 0.096,
    "m6i.xlarge": 0.192,
    "m6i.2xlarge": 0.384,
    "m6i.4xlarge": 0.768,
    "m6i.8xlarge": 1.536,
    "m6i.12xlarge": 2.304,
    "m6i.16xlarge": 3.072,
    "m6i.24xlarge": 4.608,
    # m7i - general purpose
    "m7i.large": 0.1008,
    "m7i.xlarge": 0.2016,
    "m7i.2xlarge": 0.4032,
    "m7i.4xlarge": 0.8064,
    "m7i.8xlarge": 1.6128,
    "m7i.12xlarge": 2.4192,
    "m7i.16xlarge": 3.2256,
    "m7i.24xlarge": 4.8384,
    # c5 - compute optimized
    "c5.large": 0.085,
    "c5.xlarge": 0.17,
    "c5.2xlarge": 0.34,
    "c5.4xlarge": 0.68,
    "c5.9xlarge": 1.53,
    "c5.12xlarge": 2.04,
    "c5.18xlarge": 3.06,
    "c5.24xlarge": 4.08,
    # c6i - compute optimized
    "c6i.large": 0.085,
    "c6i.xlarge": 0.17,
    "c6i.2xlarge": 0.34,
    "c6i.4xlarge": 0.68,
    "c6i.8xlarge": 1.36,
    "c6i.12xlarge": 2.04,
    "c6i.16xlarge": 2.72,
    "c6i.24xlarge": 4.08,
    # c7g - compute optimized (Graviton)
    "c7g.medium": 0.0363,
    "c7g.large": 0.0725,
    "c7g.xlarge": 0.145,
    "c7g.2xlarge": 0.29,
    "c7g.4xlarge": 0.58,
    "c7g.8xlarge": 1.16,
    "c7g.12xlarge": 1.74,
    "c7g.16xlarge": 2.32,
    # r5 - memory optimized
    "r5.large": 0.126,
    "r5.xlarge": 0.252,
    "r5.2xlarge": 0.504,
    "r5.4xlarge": 1.008,
    "r5.8xlarge": 2.016,
    "r5.12xlarge": 3.024,
    "r5.16xlarge": 4.032,
    "r5.24xlarge": 6.048,
    # r6i - memory optimized
    "r6i.large": 0.126,
    "r6i.xlarge": 0.252,
    "r6i.2xlarge": 0.504,
    "r6i.4xlarge": 1.008,
    "r6i.8xlarge": 2.016,
    "r6i.12xlarge": 3.024,
    "r6i.16xlarge": 4.032,
    "r6i.24xlarge": 6.048,
    # x2gd - memory optimized (Graviton)
    "x2gd.medium": 0.0835,
    "x2gd.large": 0.167,
    "x2gd.xlarge": 0.334,
    "x2gd.2xlarge": 0.668,
    "x2gd.4xlarge": 1.336,
    "x2gd.8xlarge": 2.672,
    "x2gd.12xlarge": 4.008,
    "x2gd.16xlarge": 5.344,
    # i3 - storage optimized
    "i3.large": 0.156,
    "i3.xlarge": 0.312,
    "i3.2xlarge": 0.624,
    "i3.4xlarge": 1.248,
    "i3.8xlarge": 2.496,
    "i3.16xlarge": 4.992,
    # d3 - storage optimized
    "d3.xlarge": 0.499,
    "d3.2xlarge": 0.999,
    "d3.4xlarge": 1.998,
    "d3.8xlarge": 3.9955,
    # p3 - accelerated computing (GPU)
    "p3.2xlarge": 3.06,
    "p3.8xlarge": 12.24,
    "p3.16xlarge": 24.48,
    # g4dn - accelerated computing (GPU)
    "g4dn.xlarge": 0.526,
    "g4dn.2xlarge": 0.752,
    "g4dn.4xlarge": 1.204,
    "g4dn.8xlarge": 2.176,
    "g4dn.12xlarge": 3.912,
    "g4dn.16xlarge": 4.352,
}

# Static on-demand Bedrock model pricing (USD per 1,000,000 tokens), us-east-1.
# input/output cost per model_id. Verified 2026-07-28 against
# platform.claude.com/docs/en/about-claude/pricing (Anthropic models only --
# Bedrock pricing matches the Claude API dollar-for-dollar there) and
# cross-referenced against several 2026 pricing aggregators for the rest
# (aws.amazon.com/bedrock/pricing/ had no scrapeable table): cloudzero.com,
# bacancytechnology.com, pecollective.com, wring.co. Non-Anthropic figures are
# lower-confidence than the EC2 table above -- re-verify before relying on them.
BEDROCK_MODEL_PRICING = {
    # Anthropic Claude -- high confidence
    "anthropic.claude-opus-4-5-v1:0": (5.00, 25.00),
    "anthropic.claude-sonnet-4-5-v1:0": (3.00, 15.00),
    "anthropic.claude-haiku-4-5-v1:0": (1.00, 5.00),
    "anthropic.claude-3-5-haiku-20241022-v1:0": (0.80, 4.00),
    "anthropic.claude-opus-4-1-20250805-v1:0": (15.00, 75.00),
    # Amazon Nova -- moderate confidence
    "amazon.nova-micro-v1:0": (0.035, 0.14),
    "amazon.nova-lite-v1:0": (0.06, 0.24),
    "amazon.nova-pro-v1:0": (0.80, 3.20),
    # Meta Llama -- moderate confidence
    "meta.llama3-3-70b-instruct-v1:0": (0.72, 0.72),
    "meta.llama3-1-8b-instruct-v1:0": (0.22, 0.22),
    "meta.llama3-1-405b-instruct-v1:0": (5.32, 16.00),
    "meta.llama3-2-1b-instruct-v1:0": (0.10, 0.10),
    "meta.llama3-2-3b-instruct-v1:0": (0.15, 0.15),
    "meta.llama3-2-11b-instruct-v1:0": (0.35, 0.35),
    "meta.llama3-2-90b-instruct-v1:0": (2.00, 2.00),
    # Mistral -- moderate confidence
    "mistral.mistral-large-2402-v1:0": (3.00, 9.00),
    "mistral.mistral-small-2402-v1:0": (0.20, 0.60),
    # Cohere -- moderate confidence
    "cohere.command-text-v14": (1.00, 2.00),
    "cohere.command-r-plus-v1:0": (3.00, 15.00),
}

DB_PATH = os.path.join(os.path.dirname(__file__), "aws_instance_pricing.db")


def build():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "CREATE TABLE aws_instance_pricing ("
            "instance_type TEXT PRIMARY KEY, "
            "hourly_cost REAL)"
        )
        conn.executemany(
            "INSERT INTO aws_instance_pricing (instance_type, hourly_cost) VALUES (?, ?)",
            list(HOURLY_PRICING.items()),
        )
        conn.execute(
            "CREATE TABLE bedrock_model_pricing ("
            "model_id TEXT PRIMARY KEY, "
            "input_cost_per_million_tokens REAL, "
            "output_cost_per_million_tokens REAL)"
        )
        conn.executemany(
            "INSERT INTO bedrock_model_pricing "
            "(model_id, input_cost_per_million_tokens, output_cost_per_million_tokens) "
            "VALUES (?, ?, ?)",
            [(model_id, inp, out) for model_id, (inp, out) in BEDROCK_MODEL_PRICING.items()],
        )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    build()
    print(
        f"Wrote {len(HOURLY_PRICING)} EC2 rows and "
        f"{len(BEDROCK_MODEL_PRICING)} Bedrock model rows to {DB_PATH}"
    )
