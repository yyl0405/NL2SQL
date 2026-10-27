# 工具调用协议

## 1. 两层返回结构

`ToolRegistry.call` 统一返回：

```json
{ "ok": true, "data": { "...": "工具业务结果" } }
```

- `ok=false`：工具无法运行（未知工具、参数错误、权限/输入错误）。错误文本会作为 observation 回灌给模型。
- `ok=true, data.success=false`：工具正常运行，但业务失败（如 SQL 未过 guard、执行报错）。模型需要根据 `data.error` 修正。

这样区分“调用失败”和“查询失败”，避免模型把参数错误误判为 SQL 错误。

---

## 2. 工具定义

### 2.1 `search_tables`

```json
{
  "type": "function",
  "function": {
    "name": "search_tables",
    "description": "Retrieve relevant tables from the schema catalog using lexical schema linking.",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {"type": "string", "description": "用户问题或其中的业务关键词"},
        "top_k": {"type": "integer", "description": "返回候选表数量，1-20", "default": 5}
      },
      "required": ["query"]
    }
  }
}
```

返回示例：

```json
{
  "ok": true,
  "data": {
    "query": "哪些物料库存低于安全库存",
    "count": 5,
    "tables": [
      {"name": "inventory", "description": "库存快照表...", "score": 9.42, "columns": ["material_id", "warehouse", "on_hand_qty", "locked_qty"]},
      {"name": "material", "description": "物料主数据...", "score": 7.18, "columns": ["material_id", "material_name", "category", "unit", "safety_stock", "standard_cost"]}
    ]
  }
}
```

### 2.2 `get_table_schema`

```json
{
  "type": "function",
  "function": {
    "name": "get_table_schema",
    "description": "Get full column definitions, disabled/sensitive markers, required join filters and relationships for up to 8 tables.",
    "parameters": {
      "type": "object",
      "properties": {
        "tables": {"type": "array", "items": {"type": "string"}, "description": "需要查看的表名列表"}
      },
      "required": ["tables"]
    }
  }
}
```

返回字段：`tables[].columns[]`（name/type/description/synonyms/sensitive/disabled）、`relationships`、`business_rules`、`missing`、`returned_tables`。禁用列不会返回。

### 2.3 `search_values`

```json
{
  "type": "function",
  "function": {
    "name": "search_values",
    "description": "Look up real values of a low-cardinality dimension column. The column must be an exact name from get_table_schema.",
    "parameters": {
      "type": "object",
      "properties": {
        "table": {"type": "string"},
        "column": {"type": "string"},
        "query": {"type": "string"},
        "limit": {"type": "integer", "default": 10}
      },
      "required": ["table", "column"]
    }
  }
}
```

返回按匹配度排序的真实值。敏感字段、禁用字段、禁用表直接 `ok=false`；未知字段的报错会附带该表最多 20 个可用字段，帮助模型一次纠正而不是连续试列。`count=0` 时返回 `hint`，提示模型不要继续对同一列反复试值。

### 2.4 `validate_sql`

```json
{
  "type": "function",
  "function": {
    "name": "validate_sql",
    "description": "Validate a candidate SQL before execution.",
    "parameters": {
      "type": "object",
      "properties": {"sql": {"type": "string"}},
      "required": ["sql"]
    }
  }
}
```

返回：

```json
{
  "ok": true,
  "data": {
    "valid": false,
    "sql": "SELECT * FROM sales_order",
    "tables": ["sales_order"],
    "columns": [],
    "errors": [{"level": "error", "code": "select_star", "message": "禁止 SELECT *，必须显式列出最小必要字段"}],
    "warnings": []
  }
}
```

错误码：`empty_sql`、`sql_too_long`、`not_select`、`multi_statement`、`write_keyword`、`unknown_table`、`disabled_table`、`required_filter`、`select_star`、`unknown_column`、`disabled_column`、`sensitive_column`、`table_hint`、`offset_fetch`。

启用权限（`--principal`）后追加：`permission_table`、`permission_column`、`row_filter_missing`——越权表报的是 `permission_table` 而不是 `unknown_table`，避免用错误消息把权限策略泄露成"这张表不存在"。启用 AST 层（`NL2SQL_AST_GUARD=on`）后追加：`ast_unavailable`、`ast_parse_failed`、`dangerous_function`、`metadata_access`，以及 warning `ast_table_bypass`；这两类 AST error 是 fail-closed，不会退回正则判定。扇出与多表Join 只产 warning：`join_fanout`、`wide_join`。

### 2.5 `execute_sql`

```json
{
  "type": "function",
  "function": {
    "name": "execute_sql",
    "description": "Execute a validated, read-only SELECT with row/time limits.",
    "parameters": {
      "type": "object",
      "properties": {
        "sql": {"type": "string"},
        "max_rows_limit": {"type": "integer", "description": "不能超过服务端配置"}
      },
      "required": ["sql"]
    }
  }
}
```

执行前会再次跑 guard（不信任模型“我已经校验过”的说法）。返回：

```json
{
  "ok": true,
  "data": {
    "success": true,
    "columns": ["total_amount"],
    "rows": [[62280.0]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 0.42,
    "sql": "SELECT ROUND(SUM(...), 2) AS total_amount ..."
  }
}
```

