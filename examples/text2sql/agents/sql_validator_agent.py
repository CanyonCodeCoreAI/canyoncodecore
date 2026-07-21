# SQL Validator Agent
#
# Third stage. Static validation of each candidate WITHOUT executing it:
# lint/parse checks plus an EXPLAIN-style cost estimate. The cost estimate is
# the key scheduling signal — cheap queries pass straight through, expensive
# ones can be queued, batched to off-peak, or bounced back for rewriting.
#
# Resource profile: cheap CPU. High fan-out (one call per candidate), but each
# call is light, so this stage is where admission-control decisions are made.


class SQLValidatorAgent(object):
    # Statements that must never reach a read-only analytics path.
    _FORBIDDEN = ("drop", "delete", "update", "insert", "truncate", "alter")

    def __init__(self):
        self.tools = [self.lint, self.explain_cost]

    def lint(self, sql: str) -> dict:
        """Static lint / parse check. Returns validity and any errors."""
        errors = []
        lowered = sql.lower().strip()

        if not lowered.startswith("select") and not lowered.startswith("with"):
            errors.append("not a read-only SELECT/WITH statement")
        for kw in self._FORBIDDEN:
            if f" {kw} " in f" {lowered} ":
                errors.append(f"forbidden statement: {kw}")
        if sql.count("(") != sql.count(")"):
            errors.append("unbalanced parentheses")

        return {"sql": sql, "valid": len(errors) == 0, "errors": errors}

    def explain_cost(self, sql: str) -> dict:
        """Estimate query cost via EXPLAIN without executing it.

        Real impl runs `EXPLAIN (FORMAT JSON) <sql>` against the target DB and
        reads the planner's total cost. Here we heuristically approximate it so
        the scheduler has a numeric cost to gate on.
        """
        lowered = sql.lower()
        cost = 10.0
        cost *= 5 ** lowered.count(" join ")        # joins blow up cost
        cost *= 3 if "group by" in lowered else 1
        cost *= 4 if "order by" in lowered else 1
        cost *= 8 if "distinct" in lowered else 1
        # A cross join / missing predicate is the classic runaway query.
        if " join " in lowered and " on " not in lowered:
            cost *= 100

        tier = "cheap" if cost < 100 else "moderate" if cost < 1000 else "expensive"
        return {"sql": sql, "estimated_cost": round(cost, 1), "tier": tier}


if __name__ == "__main__":
    agent = SQLValidatorAgent()
    q = ("SELECT c.region, SUM(o.amount) AS total FROM customers c "
         "JOIN orders o ON o.customer_id = c.id GROUP BY c.region")
    print(agent.lint(q))
    print(agent.explain_cost(q))