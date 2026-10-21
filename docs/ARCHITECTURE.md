# 架构设计

## 1. 目标与非目标

**目标**

- 在制造 ERP schema 上，把自然语言问题可靠地转换为可执行 SQL。
- 每一步都可观测：模型看到了什么、调用了什么工具、SQL 是否通过护栏、执行是否成功。
- 失败可恢复：确定性修复优先，无法修复时明确升级，不编造结果。
- 可离线复现、可自动评测、可扩展到真实 LLM 与真实数据库。

**非目标**

- 不做通用 BI 自然语言界面（图表、钻取、权限 UI）。
- 不替代数据库权限体系；应用层 guard 只是第一道闸门。
- 不追求“一次生成即正确”，而是把正确性建立在工具反馈与执行验证上。

---

## 2. 组件图

```text
┌──────────────────────────── NL2SQLAgent ────────────────────────────┐
│  system prompt + user question                                      │
│  ┌────────────── loop (max_steps) ──────────────┐                   │
│  │ chat(messages, tool_specs)                    │                   │
│  │   ├─ no tool_calls → finalize(JSON)           │                   │
│  │   └─ tool_calls → ToolRegistry.call(...)      │                   │
│  │        observation 回灌 messages               │                   │
│  └───────────────────────────────────────────────┘                   │
└───────────────┬─────────────────────────────────────────────────────┘
                │
    ┌───────────┼──────────────┬──────────────┬───────────────┐
    ▼           ▼              ▼              ▼               ▼
 Catalog     SQLGuard      SQLRepairer    ExperienceStore   Read-only DB
 (schema)    (permission)  (repair)       (memory)          (SQLite mode=ro)
```

| 组件 | 文件 | 职责 |
|---|---|---|
| `NL2SQLAgent` | `agent.py` | 多轮工具调用、trace、最终状态校验、结果组装 |
| `Catalog` | `catalog.py` | schema 加载、schema hash、BM25 表召回、关系/业务口径 |
| `SQLGuard` | `guard.py` | 只读、表白名单、必填过滤、列级权限、`SELECT *` 检查 |
| `SQLRepairer` | `repair.py` | 基于数据库错误的确定性修复 |
| `ExperienceStore` | `memory/experience.py` | 经验持久化、准入、schema 失效、相似度召回 |
| `ToolRegistry` | `tools/base.py` | 工具注册、JSON Schema、参数绑定、错误回灌 |
| `run_readonly_query` | `tools/sql_tools.py` | 只读执行、超时、行数限制、截断标记 |
| `evaluate` | `evaluation/evaluator.py` | 冻结集执行、结果集比对、指标与报告 |

---

## 3. Agent 循环

伪代码（对应 `NL2SQLAgent.run`）：

```python
messages = [system_prompt, user_question]
for step in range(1, max_steps + 1):
    response = llm.chat(messages, tools=registry.specs())

    if not response.tool_calls:
        return finalize(response)          # 解析最终 JSON

    messages.append(assistant_tool_calls(response))

    for call in response.tool_calls:
        payload = registry.call(call.name, call.arguments)
        messages.append(tool_observation(call.id, payload))
        trace.append(summarize(payload))

        if call.name == "validate_sql" and payload.ok:
            last_sql = payload.data.sql
        if call.name == "execute_sql" and payload.data.success:
            last_execution = payload.data
return max_steps_result(last_sql, trace)
```

设计要点：

1. **工具错误不是异常**：未知工具、参数不匹配、SQL 被拒、执行报错都会作为 observation 返回模型，让其自我修正。
2. **执行证据优先**：只有 `execute_sql` 成功才会记录 `last_execution`；最终 `status=ok` 必须存在执行证据。
3. **trace 可审计**：每次工具调用记录 step、参数、ok、摘要、耗时，便于定位“模型为什么这么写”。
4. **步数上限**：默认 12 轮，防止模型在错误路径上无限循环；超限返回 `max_steps` 而不是伪造答案。

### 最终状态机

