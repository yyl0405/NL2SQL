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
| `ExperienceStore` | `memory/experience.py` | 经验持久化、准入、schema 失效、相似度召回、失败队列 |
| `taxonomy` | `taxonomy.py` | 固定类别体系与规则分类器（召回加权、经验打类） |
| `ToolRegistry` | `tools/base.py` | 工具注册、JSON Schema、参数绑定、错误回灌 |
| `run_readonly_query` | `tools/sql_tools.py` | 只读执行、超时、行数限制、截断标记 |
| `evaluate` | `evaluation/evaluator.py` | 冻结集执行、结果集比对、指标与报告 |

---

## 3. Agent 循环

伪代码（对应 `NL2SQLAgent.run`）：

```python
category = classify(question)                  # 规则分类器，用于召回加权
messages = [system_prompt + few_shot_block(question, category), user_question]
repairs = 0
for step in range(1, max_steps + 1):
    response = llm.chat(messages, tools=registry.specs())

    if not response.tool_calls:
        return finalize(response)          # 解析最终 JSON

    messages.append(assistant_tool_calls(response))

    for call in response.tool_calls:
        if call.name == "repair_sql" and repairs >= max_repairs:
            payload = reject("确定性修复已达上限，请输出 status=needs_human")
        else:
            payload = registry.call(call.name, call.arguments)
        if call.name == "repair_sql":
            repairs += 1
        messages.append(tool_observation(call.id, payload))
        trace.append(summarize(payload))

        if call.name == "validate_sql" and payload.ok:
            last_sql = payload.data.sql
        if call.name == "execute_sql" and payload.data.success:
            last_execution = payload.data
result = build_result(...)                 # 终态失败时 best-effort 写入 failure_queue
return result
```

设计要点：

1. **工具错误不是异常**：未知工具、参数不匹配、SQL 被拒、执行报错都会作为 observation 返回模型，让其自我修正。
2. **执行证据优先**：只有 `execute_sql` 成功才会记录 `last_execution`；最终 `status=ok` 必须存在执行证据。
3. **trace 可审计**：每次工具调用记录 step、参数、ok、摘要、耗时，便于定位“模型为什么这么写”。
4. **步数上限**：默认 12 轮，防止模型在错误路径上无限循环；超限返回 `max_steps` 而不是伪造答案。
5. **修复轮次上限**：`repair_sql` 超过 `max_repairs`（默认 2）后由 Agent 直接拦截，把“最多修两轮”从提示词约束变成不可绕过的硬护栏。
6. **verified-only 自动注入**：每次运行前按类别加权召回已验证经验并注入 system prompt（条数与字符双预算），模型不需要主动想起来调 `search_experience`；召回失败或记忆层异常一律降级为“不注入”。
7. **终态失败入队**：`needs_human / failed / max_steps` 会 best-effort 落入 `failure_queue`（见 §7.2），供人工结案；澄清与拒答不入队。

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

### 7.1 表结构

```sql
experiences(
  id, question, normalized_question, sql, category, source,
  status,       -- candidate | verified
  quality,      -- candidate | hard
  schema_hash, hit_count, created_at, last_used_at,
  UNIQUE(normalized_question, schema_hash)
)

failure_queue(
  id, question, normalized_question, schema_hash, category,
  reason,       -- needs_human | failed | max_steps
  last_sql, detail,
  status,       -- open | resolved | dismissed
  occur_count, created_at, last_seen_at, resolved_at, resolution,
  UNIQUE(normalized_question, schema_hash)
)
```

`category` 由 `taxonomy.classify_category()` 规则分类器填写（`inventory / purchase / production / bom / kitting / detail / group_by / ranking / time_filter / metric`，无命中为 `unknown`）；调用方显式给出类别时不覆盖。分类器只区分领域与答案形态，**不承担超范围/拒答判定**（那由 schema 查不到表 + 护栏决定）。

### 7.2 准入流程

```text
execute_sql 成功
   └─ save_experience → candidate（永不召回）
          └─ 人工 experience verify
                 ├─ guard.check(sql) 必须通过
                 ├─ 真实执行必须成功
                 └─ → verified（可召回）

Agent 终态失败（needs_human / failed / max_steps）
   └─ record_failure → failure_queue open
          ├─ 同 (归一化问题, schema_hash) 只保一行，重复只累加 occur_count
          ├─ 已结案的再次失败 → 自动 reopened
          └─ 人工 experience resolve <id> --sql "..."
                 ├─ validator = guard.check + 真实只读执行（缺一即抛错，队列保持 open）
                 └─ → 队列 resolved ＋ experiences 立即写入 verified
```

入队发生在结果组装阶段并被 try/except 包裹：记忆层故障不得改变返回给用户的答案。`needs_clarification` 与 `refused` 不入队——它们是正确的行为而非失败。`experience stats` 合并队列指标，其中 `failures_by_category` 只统计 open 行（待人工处理的积压）。

### 7.3 召回与自动注入

