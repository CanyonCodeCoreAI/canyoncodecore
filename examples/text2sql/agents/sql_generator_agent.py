# SQL Generator Agent
#
# Second stage. Given the question and the retrieved schema, generates N
# candidate SQL queries. State-of-the-art NL-to-SQL pipelines (CHASE-SQL,
# XiYan-SQL) generate several candidates and select among them, so this is a
# genuine fan-out.
#
# Resource profile: LLM-bound. Calls the VllmAgent remotely for each candidate.
# These calls sit on the request's critical path, so the scheduler should
# prioritize them over background work.

from vllm_agent_stub import VllmAgentStub


class SQLGeneratorAgent(object):
    def __init__(self):
        self.tools = [self.generate_candidates]
        self.vllm = VllmAgentStub()

    def generate_candidates(self, question: str, schema: dict, n: int = 3) -> list:
        """Generate N candidate SQL queries for a question given the schema."""
        tables = schema.get("tables", {}) if isinstance(schema, dict) else {}
        schema_str = "; ".join(
            f"{t}({', '.join(cols)})" for t, cols in tables.items()
        )

        candidates = []
        for i in range(n):
            # Each candidate is generated with a slightly different instruction
            # to encourage diversity, then the LLM call is dispatched remotely.
            prompt = (
                f"Given schema: {schema_str}. "
                f"Write SQL (variant {i + 1}) to answer: {question}. "
                f"Return only SQL."
            )
            # Remote LLM call; .value() blocks until the future resolves.
            generated = self.vllm.generate(prompt).value()
            candidates.append(self._to_sql(generated, tables, i))
        return candidates

    def _to_sql(self, generated: str, tables: dict, variant: int) -> str:
        """Coerce the LLM output into a runnable SQL string.

        The stub VllmAgent returns placeholder text, so we synthesize a
        plausible query here. A real generator would parse the model output.
        """
        if "orders" in tables and "customers" in tables:
            variants = [
                "SELECT c.region, SUM(o.amount) AS total "
                "FROM customers c JOIN orders o ON o.customer_id = c.id "
                "GROUP BY c.region ORDER BY total DESC",
                "SELECT c.region, SUM(o.amount) AS total "
                "FROM orders o JOIN customers c ON c.id = o.customer_id "
                "GROUP BY c.region",
                # A deliberately weaker candidate to exercise selection.
                "SELECT region, SUM(amount) AS total FROM customers "
                "GROUP BY region",
            ]
            return variants[variant % len(variants)]
        return "SELECT 1"


if __name__ == "__main__":
    agent = SQLGeneratorAgent()
    schema = {"tables": {"customers": ["id", "region"], "orders": ["customer_id", "amount"]}}
    for sql in agent.generate_candidates("total amount per region", schema, n=3):
        print(sql)