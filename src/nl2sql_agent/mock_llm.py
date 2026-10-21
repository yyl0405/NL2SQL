"""Offline rule-based tool-calling model.

This is a deterministic stand-in for an LLM so the repository can demo the
full agent loop and run CI without an API key. It is *not* the intelligence of
the project; use NL2SQL_PROVIDER=openai for real generation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from .catalog import Catalog
from .demo.seed import CUSTOMER_NAMES, MATERIAL_IDS, MATERIAL_NAMES, REGIONS
from .llm import LLMResponse, ToolCall


@dataclass
class Plan:
    label: str
    sql: str | None = None
    probe: tuple[str, str, str] | None = None
    refusal: bool = False
    reason: str = ""


class RulePlanner:
    """Template baseline keyed by business intent, used only by the mock model."""

    def __init__(self, catalog: Catalog, today: str = "2026-07-01") -> None:
        self.catalog = catalog
        self.today = today

    def plan(self, question: str) -> Plan:
        q = _normalize_text(question)
        lowered = q.lower()

        if any(word in q for word in ("满意度", "评分", "天气", "股价", "员工工资")):
            return Plan("refusal", refusal=True, reason="演示 schema 不包含该指标")

        if "齐套" in q:
            return self._kitting(q)
        if "bom" in lowered or "子件" in q or "物料清单" in q or ("展开" in q and "物料" in q):
            return self._bom(q)
        if "安全库存" in q and ("低于" in q or "不足" in q or "小于" in q):
            return self._low_stock()
        if "可用库存" in q or ("库存" in q and "可用" in q):
            return self._available_stock(q)
        if "在途" in q and "供应商" in q:
            return self._in_transit()
        if "完工率" in q:
            return self._work_order_rate(q)
        if "逾期" in q or "未发货" in q or ("发货" in q and ("未" in q or "没" in q)):
            return self._overdue(q)
        if "库存金额" in q or ("库存" in q and "金额" in q):
            return self._inventory_value()
        if "分类" in q and "销售" in q and ("金额" in q or "额" in q):
            return self._sales_by_category(q)
        if (
            "各地区" in q
            or "各区域" in q
            or "分地区" in q
            or ("地区" in q and "排名" in q)
            or ("区域" in q and "排名" in q)
        ):
            return self._sales_by_region(q)
        if "客户" in q and any(word in lowered for word in ("最高", "最低", "前", "top")):
            return self._top_customers(q)
        if "状态" in q and "订单" in q and any(word in q for word in ("数量", "统计", "多少")):
            return self._order_status_count()
        if "采购" in q and ("到货" in q or "收货" in q):
            return self._purchase_received(q)
        if "完工数量" in q and ("低于" in q or "小于" in q):
            return self._low_completion()
        if "客户" in q and "订单" in q and "明细" in q:
            return self._customer_orders(q)
        if "销售" in q and any(word in q for word in ("金额", "总额", "销售额")):
            return self._revenue_total(q)
        return Plan("unsupported", reason="没有匹配到演示库支持的业务指标")

    def _revenue_total(self, q: str) -> Plan:
        filters: list[str] = []
        region = _find_region(q)
        date_filter = _date_filter("o.order_date", q)
        if region:
            filters.append(f"o.region = '{_lit(region)}'")
        if date_filter:
            filters.append(date_filter)
        where = (" WHERE " + " AND ".join(filters)) if filters else ""
        sql = (
            "SELECT ROUND(SUM(oi.qty * oi.unit_price * (1 - oi.discount)), 2) AS total_amount "
            "FROM sales_order o JOIN order_item oi ON oi.order_id = o.order_id"
            + where
        )
        return Plan("revenue_total", sql=sql)

    def _sales_by_region(self, q: str) -> Plan:
        date_filter = _date_filter("o.order_date", q)
        where = f" WHERE {date_filter}" if date_filter else ""
        sql = (
            "SELECT o.region, ROUND(SUM(oi.qty * oi.unit_price * (1 - oi.discount)), 2) AS total_amount "
            "FROM sales_order o JOIN order_item oi ON oi.order_id = o.order_id"
            + where
            + " GROUP BY o.region ORDER BY total_amount DESC"
        )
        return Plan("sales_by_region", sql=sql)

    def _top_customers(self, q: str) -> Plan:
        date_filter = _date_filter("o.order_date", q)
        where = f" WHERE {date_filter}" if date_filter else ""
        limit = _top_n(q)
        sql = (
            "SELECT o.customer_name, ROUND(SUM(oi.qty * oi.unit_price * (1 - oi.discount)), 2) AS total_amount "
            "FROM sales_order o JOIN order_item oi ON oi.order_id = o.order_id"
            + where
            + f" GROUP BY o.customer_name ORDER BY total_amount DESC LIMIT {limit}"
        )
        return Plan("top_customers", sql=sql)

    def _overdue(self, q: str) -> Plan:
        as_of = _as_of_date(q)
        sql = (
            "SELECT o.order_id, o.customer_name, o.due_date "
            "FROM sales_order o "
            f"WHERE o.status = 'pending' AND o.due_date < '{as_of}' "
            "ORDER BY o.due_date"
        )
        return Plan("overdue", sql=sql)

    def _low_stock(self) -> Plan:
        sql = (
            "SELECT m.material_id, m.material_name, "
            "SUM(i.on_hand_qty - i.locked_qty) AS available_qty, m.safety_stock "
            "FROM material m JOIN inventory i ON i.material_id = m.material_id "
            "GROUP BY m.material_id, m.material_name, m.safety_stock "
            "HAVING SUM(i.on_hand_qty - i.locked_qty) < m.safety_stock "
            "ORDER BY (m.safety_stock - SUM(i.on_hand_qty - i.locked_qty)) DESC"
        )
        return Plan("low_stock", sql=sql)

    def _available_stock(self, q: str) -> Plan:
        name = _find_material_name(q)
        if not name:
            return Plan("available_stock", reason="问题中没有识别到物料名称")
        sql = (
            "SELECT ROUND(SUM(i.on_hand_qty - i.locked_qty), 2) AS available_qty "
            "FROM inventory i JOIN material m ON m.material_id = i.material_id "
            f"WHERE m.material_name = '{_lit(name)}'"
        )
        return Plan("available_stock", sql=sql, probe=("material", "material_name", name))

    def _in_transit(self) -> Plan:
        sql = (
            "SELECT po.supplier_name, SUM(po.qty) AS in_transit_qty "
            "FROM purchase_order po WHERE po.status = 'in_transit' "
            "GROUP BY po.supplier_name ORDER BY in_transit_qty DESC, po.supplier_name"
        )
        return Plan("in_transit", sql=sql)

    def _work_order_rate(self, q: str) -> Plan:
        date_filter = _date_filter("wo.planned_end", q)
        where = f" WHERE {date_filter}" if date_filter else ""
        sql = (
            "SELECT wo.wo_id, wo.planned_qty, wo.completed_qty, "
            "ROUND(wo.completed_qty * 1.0 / wo.planned_qty, 4) AS completion_rate "
            "FROM work_order wo"
            + where
            + " ORDER BY wo.wo_id"
        )
        return Plan("work_order_rate", sql=sql)

    def _order_status_count(self) -> Plan:
        sql = (
            "SELECT status, COUNT(*) AS order_count FROM sales_order "
            "GROUP BY status ORDER BY order_count DESC"
        )
        return Plan("order_status_count", sql=sql)

    def _inventory_value(self) -> Plan:
        sql = (
            "SELECT m.category, ROUND(SUM(i.on_hand_qty * m.standard_cost), 2) AS inventory_value "
            "FROM material m JOIN inventory i ON i.material_id = m.material_id "
            "GROUP BY m.category ORDER BY inventory_value DESC"
        )
        return Plan("inventory_value", sql=sql)

    def _sales_by_category(self, q: str) -> Plan:
        date_filter = _date_filter("o.order_date", q)
        where = f" WHERE {date_filter}" if date_filter else ""
        sql = (
            "SELECT m.category, ROUND(SUM(oi.qty * oi.unit_price * (1 - oi.discount)), 2) AS sales_amount "
            "FROM sales_order o "
            "JOIN order_item oi ON oi.order_id = o.order_id "
            "JOIN material m ON m.material_id = oi.material_id"
            + where
            + " GROUP BY m.category ORDER BY sales_amount DESC"
        )
        return Plan("sales_by_category", sql=sql)

    def _low_completion(self) -> Plan:
        sql = (
            "SELECT wo.wo_id, wo.planned_qty, wo.completed_qty FROM work_order wo "
            "WHERE wo.completed_qty < wo.planned_qty * 0.8 ORDER BY wo.wo_id"
        )
        return Plan("low_completion", sql=sql)

    def _purchase_received(self, q: str) -> Plan:
        date_filter = _date_filter("po.expected_date", q)
        where = " WHERE po.status = 'received'"
        if date_filter:
            where += f" AND {date_filter}"
        sql = (
            "SELECT m.material_name, SUM(po.qty) AS received_qty "
            "FROM purchase_order po JOIN material m ON m.material_id = po.material_id"
            + where
            + " GROUP BY m.material_name ORDER BY received_qty DESC"
        )
        return Plan("purchase_received", sql=sql)

    def _customer_orders(self, q: str) -> Plan:
        name = _find_customer_name(q)
        if not name:
            return Plan("customer_orders", reason="问题中没有识别到客户名称")
        sql = (
            "SELECT o.order_id, o.order_date, o.due_date, o.status, "
            "ROUND(SUM(oi.qty * oi.unit_price * (1 - oi.discount)), 2) AS amount "
            "FROM sales_order o JOIN order_item oi ON oi.order_id = o.order_id "
            f"WHERE o.customer_name = '{_lit(name)}' "
            "GROUP BY o.order_id, o.order_date, o.due_date, o.status "
            "ORDER BY o.order_date"
        )
        return Plan("customer_orders", sql=sql, probe=("sales_order", "customer_name", name))

    def _bom(self, q: str) -> Plan:
        name = _find_material_name(q) or "整机设备"
        parent_id = MATERIAL_IDS.get(name)
        if not parent_id:
            return Plan("bom", reason=f"未找到物料: {name}")
        sql = (
            "WITH RECURSIVE bom_tree(parent_id, child_id, total_qty, level) AS ("
            "SELECT b.parent_material_id, b.child_material_id, b.qty_per_parent, 1 "
            "FROM bom b WHERE b.parent_material_id = '" + _lit(parent_id) + "' "
            "UNION ALL "
            "SELECT bt.child_id, b.child_material_id, bt.total_qty * b.qty_per_parent, bt.level + 1 "
            "FROM bom_tree bt JOIN bom b ON b.parent_material_id = bt.child_id) "
            "SELECT bt.child_id AS material_id, m.material_name, "
            "ROUND(SUM(bt.total_qty), 4) AS total_qty, MIN(bt.level) AS first_level "
            "FROM bom_tree bt JOIN material m ON m.material_id = bt.child_id "
            "GROUP BY bt.child_id, m.material_name ORDER BY first_level, bt.child_id"
        )
        return Plan("bom", sql=sql, probe=("material", "material_name", name))

    def _kitting(self, q: str) -> Plan:
        match = re.search(r"ORD-\d+", q, re.IGNORECASE)
        if not match:
            return Plan("kitting", reason="问题中没有识别到订单号")
        order_id = match.group(0).upper()
        sql = (
            "WITH RECURSIVE bt(root_id, parent_id, qty, level) AS ("
            "SELECT oi.material_id, oi.material_id, oi.qty, 0 FROM order_item oi "
            f"WHERE oi.order_id = '{order_id}' "
            "UNION ALL "
            "SELECT bt.root_id, b.child_material_id, bt.qty * b.qty_per_parent, bt.level + 1 "
            "FROM bt JOIN bom b ON b.parent_material_id = bt.parent_id), "
            "req AS (SELECT material_id, SUM(qty) AS required_qty FROM ("
            f"SELECT oi.material_id, oi.qty FROM order_item oi WHERE oi.order_id = '{order_id}' "
            "AND oi.material_id NOT IN (SELECT DISTINCT parent_material_id FROM bom) "
            "UNION ALL SELECT bt.parent_id AS material_id, bt.qty FROM bt WHERE bt.level > 0) "
            "GROUP BY material_id), "
            "sup AS (SELECT m.material_id, "
            "COALESCE((SELECT SUM(i.on_hand_qty - i.locked_qty) FROM inventory i "
            "WHERE i.material_id = m.material_id), 0) + "
            "COALESCE((SELECT SUM(po.qty) FROM purchase_order po "
            "WHERE po.material_id = m.material_id AND po.status = 'in_transit'), 0) AS supply_qty "
            "FROM material m) "
            "SELECT r.material_id, m.material_name, r.required_qty, s.supply_qty, "
            "ROUND(MIN(1.0, s.supply_qty * 1.0 / r.required_qty), 4) AS fill_rate "
            "FROM req r JOIN material m ON m.material_id = r.material_id "
            "JOIN sup s ON s.material_id = r.material_id "
            "ORDER BY fill_rate ASC, r.material_id"
        )
        return Plan("kitting", sql=sql)


class RuleBasedLLM:
    def __init__(self, catalog: Catalog, today: str = "2026-07-01") -> None:
        self.planner = RulePlanner(catalog, today)
        self._counter = 0
        self._resolved_sql: dict[str, str] = {}

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> LLMResponse:
        question = _first_user_question(messages)
        plan = self.planner.plan(question)
        if plan.refusal or (plan.sql is None and plan.label == "unsupported"):
            return self._final_clarification(question, plan)

        last = _last_tool(messages)
        if last is None:
            return self._call("search_tables", {"query": question, "top_k": 8})

        name = last.get("tool")
        data = last.get("data") or {}
        used = _used_tools(messages)

        if name == "search_tables":
            candidates = [item.get("name") for item in data.get("tables", []) if item.get("name")]
            tables = self._schema_tables(plan) or candidates[:8]
            if not tables:
                tables = ["sales_order", "order_item", "material"]
            return self._call("get_table_schema", {"tables": tables})

        if name == "get_table_schema":
            return self._call("search_experience", {"question": question, "top_k": 3})

        if name == "search_experience":
            examples = data.get("examples") or []
            if examples and float(examples[0].get("score", 0)) >= 0.9:
                self._resolved_sql[question] = str(examples[0]["sql"])
            if plan.sql is None:
                return self._final_clarification(question, plan)
            if plan.probe and "search_values" not in used:
                table, column, value = plan.probe
                return self._call(
                    "search_values",
                    {"table": table, "column": column, "query": value, "limit": 10},
                )
            return self._call("validate_sql", {"sql": self._sql_for(question, plan)})

        if name == "search_values":
            if plan.sql is None:
                return self._final_clarification(question, plan)
            return self._call("validate_sql", {"sql": self._sql_for(question, plan)})

        if name == "validate_sql":
            if data.get("valid"):
                return self._call("execute_sql", {"sql": data.get("sql") or self._sql_for(question, plan)})
            return self._final_failure(question, plan, "SQL 未通过护栏", data.get("errors") or [])

        if name == "execute_sql":
            if data.get("success"):
                return self._final_success(question, plan, data)
            if "repair_sql" not in used:
                return self._call(
                    "repair_sql",
                    {"sql": data.get("sql") or self._sql_for(question, plan), "error": data.get("error", "")},
                )
            return self._final_failure(question, plan, data.get("error", "执行失败"), [])

        if name == "repair_sql":
            if data.get("changed") and data.get("sql"):
                return self._call("execute_sql", {"sql": data["sql"]})
            return self._final_failure(question, plan, "确定性修复未命中", [])

        return self._final_clarification(question, plan)

    def _sql_for(self, question: str, plan: Plan) -> str:
        return self._resolved_sql.get(question) or plan.sql or ""

    def _schema_tables(self, plan: Plan) -> list[str]:
        """Physical tables referenced by the planned SQL, excluding CTE aliases."""
        names: list[str] = []
        for match in re.finditer(r"\b(?:from|join)\s+([A-Za-z_]\w*)", plan.sql or "", re.IGNORECASE):
            name = match.group(1)
            if name in names or self.planner.catalog.table(name) is None:
                continue
            names.append(name)
        return names

    def _call(self, name: str, arguments: dict[str, Any]) -> LLMResponse:
        self._counter += 1
        return LLMResponse(
            tool_calls=[ToolCall(id=f"rule-call-{self._counter}", name=name, arguments=arguments)],
            finish_reason="tool_calls",
        )

    def _final_success(self, question: str, plan: Plan, data: dict[str, Any]) -> LLMResponse:
        rows = data.get("rows") or []
        columns = data.get("columns") or []
        row_count = int(data.get("row_count", len(rows)))
        if row_count == 1 and len(columns) == 1:
            answer = f"查询结果：{rows[0][0]}"
        elif row_count == 0:
            answer = "查询完成，但没有匹配的数据。"
        else:
            preview = []
            for row in rows[:3]:
                pairs = [f"{columns[i]}={row[i]}" for i in range(min(len(columns), len(row)))]
                preview.append("；".join(pairs))
            answer = f"查询完成，共 {row_count} 行。" + (" 示例：" + " | ".join(preview) if preview else "")
        if data.get("truncated"):
            answer += " 结果已达到返回行数上限，已截断。"
        return self._final_json(
            status="ok",
            answer=answer,
            sql=data.get("sql") or self._sql_for(question, plan),
            confidence=0.92,
            assumptions=[],
        )

    def _final_clarification(self, question: str, plan: Plan) -> LLMResponse:
        answer = (
            "当前演示库只覆盖订单、库存、采购、工单、BOM 与齐套等制造业务指标。"
            "请补充或换一个可查询的指标。"
        )
        if plan.reason:
            answer += f"（{plan.reason}）"
        return self._final_json(
            status="needs_clarification",
            answer=answer,
            sql=None,
            confidence=0.2,
            assumptions=[],
        )

    def _final_failure(
        self,
        question: str,
        plan: Plan,
        error: str,
        issues: list[Any],
    ) -> LLMResponse:
        messages = []
        for item in issues[:4]:
            if isinstance(item, dict):
                messages.append(str(item.get("message", "")))
            else:
                messages.append(str(item))
        detail = "；".join(m for m in messages if m) or error
        return self._final_json(
            status="needs_human",
            answer=f"查询未能完成：{detail}",
            sql=self._sql_for(question, plan) or None,
            confidence=0.1,
            assumptions=[],
        )

    @staticmethod
    def _final_json(
        *,
        status: str,
        answer: str,
        sql: str | None,
        confidence: float,
        assumptions: list[str],
    ) -> LLMResponse:
        return LLMResponse(
            content=json.dumps(
                {
                    "status": status,
                    "answer": answer,
                    "sql": sql,
                    "confidence": confidence,
                    "assumptions": assumptions,
                },
                ensure_ascii=False,
            ),
            finish_reason="stop",
        )


def _first_user_question(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") == "user":
            return str(message.get("content", ""))
    return ""


def _last_tool(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        try:
            payload = json.loads(str(message.get("content", "{}")))
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
    return None


def _used_tools(messages: list[dict[str, Any]]) -> set[str]:
    used: set[str] = set()
    for message in messages:
        if message.get("role") != "tool":
            continue
        try:
            payload = json.loads(str(message.get("content", "{}")))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("tool"):
            used.add(str(payload["tool"]))
    return used


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _lit(value: str) -> str:
    return str(value).replace("'", "''")


def _find_region(q: str) -> str | None:
    for region in REGIONS:
        if region in q:
            return region
    return None


def _find_material_name(q: str) -> str | None:
    for name in sorted(MATERIAL_NAMES, key=len, reverse=True):
        if name in q:
            return name
    match = re.search(r"物料[“\"「]?([^，。？,\s的]+)", q)
    return match.group(1).strip("“”\"「」") if match else None


def _find_customer_name(q: str) -> str | None:
    for name in sorted(CUSTOMER_NAMES, key=len, reverse=True):
        if name in q:
            return name
    match = re.search(r"客户[“\"「]?([^，。？,\s的]+)", q)
    return match.group(1).strip("“”\"「」") if match else None


def _top_n(q: str, default: int = 3) -> int:
    for pattern in (r"前\s*(\d+)", r"top\s*(\d+)", r"(\d+)\s*个"):
        match = re.search(pattern, q, re.IGNORECASE)
        if match:
            return max(1, min(20, int(match.group(1))))
    return default


def _as_of_date(q: str) -> str:
    match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", q)
    if match:
        year, month, day = (int(part) for part in match.groups())
        return date(year, month, day).isoformat()
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", q)
    if match:
        return match.group(0)
    return "2026-07-01"


def _month_range(q: str) -> tuple[str, str] | None:
    match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", q)
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start.isoformat(), end.isoformat()


def _date_filter(column: str, q: str) -> str | None:
    date_range = _month_range(q)
    if not date_range:
        return None
    start, end = date_range
    return f"{column} >= '{start}' AND {column} < '{end}'"