```text
LLM final JSON
   │
   ├─ status=ok 且 last_execution 存在 ──────────────> ok
   ├─ status=ok 但无执行证据 ────────────────────────> needs_clarification（置信度 ≤ 0.4）
   ├─ status∈{needs_clarification, refused, needs_human} ─> 原样返回
   └─ status 非法/缺失
          ├─ 有执行证据 ─────────────────────────────> ok
          ├─ 文本含“请补充/无法/不包含/不支持” ───────> needs_clarification
          └─ 其它 ───────────────────────────────────> needs_human
```

`status` 取值：

| 状态 | 含义 | CLI 退出码 |
|---|---|---|
| `ok` | 已执行并得到结果 | 0 |
| `needs_clarification` | 问题超出 schema/指标范围，需要用户补充 | 0 |
| `refused` | 明确拒绝（预留） | 0 |
| `needs_human` | 自动修复失败，需要人工接管 | 1 |
| `failed` | 模型调用失败 | 1 |
| `max_steps` | 超过工具轮次上限 | 1 |

---

## 4. Schema linking 与提示词

两层召回：

1. **表级粗召回**：`Catalog.search_tables` 用 BM25 对表名、同义词、描述、字段名/描述/同义词打分；表名或同义词在问题中精确出现时加 2.5 分。中文按 unigram/bigram/trigram 索引，无需 jieba。
2. **字段级精读**：模型必须调用 `get_table_schema` 获取真实字段、类型、敏感/禁用标记、必填过滤和关系。

系统提示词只包含：

- 表名 + 一句话描述（不含全量 DDL，控制 token）；
- 业务口径（销售额、可用库存、在途、齐套率等）；
- 强制工作流；
- 硬性约束与最终 JSON 契约。

这样既避免把 185 张表全量塞进 prompt，也强制模型在生成前“看真实 schema”。

---

## 5. SQL Guard 分层

`SQLGuard.check(sql)` 返回 `GuardResult(valid, sql, tables, columns, issues)`，任何 error 都阻断执行。

| 层 | 检查 | 例子 |
|---|---|---|
| L1 输入 | 去 code fence、空值、20000 字符上限 | ```sql ...``` |
| L2 语句 | 仅 SELECT/WITH、单语句、只读关键字 | `DELETE`、`; DROP`、`PRAGMA` |
| L3 表 | 表白名单、禁用表、CTE 别名排除 | `internal_audit`、`WITH x AS ...` |
| L4 维度 | 必填过滤 | `dim_lookup.lookup_type='material_category'` |
| L5 列 | 禁止 `*`、`t.*`；列存在性；禁用/敏感列 | `SELECT *`、`o.custmer_name`、`customer_phone` |
| L6 风险 | 超过 5 张表给 warning（JOIN 扇出风险） | 多表聚合 |

实现细节：

- 先 `mask_sql` 清空字符串字面量和注释，避免 `status='update'` 或注释里的关键字误报。
- CTE 识别正则支持 `WITH a AS (...), b AS (...)`；CTE 名不当作物理表。
- `SELECT *` 检查会先剔除 `COUNT(*)` 等聚合星号。
- 列校验通过别名映射回真实表；未知列用 `difflib` 给相近字段建议。
- 表级裸列扫描兜底 `WHERE internal_note LIKE ...` 这类未限定列。

**边界**：guard 是正则 + 轻量扫描，不构建 AST。它覆盖高频事故模式，但复杂 SQL 方言可能漏检。生产必须在数据库侧使用只读账号、最小权限和审计。

---

## 6. 确定性修复

修复只在执行报错后触发，策略是“可回退、低风险、不扩权”。

| 规则 | 触发 | 行为 |
|---|---|---|
| `clean_syntax` | 任意 | 去尾分号、清理 `, FROM`、`, )` |
| `remove_invalid_projection` | SQLite `no such column` / SQL Server 42S22 / 中文“列名无效” | 仅从 SELECT 列表移除报错列，保留 WHERE/JOIN/GROUP BY |
| `wrap_group_by_expression` | SQL Server 8120 / “选择列表中的列…” | 对 SELECT 中的二元算术表达式包 `SUM()` |

保护条件：

