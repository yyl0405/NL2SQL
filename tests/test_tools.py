from __future__ import annotations

from nl2sql_agent.runtime import Runtime


def _call(runtime: Runtime, name: str, arguments: dict | None = None) -> dict:
    return runtime.agent.tools.call(name, arguments)


def test_registry_reports_unknown_tool(runtime: Runtime) -> None:
    result = _call(runtime, "drop_database")
    assert result["ok"] is False
    assert "未知工具" in result["error"]


def test_registry_reports_bad_arguments(runtime: Runtime) -> None:
    result = _call(runtime, "get_table_schema", {"tables": ["sales_order"], "unknown": 1})
    assert result["ok"] is False
    assert "参数错误" in result["error"]


def test_search_tables_tool(runtime: Runtime) -> None:
    result = _call(runtime, "search_tables", {"query": "BOM 子件", "top_k": 3})
    assert result["ok"] is True
    assert result["data"]["tables"][0]["name"] == "bom"


def test_get_table_schema_hides_disabled_columns(runtime: Runtime) -> None:
    result = _call(runtime, "get_table_schema", {"tables": ["sales_order"]})
    assert result["ok"] is True
    table = result["data"]["tables"][0]
    names = {column["name"] for column in table["columns"]}
    assert "internal_note" not in names
    assert "customer_phone" in names


def test_search_values_returns_dimension_values(runtime: Runtime) -> None:
    result = _call(
        runtime,
        "search_values",
        {"table": "sales_order", "column": "customer_name", "query": "杭州"},
    )
    assert result["ok"] is True
    assert result["data"]["values"] == ["杭州智造科技有限公司"]


def test_search_values_unknown_column_lists_available_columns(runtime: Runtime) -> None:
    result = _call(
        runtime,
        "search_values",
        {"table": "sales_order", "column": "设备", "query": "机床"},
    )
    assert result["ok"] is False
    assert "可用字段" in result["error"]
    assert "customer_name" in result["error"]


def test_search_values_empty_result_tells_model_to_stop_retrying(runtime: Runtime) -> None:
    result = _call(
        runtime,
        "search_values",
        {"table": "sales_order", "column": "customer_name", "query": "不存在的客户"},
    )
    assert result["ok"] is True
    assert result["data"]["count"] == 0
    assert "不要继续" in result["data"]["hint"]


def test_search_values_blocks_sensitive_column(runtime: Runtime) -> None:
    result = _call(
        runtime,
        "search_values",
        {"table": "sales_order", "column": "customer_phone"},
    )
    assert result["ok"] is False
    assert "敏感字段" in result["error"]


def test_search_values_blocks_disabled_column(runtime: Runtime) -> None:
    result = _call(
        runtime,
        "search_values",
        {"table": "sales_order", "column": "internal_note"},
    )
    assert result["ok"] is False
    assert "禁用" in result["error"]


def test_execute_sql_blocks_unsafe_sql(runtime: Runtime) -> None:
    result = _call(runtime, "execute_sql", {"sql": "SELECT * FROM sales_order"})
    assert result["ok"] is True
    assert result["data"]["success"] is False
    assert result["data"]["stage"] == "guard"


def test_execute_sql_returns_rows_and_truncates(runtime: Runtime) -> None:
    result = _call(
        runtime,
        "execute_sql",
        {"sql": "SELECT order_id FROM sales_order ORDER BY order_id", "max_rows_limit": 2},
    )
    data = result["data"]
    assert data["success"] is True
    assert data["row_count"] == 2
    assert data["truncated"] is True
    assert data["columns"] == ["order_id"]


def test_save_experience_is_candidate_only(runtime: Runtime) -> None:
    sql = "SELECT order_id FROM sales_order WHERE status = 'pending'"
    saved = _call(
        runtime,
        "save_experience",
        {"question": "待发货订单有哪些", "sql": sql, "category": "detail"},
    )
    assert saved["ok"] is True
    assert saved["data"]["record"]["status"] == "candidate"

    hidden = _call(runtime, "search_experience", {"question": "待发货订单有哪些"})
    assert hidden["data"]["count"] == 0

    runtime.store.verify(int(saved["data"]["record"]["id"]))
    visible = _call(runtime, "search_experience", {"question": "待发货订单有哪些"})
    assert visible["data"]["count"] == 1
    assert visible["data"]["examples"][0]["sql"] == sql


def test_save_experience_rejects_unsafe_sql(runtime: Runtime) -> None:
    result = _call(
        runtime,
        "save_experience",
        {"question": "所有数据", "sql": "SELECT * FROM sales_order"},
    )
    assert result["ok"] is False
    assert "未通过护栏" in result["error"]


def test_repair_sql_tool(runtime: Runtime) -> None:
    result = _call(
        runtime,
        "repair_sql",
        {
            "sql": "SELECT o.order_id, o.missing FROM sales_order o",
            "error": "no such column: o.missing",
        },
    )
    assert result["ok"] is True
    assert result["data"]["changed"] is True
    assert "o.missing" not in result["data"]["sql"]
