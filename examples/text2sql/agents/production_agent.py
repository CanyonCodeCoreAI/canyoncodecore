# Production Executor Agent
#
# Final stage. Runs the single winning query against the big production
# warehouse, but only through a governed admission gate: queries whose
# estimated cost exceeds the budget are rejected rather than allowed to burn
# warehouse resources.
#
# Resource profile: scarce, expensive production slots. This is the resource
# the whole staged pipeline exists to protect. A statement timeout stands in
# for real preemption (killing a runaway query mid-flight).


class ProductionExecutorAgent(object):
    # Reject anything the planner estimates above this cost.
    COST_BUDGET = 5000.0

    def __init__(self):
        self.tools = [self.run_on_production]
        # Real impl holds a governed connection pool / workload-management
        # queue against Snowflake/BigQuery/Postgres, e.g.:
        #   self.pool = psycopg2.pool.ThreadedConnectionPool(...)

    def run_on_production(self, sql: str, estimated_cost: float = 0.0) -> dict:
        """Run the winning query on the big warehouse behind a cost gate."""
        if sql is None:
            return {"ok": False, "error": "no query selected by sandbox stage"}

        # Admission control: reject queries too expensive for the budget.
        if estimated_cost > self.COST_BUDGET:
            return {
                "ok": False,
                "admitted": False,
                "error": f"estimated cost {estimated_cost} exceeds budget {self.COST_BUDGET}",
                "sql": sql,
            }

        # Simulated governed execution. Real impl acquires a warehouse slot,
        # sets a statement_timeout (preemption), executes, and releases.
        result_preview = f"<result rows for: {sql[:60]}...>"
        return {
            "ok": True,
            "admitted": True,
            "sql": sql,
            "estimated_cost": estimated_cost,
            "result": result_preview,
        }


if __name__ == "__main__":
    agent = ProductionExecutorAgent()
    print(agent.run_on_production(
        "SELECT c.region, SUM(o.amount) FROM customers c "
        "JOIN orders o ON o.customer_id = c.id GROUP BY c.region",
        estimated_cost=250.0,
    ))
    print(agent.run_on_production("SELECT * FROM huge_fact_table", estimated_cost=99999.0))