- 至少保留一个投影列，否则放弃修复；
- 已在聚合函数内的表达式不重复包裹；
- 修复后仍需重新走 `validate_sql` 与 `execute_sql`；
- 最多两轮修复，未命中则 `needs_human`。

**为什么不让 LLM 自动重写？** 重写会引入新表/新字段和新幻觉，且不可回退。项目把修复限制为可证明安全的文本变换；需要语义重写时交回主循环重新生成。

---

## 7. 经验记忆

表结构：

```sql
experiences(
  id, question, normalized_question, sql, category, source,
  status,       -- candidate | verified
  quality,      -- candidate | hard
  schema_hash, hit_count, created_at, last_used_at,
  UNIQUE(normalized_question, schema_hash)
)
```

准入流程：

```text
execute_sql 成功
   └─ save_experience → candidate（永不召回）
          └─ 人工 experience verify
                 ├─ guard.check(sql) 必须通过
                 ├─ 真实执行必须成功
                 └─ → verified（可召回）
```

召回：

- 只在 `status='verified' AND schema_hash=当前hash` 中搜索；
- 精确归一化问题得 1.0；
- 其它：`0.55 * token Jaccard + 0.45 * SequenceMatcher`，子串匹配 +0.08；
- 默认阈值 0.3，top-k=3；命中更新 `hit_count/last_used_at`。

**设计动机**：LLM 自生成经验容易形成“错误答案 → 存库 → 下次召回 → 继续错误”的闭环。人工验证是刻意保留的瓶颈；schema hash 则解决表结构变更后的知识过期。

---

## 8. 评测设计

```text
case.question → agent.run()
                    │
case.gold_sql → run_readonly_query()
                    │
              rows_equal(agent.rows, gold.rows)
```

- **执行准确率**：结果集完全一致（列顺序无关、行顺序按 `order_sensitive`）。
- **Valid SQL rate**：可回答用例中，产出 SQL 且通过 guard 的比例。
- **Execution success rate**：状态为 `ok` 的比例。
- **Refusal accuracy**：拒答用例中，`needs_clarification/refused` 且无 SQL 的比例。
- **平均步数/延迟**：观测 Agent 效率。

数值归一化：`round(float, 4)`；字符串 `strip()`；布尔保持布尔。gold SQL 执行失败会标记为 `gold_sql_failed`，避免把评测集错误算成模型错误。

CI 中 `nl2sql-agent eval` 的退出码由是否有失败用例决定；报告写入 `reports/`（gitignore）。

---

## 9. 扩展点

| 需求 | 扩展方式 |
|---|---|
| 接其它模型 | 实现 `ChatModel.chat`，返回 `LLMResponse`；或复用 `OpenAICompatibleLLM` 改 base_url/model |
| 接真实数据库 | 替换 `run_readonly_query`，保持 `{success, columns, rows, error}` 契约；方言适配 guard 的 `TOP/LIMIT` 等 |
| 新业务域 | 替换 `data/demo_schema.json`，补充业务口径与 required_filters，重写评测集 |
| 更强 schema linking | 在 `Catalog.search_tables` 前加 embedding 召回 + 重排，保留 BM25 兜底 |
| 多轮对话 | 在 messages 中维护会话历史；对指代做 query rewrite，再进入本循环 |
| 权限按人过滤 | 在 catalog/guard 初始化时注入用户可见表/字段/行级策略 |
| 可观测性 | 将 `tool_trace` 接入 OpenTelemetry；记录 prompt hash、模型版本、token 用量 |

---

## 10. 性能与并发

- 整个 Agent 是同步实现，单进程内可并发调用（每次 `run` 独立 messages/trace；SQLite 连接每次新建）。
- 经验库使用独立 SQLite 文件，写操作短事务；高频写入场景建议换 PostgreSQL。
- Schema 每次从 JSON 加载（10 张表规模可忽略）；真实大 schema 建议启动时缓存并热更新。
- SQL 执行有 `max_rows` 与 `progress_handler` 超时；LLM HTTP 超时由 `NL2SQL_REQUEST_TIMEOUT_SEC` 控制（默认 120s）。
