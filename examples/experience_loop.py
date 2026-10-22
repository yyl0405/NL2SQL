"""Human-in-the-loop experience: save a candidate, verify it, then reuse it."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from nl2sql_agent import Settings
from nl2sql_agent.agent import NL2SQLAgent
from nl2sql_agent.llm import LLMResponse
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

        # Failure loop: terminal failures queue once, and a human resolve
        # becomes a verified experience again.
        failing = "每个物料的库存金额是多少"
        first = store.record_failure(failing, reason="needs_human", schema_hash=schema_hash)
        second = store.record_failure(failing, reason="needs_human", schema_hash=schema_hash)
        print("failure queued:", first["id"], first["status"], "created:", first["created"])
        print("duplicate:", second["duplicate"], "occur_count:", second["occur_count"])

        try:
            store.resolve_failure(int(first["id"]), "SELECT * FROM inventory", validator=validator)
        except ValueError as error:
            print("resolve blocked:", error)
            print("still open:", store.get_failure(int(first["id"]))["status"])

        resolved = store.resolve_failure(
            int(first["id"]),
            "SELECT material_id, on_hand_qty FROM inventory LIMIT 3",
            validator=validator,
            note="库存单价字段未接入，先给出数量口径",
        )
        print("resolved:", resolved["failure"]["status"], "-> experience", resolved["experience"]["status"])

        inventory_sql = resolved["experience"]["sql"]

        # Verified experience is injected into the system prompt automatically.
        class CapturingLLM:
            def __init__(self) -> None:
                self.messages: list[dict] = []

            def chat(self, messages, tools=None):
                self.messages = list(messages)
                return LLMResponse(
                    content='{"status": "needs_clarification", "answer": "示例"}',
                    finish_reason="stop",
                )

        llm = CapturingLLM()
        agent = NL2SQLAgent(
            runtime.catalog,
            llm,
            runtime.agent.tools,
            runtime.settings,
            store=store,
            schema_hash=schema_hash,
        )
        agent.run("物料库存金额统计一下")
        print("verified SQL injected:", inventory_sql in llm.messages[0]["content"])
        print("stats:", store.stats())


if __name__ == "__main__":
    main()
