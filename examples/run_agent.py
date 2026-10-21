"""Programmatic use: ask a question and inspect the tool trace."""

from __future__ import annotations

import json
import sys

from nl2sql_agent import Settings, create_agent


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    settings = Settings.load(provider="mock")
    agent = create_agent(settings, provider="mock")

    result = agent.run("订单ORD-1012的物料齐套率是多少？")
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))

    print("\nTool trace:")
    for item in result.tool_trace:
        print(f"  [{item['step']}] {item['tool']}: {item['summary']}")


if __name__ == "__main__":
    main()