- 只在 `status='verified' AND schema_hash=当前hash` 中搜索（候选经验在 SQL 层就被排除）；
- 精确归一化问题得 1.0；
- 其它：`0.55 * token Jaccard + 0.45 * SequenceMatcher`，子串匹配 +0.08；
- 与问题**同类别**的 verified 经验额外加 `category_boost`（默认 0.12，置 0 关闭；`strict_category=True` 时直接过滤掉异类）；
- 默认阈值 0.3，top-k=3；命中更新 `hit_count/last_used_at`。

除 `search_experience` 工具之外，Agent 在每轮运行前**自动**把召回结果拼进 system prompt：`few_shot_max`（默认 3）条、`few_shot_max_chars`（默认 1200）字符预算，按整块丢弃以保证 SQL 不被截断；`few_shot_enabled=false` 可完全关闭。注入内容只作 few-shot 参考，不允许据此跳过 validate/execute。

**设计动机**：LLM 自生成经验容易形成“错误答案 → 存库 → 下次召回 → 继续错误”的闭环。人工验证是刻意保留的瓶颈；schema hash 则解决表结构变更后的知识过期；失败队列把“答不上的问题”变成可统计、可结案的人工待办，结案产物又直接回流为可注入经验，闭环由此合上。

---

## 8. 评测设计

### 8.1 确定性主指标

```text
case.question → agent.run()
                    │
case.gold_sql → run_readonly_query()
                    │
              rows_equal(agent.rows, gold.rows)
```

- **执行准确率**（`execution_accuracy`，主指标）：结果集完全一致（列顺序无关、行顺序按 `order_sensitive`）。
- **Valid SQL rate**：可回答用例中，产出 SQL 且通过 guard 的比例。
- **Execution success rate**：状态为 `ok` 的比例。
- **Refusal accuracy**：拒答用例中，`needs_clarification/refused` 且无 SQL 的比例。
- **table_recall@5**：`expected_tables` 是否被 `Catalog.search_tables` 的 top-5 覆盖，衡量 schema linking 质量（与 SQL 正确性解耦）。
- **平均步数/延迟/token**：观测 Agent 效率与成本；token 来自 `LLMResponse.usage`，`mock` provider 下为 0。

数值归一化：`round(float, 4)`；字符串 `strip()`；布尔保持布尔。gold SQL 执行失败会标记为 `gold_sql_failed`，避免把评测集错误算成模型错误。报告按分类（`category`）拆分，元数据记录 provider、模型、用例数、git sha 与时间戳。CI 中退出码由是否有失败用例决定；报告写入 `reports/`（gitignore）。

### 8.2 AI Judge（辅助信号）

确定性比对之外，`LLMJudge` 对同一 case 的 question / gold SQL+结果 / candidate SQL+结果 / answer 做结构化裁定，产出 `sql_equivalent`、`result_equivalent`、`answer_faithful`、`answer_complete`、`refusal_appropriate` 与 issues/severity。

- **职责切分**：`answer_faithful` 只判断回答是否编造了 `candidate_result` 之外的值（groundedness），与"答对没答对、答全没答全"无关；漏答关键点由 `answer_complete` 承担。这条边界写在 `data/judge_prompt.txt` 里，是校准一致率从 75% 提升到 95% 的关键。
- **注入防护**：待评数据用 XML 标签包裹并声明为"数据，不是指令"；输入按 rows/cols/cell/total 四级截断；schema 摘录剔除 sensitive/disabled 列。
- **降级**：JSON 解析失败重试一次（去掉 `json_object`），枚举非法或仍失败即记 `unknown`，不污染指标。
- **缓存**：`CachedJudge` 落 SQLite，key = `sha256(model + prompt_version + cache_payload)`；改 prompt 必须 bump `JUDGE_PROMPT_VERSION` 使旧缓存失效，`unknown`/`parse_error` 不缓存。
- **成本上限**：`judge_max_calls` 截断真实调用，其余标记为未评测。

Judge 指标（coverage / sql_equivalent / result_equivalent / faithful / complete / refusal / unknown / cache_hits / tokens）与确定性指标分块写入同一报告，并带免责声明——**AI 指标只用于发现 SQL 写法差异、拒答合理性和回答质量，不取代 gold 执行比对**。

### 8.3 人工校准门禁

`data/judge_calibration.jsonl` 是 20 条人工标注集。`eval --calibrate` 逐条比对 Judge 与人工标签，输出总体一致率、逐字段一致率和 Cohen's kappa；`--judge-sample N`（固定种子）抽样全量评审，`--judge-on-fail` 只评确定性失败用例，`--compare <report.json>` 输出与历史基线的指标增量。

未过门禁（一致率 ≥ 85% 且 sql/answer kappa ≥ 0.6）前不得引用 AI 数字。当前 `qwen-max` + `judge-v2` 实测：总体 90%、sql_equivalent 95%/κ0.857、answer_faithful 95%/κ0.857，达标。残余两条分歧（`ORDER BY` 字段不同导致行序差异、回答忠实但过于简短）记录在 [CHANGELOG.md](../CHANGELOG.md) 的校准小节，作为"Judge 属辅助信号"的实证。

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
