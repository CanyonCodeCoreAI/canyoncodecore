# NL-to-SQL workflow deployed as a REST API endpoint.
#
# Staged escalation pipeline:
#   1. SchemaRetrievalAgent   - retrieve relevant schema for the question
#   2. SQLGeneratorAgent      - fan out N candidate queries (LLM)
#   3. SQLValidatorAgent      - lint + EXPLAIN cost per candidate (fan-out)
#   4. SandboxExecutorAgent   - run survivors on a small sample, vote on best
#   5. ProductionExecutorAgent- run the winner on the big warehouse, cost-gated
#
# Start agents first:  python -m ventis.controller.global_controller
# Test:
#   curl -X POST http://localhost:8080/main \
#        -H 'Content-Type: application/json' \
#        -d '{"question": "total order amount per customer region"}'
#   curl http://localhost:8080/status/<request_id>

import sys
import os

# These path inserts are needed when running inside a Docker container
# where all files are copied flat into /app/, and for local stub imports.
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "stubs"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "grpc_stubs"))

from deploy import deploy
from schema_agent_stub import SchemaRetrievalAgent
from sql_generator_agent_stub import SQLGeneratorAgent
from sql_validator_agent_stub import SQLValidatorAgent
from sandbox_agent_stub import SandboxExecutorAgent
from production_agent_stub import ProductionExecutorAgent


def main(question: str = "total order amount per customer region", n_candidates: int = 3):
    schema_agent = SchemaRetrievalAgent()
    generator = SQLGeneratorAgent()
    validator = SQLValidatorAgent()
    sandbox = SandboxExecutorAgent()
    production = ProductionExecutorAgent()

    # Stage 1: retrieve schema. Future is chained directly into generation —
    # the framework resolves it before the generator runs.
    schema = schema_agent.get_relevant_schema(question=question)

    # Stage 2: fan out candidate SQL queries (LLM calls happen inside).
    candidates = generator.generate_candidates(
        question=question, schema=schema, n=n_candidates
    ).value()

    # Stage 3: validate each candidate. lint (cheap) gates admission; the cost
    # estimate feeds the production admission gate later. Two futures per
    # candidate are dispatched, then resolved together — this is the fan-out
    # the scheduler sees.
    lint_futures = {sql: validator.lint(sql=sql) for sql in candidates}
    cost_futures = {sql: validator.explain_cost(sql=sql) for sql in candidates}

    survivors = []
    costs = {}
    for sql in candidates:
        lint = lint_futures[sql].value()
        cost = cost_futures[sql].value()
        costs[sql] = cost["estimated_cost"]
        if lint["valid"]:
            survivors.append(sql)

    if not survivors:
        return {"question": question, "error": "no candidate passed static validation"}

    # Stage 4: execute survivors on the sampled replica, then vote.
    sample_results = [sandbox.run_on_sample(sql=sql).value() for sql in survivors]
    selection = sandbox.select_best(results=sample_results).value()
    best_sql = selection.get("selected")

    # Stage 5: run the winner on production behind the cost gate.
    prod = production.run_on_production(
        sql=best_sql, estimated_cost=costs.get(best_sql, 0.0)
    ).value()

    return {
        "question": question,
        "candidates": candidates,
        "costs": costs,
        "survivors": survivors,
        "selection": selection,
        "production": prod,
    }


deploy(main, port=8080)