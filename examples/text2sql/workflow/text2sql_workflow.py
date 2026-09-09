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

import json
import sys
import os

# These path inserts are needed when running inside a Docker container
# where all files are copied flat into /app/, and for local stub imports.
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "stubs"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "grpc_stubs"))

from deploy import deploy
from agents.schema_agent import SchemaRetrievalAgent
from agents.sql_generator_agent import SQLGeneratorAgent
from agents.sql_validator_agent import SQLValidatorAgent
from agents.sandbox_agent import SandboxExecutorAgent
from agents.production_agent import ProductionExecutorAgent


def main(question: str = "total order amount per customer region", n_candidates: int = 3):
    schema_agent = SchemaRetrievalAgent()
    generator = SQLGeneratorAgent()
    validator = SQLValidatorAgent()
    sandbox = SandboxExecutorAgent()
    production = ProductionExecutorAgent()

    # Stage 1: retrieve schema. Resolved here (not chained unresolved into the
    # next call) since a Future's result only ever lives on the Redis of
    # whichever node created it -- here, this workflow's own -- so resolving
    # it where it was created is always safe, regardless of which node ends
    # up running the next stage.
    schema = json.loads(schema_agent.get_relevant_schema(question=question).value())

    # Stage 2: fan out candidate SQL queries (LLM calls happen inside).
    candidates = json.loads(
        generator.generate_candidates(
            question=question, schema=schema, n=n_candidates
        ).value()
    )

    # Stage 3: validate each candidate. lint (cheap) gates admission; the cost
    # estimate feeds the production admission gate later. Two futures per
    # candidate are dispatched, then resolved together — this is the fan-out
    # the scheduler sees.
    lint_futures = {sql: validator.lint(sql=sql) for sql in candidates}
    cost_futures = {sql: validator.explain_cost(sql=sql) for sql in candidates}

    survivors = []
    costs = {}
    for sql in candidates:
        lint = json.loads(lint_futures[sql].value())
        cost = json.loads(cost_futures[sql].value())
        costs[sql] = cost["estimated_cost"]
        if lint["valid"]:
            survivors.append(sql)

    if not survivors:
        return {"question": question, "error": "no candidate passed static validation"}

    # Stage 4: execute survivors on the sampled replica, then vote.
    sample_results = [
        json.loads(sandbox.run_on_sample(sql=sql).value()) for sql in survivors
    ]
    selection = json.loads(sandbox.select_best(results=sample_results).value())
    best_sql = selection.get("selected")

    # Stage 5: run the winner on production behind the cost gate.
    prod = json.loads(
        production.run_on_production(
            sql=best_sql, estimated_cost=costs.get(best_sql, 0.0)
        ).value()
    )

    return {
        "question": question,
        "candidates": candidates,
        "costs": costs,
        "survivors": survivors,
        "selection": selection,
        "production": prod,
    }


deploy(main, port=8080)