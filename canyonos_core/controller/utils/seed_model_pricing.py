"""Re-runnable seed for the non-Bedrock rows of aws_pricing_chart.db's model pricing table."""

import sqlite3
import os

_DB_PATH = os.path.join(os.path.dirname(__file__), "aws_pricing_chart.db")

# USD per million tokens (input, output), from OpenAI's published API pricing.
OPENAI_PRICING = {
    "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5-nano": (0.05, 0.4),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4.1-nano": (0.1, 0.4),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
}


def seed(db_path=_DB_PATH):
    conn = sqlite3.connect(db_path)
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO bedrock_model_pricing "
            "(model_id, input_cost_per_million_tokens, output_cost_per_million_tokens) "
            "VALUES (?, ?, ?)",
            [(m, i, o) for m, (i, o) in OPENAI_PRICING.items()],
        )
    conn.close()


if __name__ == "__main__":
    seed()
