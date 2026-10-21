"""Human-in-the-loop experience: save a candidate, verify it, then reuse it."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from nl2sql_agent import Settings
from nl2sql_agent.runtime import create_runtime
from nl2sql_agent.tools.sql_tools import run_readonly_query


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    question = "待发货订单有多少个？"
    sql = "SELECT COUNT(*) AS pending_orders FROM sales_order WHERE status = 'pending'"

    # Use a throwaway experience DB so the demo always starts from an empty memory.
    with tempfile.TemporaryDirectory() as directory:
        settings = Settings.load(provider="mock")
        settings.experience_path = Path(directory) / "experience.db"
        runtime = create_runtime(settings, provider="mock")
        store = runtime.store
        schema_hash = runtime.catalog.schema_hash()

        def validator(text: str) -> tuple[bool, str]:
            check = runtime.guard.check(text)
            if not check.valid:
                return False, "; ".join(issue.message for issue in check.errors)
            execution = run_readonly_query(runtime.settings.db_path, check.sql)
            return bool(execution.get("success")), str(execution.get("error", "ok"))

        record = store.add(
            question,
            sql,
            category="metric",
            source="human",
            schema_hash=schema_hash,
        )
        print("candidate:", record["id"], record["status"])
        print("search before verify:", store.search(question, schema_hash=schema_hash))

        store.verify(int(record["id"]), validator=validator)
        print("search after verify:", store.search(question, schema_hash=schema_hash))
        print("stats:", store.stats())


if __name__ == "__main__":
    main()