失败示例：

```json
{"ok": true, "data": {"success": false, "stage": "guard", "error": "SQL 未通过安全/权限护栏", "guard": {"valid": false, "errors": [...]}}}
{"ok": true, "data": {"success": false, "error": "no such column: o.amount", "error_code": "sql_error"}}
```

`error_code`：`db_not_found`、`timeout`、`sql_error`。

### 2.6 `repair_sql`

```json
{
  "type": "function",
  "function": {
    "name": "repair_sql",
    "description": "Apply deterministic, low-risk repairs after a database error.",
    "parameters": {
      "type": "object",
      "properties": {
        "sql": {"type": "string"},
        "error": {"type": "string"}
      },
      "required": ["sql", "error"]
    }
  }
}
```

返回：

```json
{
  "ok": true,
  "data": {
    "sql": "SELECT o.order_id FROM sales_order o",
    "changed": true,
    "applied": ["remove_invalid_projection"],
    "notes": ["已从 SELECT 中移除报错字段；WHERE/JOIN/GROUP BY 保持不变"]
  }
}
```

`changed=false` 表示没有命中安全规则，模型应重新生成或升级人工。

### 2.7 `search_experience` / `save_experience`

```json
{"name": "search_experience", "arguments": {"question": "...", "top_k": 3}}
{"name": "save_experience", "arguments": {"question": "...", "sql": "SELECT ...", "category": "inventory"}}
```

`search_experience` 只返回 verified 且 schema hash 匹配的样本，并按问题类别加权（同类别加分，默认 0.12）；返回值带 `category` 说明本次分类结果。`save_experience` 只写 candidate，且 SQL 必须通过 guard；`category` 缺省时由规则分类器自动填写。

除工具调用外，Agent 在每轮运行前会**自动**把召回到的 verified 经验注入 system prompt（默认 3 条 / 1200 字符预算），模型不需要显式调用 `search_experience` 也能拿到参考；两者共用同一 store 与同一“候选经验不可召回”的过滤条件。

---

## 3. 模型侧消息序列

一次成功的调用在内部表现为：

```text
user:      2026年6月华东地区销售订单总金额是多少？
assistant: tool_calls=[search_tables({"query": "...", "top_k": 5})]
tool:      {"ok": true, "data": {"tables": [...]}}
assistant: tool_calls=[get_table_schema({"tables": ["sales_order", "order_item", ...]})]
tool:      {"ok": true, "data": {"tables": [...], "relationships": [...]}}
assistant: tool_calls=[search_experience({"question": "..."})]
tool:      {"ok": true, "data": {"count": 0, "examples": []}}
assistant: tool_calls=[validate_sql({"sql": "SELECT ..."})]
tool:      {"ok": true, "data": {"valid": true, "tables": ["order_item", "sales_order"]}}
assistant: tool_calls=[execute_sql({"sql": "SELECT ..."})]
tool:      {"ok": true, "data": {"success": true, "rows": [[62280.0]], ...}}
assistant: {"status":"ok","answer":"查询结果：62280.0","sql":"SELECT ...","confidence":0.92}
```

错误修复路径：

```text
assistant: tool_calls=[execute_sql({"sql": "... invalid column ..."})]
tool:      {"ok": true, "data": {"success": false, "error": "no such column: o.amount"}}
assistant: tool_calls=[repair_sql({"sql": "...", "error": "no such column: o.amount"})]
tool:      {"ok": true, "data": {"sql": "... fixed ...", "changed": true, "applied": ["remove_invalid_projection"]}}
assistant: tool_calls=[execute_sql({"sql": "... fixed ..."})]
tool:      {"ok": true, "data": {"success": true, ...}}
```

修复轮次有硬上限（`NL2SQL_MAX_REPAIRS`，默认 2）。超限后的 `repair_sql` 调用不会进入工具层，Agent 直接返回：

```text
assistant: tool_calls=[repair_sql({"sql": "...", "error": "..."})]
tool:      {"ok": false, "error": "确定性修复已达上限 2 轮，不再接受新的修复调用。请停止修复，输出 status=needs_human 并说明未解决的报错。"}
```

---

## 4. 接入在线模型时的注意事项

- 本项目使用 OpenAI 兼容的 `chat.completions`；若服务不支持 `tools`，需要在 `OpenAICompatibleLLM` 中降级为 JSON 指令模式。
- 建议关闭思考模式（如 DashScope 的 `enable_thinking=false`），避免推理 token 吃掉 SQL 输出预算；对应生产经验见 `docs/EXTRACTION.md`。
- 工具结果必须保持 JSON 可序列化；`to_jsonable` 会处理 Decimal/bytes/None。
- 模型并行发起多个 tool call 时，Agent 按顺序执行并逐个回灌 observation；有依赖关系的调用不应并行。
- 开启双通道复核（`--dual-channel`）时会有**第二次不带 `tools` 的 `chat.completions` 调用**：第二通道故意不给工具，否则它会复用主通道已经信任的检索与值对齐结果，"一致"就只能证明同一个循环很稳定。它的返回按 `{"sql": "..."}` 契约解析，且与主通道共用同一个 `execute_sql`（同护栏、同行限、同超时），见 `docs/ARCHITECTURE.md` 的复核小节。
