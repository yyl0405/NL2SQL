# 架构设计

## 1. 目标与非目标

**目标**

- 在制造 ERP schema 上，把自然语言问题可靠地转换为可执行 SQL。
- 每一步都可观测：模型看到了什么、调用了什么工具、SQL 是否通过护栏、执行是否成功。
- 失败可恢复：确定性修复优先，无法修复时明确升级，不编造结果。
- 可离线复现、可自动评测、可扩展到在线 LLM 与真实数据库。

**非目标**

- 不做通用 BI 自然语言界面（图表、钻取、权限 UI）。
- 不替代数据库权限体系；应用层 guard 只是第一道闸门。
- 不追求“一次生成即正确”，而是把正确性建立在工具反馈与执行验证上。

---

## 2. 组件图

```text
┌──────────────────────────── NL2SQLAgent ────────────────────────────┐
│  rewrite(question, history) → 独立问题（歧义则直接澄清）               │
│  align(独立问题) → 唯一命中的取值追加到问法后（歧义则直接澄清）        │
│  system prompt + user question                                      │
│  ┌────────────── loop (max_steps) ──────────────┐                   │
│  │ chat(messages, tool_specs)                    │                   │
│  │   ├─ no tool_calls → finalize(JSON)           │                   │
│  │   └─ tool_calls → ToolRegistry.call(...)      │                   │
│  │        observation 回灌 messages               │                   │
│  └───────────────────────────────────────────────┘                   │
│  _verify(result) → 可选：第二通道独立生成 + 真实执行 + 结果集比对      │
└───────────────┬─────────────────────────────────────────────────────┘
                │
    ┌───────────┼──────────────┬──────────────┬───────────────┐
    ▼           ▼              ▼              ▼               ▼
 Catalog     SQLGuard      SQLRepairer    ExperienceStore   Read-only DB
 (schema)    (permission)  (repair)       (memory)          (SQLite ro / pyodbc)
```

| 组件 | 文件 | 职责 |
|---|---|---|
| `NL2SQLAgent` | `agent.py` | 改写入口、工具调用步、trace、最终状态校验、结果组装 |
| `Conversation` / `Slots` | `conversation.py` | 会话轮次与查询槽位状态（供改写层继承与替换） |
| `RuleBasedRewriter` / `LLMQuestionRewriter` | `rewriter.py` | 追问 → 独立问题；同一 `Rewrite` 契约，模型通道违约即退回规则 |
| `ValueAligner` / `ValueDictionary` | `value_align.py` | 生成前把业务简称/口语值对齐到库里真实存储的取值：唯一命中才**追加** `[值对齐] 表.列=值`（不改用户原话）、多命中转澄清带候选项、未覆盖问法逐字节穿透、一次最多 8 个取值；词典是数据（`data/value_dictionary.json`），真实部署指向仓库外文件 |
| `ClarificationPolicy` / `MetricGlossary` | `clarify.py` | 澄清触发条件与**分层**：五个触发码分成"只补充"（`metric_ambiguity` / `time_range_missing`，照常回答并交回更窄的完整问句）与"可以挡"（`ambiguous_entity` / `permission_denied` / `schema_gap`）；名词表把歧义业务名词映射成候选口径问句；`enrich` 保证每条澄清都带码与可回问内容；`resolve_clarification_reply` 把序号/片段还原成完整问句 |
| `SecondOpinionGenerator` | `voting.py` | 复核通道：规则模板 / 在线模型无工具单次调用；`should_verify` 触发策略；不一致不选赢家 |
| `rows_equal` / `describe_diff` | `result_compare.py` | 结果集比对的唯一实现，复核投票与评测打分共用同一口径 |
| `Catalog` | `catalog.py` | schema 加载、schema hash、按规模选 linking 策略、关系/业务口径、向量与重排通道的装配（默认关闭） |
| `LinkIndex` / `Strategy` | `linking.py` | BM25 文档构造（字段权重、长度归一化、注释同义词挖掘、覆盖重排）、`resolve_strategy`、`link-check` 的测量单元 |
| `VectorIndex` / `FusedLinkIndex` | `linking.py` | 向量余弦召回、`reciprocal_rank_fusion`（RRF）双通道融合；向量任一步失败即整体降级回 BM25 并记录原因 |
| `EmbeddingProvider` / `SchemaIndex` | `embeddings.py` | `hash`（离线确定性替身）与 `openai`（兼容端点，只发表结构文本）；向量按 `schema_hash + docs_hash + provider` 缓存失效 |
| `LLMReranker` | `rerank.py` | 可选重排：`{"tables": [...]}` 契约、候选外表名丢弃、任何失败返回原序 |
| `introspect` | `introspect.py` | 从 SQL Server 系统视图只读生成 catalog：外键 + 注释关联提示 → `relationships`，`前置条件` → 待核对的 `filter_suggestions`，敏感/垃圾表标记；`business_rules` 不由库内推导 |
| `suggest_schema` / `schema-suggest` | `schema_suggest.py` | 业务概念 → 候选表的**草稿**通道：链接打分选候选、可选连库取 ≤5 行非敏感样本作证据、模型只能在候选里选（编造的表/列在归一化时丢弃、置信度夹回 `[0,1]`、记 `schema_hash`），一律 `review_status=pending` 落 `var/`；单概念的失败落 `observations` 而不是假装有一条映射 |
| `apply_overlay` / `load_overlay` | `schema_overlay.py` | 人工批准后的映射叠加层（表别名 / 列别名 / 业务口径 / `approved[]` 审计），`runtime` 在 `Catalog.from_path` 之后合并；未知表列忽略、`schema_hash()` 不含别名。**未批准的候选没有任何路径进入 Agent** |
| `SQLGuard` | `guard.py` | 只读、表白名单、必填过滤、列存在性、`SELECT *`、扇出 warning、可选权限与 AST 层的装配点 |
| `AstAnalyzer` / `AstReport` | `ast_guard.py` | 可选 `sqlglot` 第二意见：只报结构（语句数、顶层类型、写节点、真实表、投影星号、危险函数、元数据访问），依赖缺失或解析失败一律 error |
| `Principal` / `RowFilter` | `principal.py` | 行级/列级权限：给 Agent 的过滤视图与给护栏的全目录判权，两种机制同一策略 |
| `analyze_joins` | `fanout.py` | 1:N 连接 + 聚合只落「一」侧的静态 warning，永不阻断执行 |
| `guard_suite` / `guard-check` | `guard_suite.py` | 无模型、无连接的护栏基准：对抗 / 权限 / 扇出 / gold 误杀，分层归因并锁定回归数字 |
| `dialect_audit` / `dialect-check` | `dialect_audit.py` | 无模型的方言基准：本方言 gold 的解析与护栏接受度、错方言形状的两层拦截，以及转译到唯一可执行后端后的**结果集等价**（静默偏离与响亮失败分开数） |
| `value_audit` / `alias-check` | `value_audit.py` | 值对齐的**前置条件**与效果测量：词典↔目录↔库内取值三项吻合、`dim_lookup` 枚举交叉核对、别名语料期望、冻结集触达（gold 被改动或被转成澄清即红），以及对齐开/关的配对 A/B——两臂各用**独立经验库**，否则一臂的成功会变成另一臂的 few-shot，测出来的差是泄漏 |
| `clarify_audit` / `clarify-check` | `clarify_audit.py` | 澄清策略的测量台：五码分层是否守住（该问的问、该闭嘴的闭嘴）、每条澄清是否**可执行**（裸反问计数）、每条选项**回问**能否出答案、序号能否还原成完整问句、冻结集 footprint（政策层命中几条、`options` 挡几条、`ask` 挡几条并列出 id），以及 `off`/`options` 配对 A/B（同样两臂独立经验库）。语料与期望取值写死在打包演示目录上，**配非演示 schema 时 rc 2 拒给判定** |
| `SQLRepairer` | `repair.py` | 基于数据库错误的确定性修复 |
| `ExperienceStore` | `memory/experience.py` | 经验持久化、准入、schema 失效、相似度召回、失败队列、`status`（可见性）与 `quality`（排序先验）两根轴、反例准入闸门与 stale 批量退休 |
| `ExperienceRecall` | `recall.py` | 召回通道与排序：`lexical` / `vector` / `rrf`（k=60）、gold 加分、MMR 多样性（λ=0 逐位等于旧 top-k）、向量缓存与降级元数据；`recall_from_settings` 在没有向量 provider 时**配置期报错**而不是静默降级 |
| `experience_audit` / `experience-check` | `experience_audit.py` | 经验召回的离线测量台：冻结集 32 对源/改写问法 × 五条臂（lexical / diversity / gold-half / vector-hash / rrf-hash），hit@1/3/5、MRR、gold 分组、MMR 位移数与降级计数；断言 lexical 臂逐条等于 `store.search()`，**不调生成模型**，hash 向量只是通路替身 |
| `taxonomy` | `taxonomy.py` | 固定类别体系与规则分类器（召回加权、经验打类） |
| `ToolRegistry` | `tools/base.py` | 工具注册、JSON Schema、参数绑定、错误回灌 |
| `run_readonly_query` | `tools/sql_tools.py` | 只读执行、超时、行数限制、截断标记 |
| `SqliteTarget` / `MssqlTarget` / `PostgresTarget` | `tools/db.py` | 后端抽象：连接、`describe()`（无凭证）、`dialect_sql()` 分页与类型差异、统一返回契约、`coerce_target()`（裸路径仍指 SQLite） |
| `Dialect` / `REGISTRY` | `dialects.py` | 方言的唯一出处：`label`（prompt 用）/ `channel_label`（第二意见用）/ 引号 / 占位符 / 文本类型 / 行限制写法 / 该方言禁止的说法 / 分页 / 会话设置语句 / 只读语句，别名归一在 `normalize()`，**认不出的方言不猜**（`unknown()` 回显原标签、护栏不设规则） |
| `ConnectionPool` / `configure_pool` | `tools/db.py` | 默认关闭的固定容量池（`NL2SQL_DB_POOL_SIZE`）：归还不 commit 而是 rollback、坏句柄丢弃、池满报 `pool_unavailable`、`pool_stats()` 进 `/api/health` |
| `evaluate` | `evaluation/evaluator.py` | 冻结集执行、结果集比对、指标与报告 |
| `create_app` | `web/app.py` | FastAPI 薄壳：把 `agent.run` / `evaluate` 的产物原样序列化给浏览器；只有会话问答、跑分与反馈审核三处有写路径，审核复用 CLI 的护栏 + 真实执行 validator，不复算指标、不绕过护栏 |
| `observability` | `observability.py` | 一轮一问的 `trace_id`（只在请求边界铸造、库内只继承）、四类 JSON Lines 事件（`api` / `turn` / `tool` / `db`，`db` 行带 `connection` / `pooled`）、按键路径的凭证与结果集剔除、`NL2SQL_LOG_INCLUDE_TEXT` 决定业务文本是否落盘（默认只留 sha256 指纹）、`read_events` / `percentile` / `summarize` 把日志折回指标 |
| `metrics` | `cli.py` | 事件日志的读数台：P50/P95 延迟、QPS、人工升级率、澄清率、经验命中率、repair 成功率、工具/DB 错误率、**执行层延迟（只算 `db` 事件）**、**物理连接数与池化调用数**、token 四类拆分、结果缓存命中率与双通道一致率；**没有日志路径就退出码 2 拒给判定**，永不把"没测"印成 0 |
| `ResultCache` / `cache_key` | `cache.py` | 结果缓存：键 = 归一化问题 + schema hash + principal + `allow_sensitive` + 策略指纹 + 会话前缀哈希；只缓存 `ok` 且已执行的轮次；TTL + 有界 LRU，默认 `ttl=0` 完全关闭；命中返回深拷贝（`cached=true`、`steps=0`、新 `trace_id`） |
| `cache_audit` / `cache-check` | `cache_audit.py` | 三个"不调模型"的缓存测量：结果缓存冷/热/归一化命中与 LRU 统计、embedding 缓存第二次是否零调用、system prompt 稳定前缀比例与 few-shot 是否在尾部 |
| `costs` / `cost` | `costs.py` / `cli.py` | token 四类拆分与按业务域/用户折算费用；**不内置任何供应商价格**，input/output 两个单价必须同时由部署给出，未配置时 `estimated_cost=null` 并明确报"费用未配置" |
| `bucket` / `in_gray` | `gray.py` | 灰度分桶：`sha256(salt:key)` 前 8 位十六进制 mod 10000；`0`/`100` 精确；同一 key 在同一进程、不同进程、每一轮都落同一臂；`share()` 回传桶值与阈值便于审计 |
| `create_release` / `rollback_release` / `release` CLI | `release.py` / `cli.py` | 发布快照与回滚：`releases/<release_id>/{manifest.json,schema.json,experience.db}`，三部分各自 sha256；经验库用 SQLite 在线备份 API 复制；回滚覆盖前另存 `var/release_backups/`，prompt 只比对版本/指纹、不能从快照恢复正文（需 checkout 代码，CLI rc 3） |

---

## 3. Agent 循环

伪代码（对应 `NL2SQLAgent.run`；`run = _verify(_run_loop(...))`，下面先是循环体）：

```python
category = classify(question)                  # 规则分类器，用于召回加权
messages = [system_prompt_with_few_shot_at_tail(question, category), user_question]  # 稳定前缀在前，few-shot 在 JSON 契约之后
# 追问轮（history 里有成功执行的轮次）额外把那条 SQL 附在 system 块尾部；
# user 消息始终是逐字的独立问题，所以 mock、评测与缓存口径都不变。
repairs = 0
for step in range(1, max_steps + 1):
    response = llm.chat(messages, tools=registry.specs())

    if not response.tool_calls:
        # 在线模型（非 mock）的两次单次纠偏：
        # 1) 未查任何 schema 就要澄清/转人工 → 先强制查表；
        # 2) 已查 schema、仍未执行就要澄清/转人工 → 先尝试执行。
        if should_nudge_before_schema(): nudge("先查 schema"); continue
        if should_nudge_before_execute(): nudge("先执行 SQL"); continue
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
    # 取值打转纠偏（仅非 mock，一次）：连续 max_value_probes 次 search_values
    # 且从未执行任何 SQL → 注入一次"停止试值，直接写 SQL 并执行"
    if should_nudge_after_probing(): nudge("停止试值，去执行"); continue
if last_execution:
    return ok(last_execution, assumption="达到最大工具调用轮次")  # 不丢弃已执行结果
result = build_result(...)                 # 终态失败时 best-effort 写入 failure_queue
return result


def _verify(result):                       # 双通道复核，开关关闭时原样返回
    if second_opinion is None: return result
    if not should_verify(settings, status=result.status, confidence=result.confidence,
                         category=classify(question).category, executed=bool(result.columns)):
        return result
    opinion = second_opinion.generate(question, tables=guard.check(result.sql).tables)
    if not opinion.sql:                       # 未生成：只标注“未经交叉验证”
        return result.record(role="second", status="not_generated", agreement=None)
    execution = tools.call("execute_sql", {"sql": opinion.sql})   # 同一护栏、同一行限与超时
    if not execution.ok:
        return result.record(status="guard_failed"|"execution_failed", agreement=None)
    if rows_equal(result.rows, execution.rows):        # 与评测器共用同一比对实现
        return result.record(agreement=True, confidence=min(0.95, confidence + boost))
    return result.record(agreement=False, status="needs_human",   # 不默认取舍
                         error="dual_channel_disagreement") -> failure_queue
```

设计要点：

1. **工具错误不是异常**：未知工具、参数不匹配、SQL 被拒、执行报错都会作为 observation 返回模型，让其自我修正。
2. **执行证据优先**：只有 `execute_sql` 成功才会记录 `last_execution`；最终 `status=ok` 必须存在执行证据。
3. **trace 可审计**：每次工具调用记录 step、参数、ok、摘要、耗时，便于定位“模型为什么这么写”。
4. **步数上限**：默认 12 轮，防止模型在错误路径上无限循环；已成功执行过查询时返回最后一次执行结果（`ok` + `assumptions` 注明达到轮次上限），否则返回 `max_steps` 而不是伪造答案。
5. **修复轮次上限**：`repair_sql` 超过 `max_repairs`（默认 2）后由 Agent 直接拦截，把“最多修两轮”从提示词约束变成不可绕过的硬护栏。
6. **verified-only 自动注入**：每次运行前按类别加权召回已验证经验并注入 system prompt（条数与字符双预算），模型不需要主动想起来调 `search_experience`；召回失败或记忆层异常一律降级为“不注入”。
7. **终态失败入队**：`needs_human / failed / max_steps` 会 best-effort 落入 `failure_queue`（见 §7.2），供人工结案；澄清与拒答不入队。
8. **在线模型纠偏（仅非 mock）**：模型可能在未查 schema 时直接澄清、查完 schema 却停在不执行、连续多次 `search_values` 试值却不写 SQL、或撞到轮次上限。Agent 对前三种各注入一次单次 nudge（先查表 / 先执行 / 停止试值去执行），不改变 mock 与离线评测路径；纠偏次数有上限，不会无限续命。
10. **双通道复核（可选，默认关闭）**：`run()` 的最后一环 `_verify()` 把高风险答案交给第二条独立生成通道，两条通道都真实执行后比对结果集；一致只上调置信度，不一致转 `needs_human` 并入队，判定不了的情况**不改动主通道结论**（见下方小节）。开关关闭时这一环直接原样返回，行为与基线逐字节一致。
11. **未执行不冒充结果**：`status != ok` 且没有 `last_execution` 时，未执行的 SQL 不通过 `AgentResult.sql` 暴露；回答里若出现“数字 + 单位”（如台/元/条）会被替换为“未执行查询，没有可验证的数字结果”。`search_values` 对未知字段直接返回可用字段列表，空结果附加“不要继续对同一列试值”的提示，减少模型在枚举值上打转。

### 最终状态机

```text
LLM final JSON
   │
   ├─ status=ok 且 last_execution 存在 ──────────────> ok
   ├─ status=ok 但无执行证据 ────────────────────────> needs_clarification（置信度 ≤ 0.4）
   ├─ status∈{needs_clarification, refused, needs_human} ─> 原样返回（隐藏未执行 SQL；数字+单位替换）
   ├─ 轮次耗尽且 last_execution 存在 ────────────────> ok（assumptions 注明达到轮次上限）
   ├─ 轮次耗尽且无执行证据 ──────────────────────────> max_steps
   └─ status 非法/缺失
          ├─ 有执行证据 ─────────────────────────────> ok
          ├─ 文本含“请补充/无法/不包含/不支持” ───────> needs_clarification
          └─ 其它 ───────────────────────────────────> needs_human

   开启复核时，上面的 ok 还会再走一跳（needs_clarification / refused / max_steps 等不参与）：
   ok ──> 第二通道未生成 / 被护栏拒绝 / 执行失败 ─────> ok（agreement=null，附「未经交叉验证」假设）
   ok ──> 结果集一致 ────────────────────────────────> ok（置信度 +boost，封顶 0.95）
   ok ──> 结果集不一致 ──────────────────────────────> needs_human（不默认取舍，写入 failure_queue）
```

`status` 取值：

| 状态 | 含义 | CLI 退出码 |
|---|---|---|
| `ok` | 已执行并得到结果 | 0 |
| `needs_clarification` | 问题超出 schema/指标范围，需要用户补充 | 0 |
| `refused` | 明确拒绝（预留） | 0 |
| `needs_human` | 自动修复失败，需要人工接管 | 1 |
| `failed` | 模型调用失败 | 1 |
| `max_steps` | 超过工具轮次上限且没有成功执行结果 | 1 |

### 追问改写层（多轮会话）

多轮能力放在 Agent 循环**之前**：`agent.run(question, history=...)` 先把追问改写成一个独立问题，改写结果是一次问数里**唯一**的 `user` 消息。工具循环、SQL 护栏、失败队列和评测器因此完全不需要知道会话存在过，单轮行为逐字节不变（无历史 → 不改写、不澄清短路、prompt 版本不变）。

```text
history(Conversation) ──┐
                        ├─> Rewrite{question, slots, ambiguous, options, missing}
最新问题 ───────────────┘        │
                                 ├─ ambiguous ─> needs_clarification（不进模型）
                                 └─ question ──> Agent 循环（与单轮完全同一条路径）
```

**状态模型**（`conversation.py`）：10 个槽位 `region / customer / material / order_id / month / date / limit / dimension / metric / direction`。每轮保存*合并后*的槽位，所以“最近一轮已回答的问句 + 它的槽位”就足以解析下一轮追问，历史长度不影响正确性。未回答的轮次（澄清、拒答、失败）不改变上下文。

**两条改写通道**（`rewriter.py`，共用同一个 `Rewrite` 契约）：

| 通道 | 做法 | 失败时 |
|---|---|---|
| `RuleBasedRewriter` | 槽位继承 + 在前一轮**原始问句**上做短语替换（时间/地区/客户/物料/订单号/Top N，含「那去年的呢？」这类**相对时间词**的原文替换，槽位模型读不出它们）；改指标或改维度时用一份**已验证问法模板**重新渲染，但**模板只有在能逐字复现上一句时才允许参与**（`_template_round_trips`）——复现不出来说明这段对话不在这套词表里，此时只做原文替换或转澄清 | 无法落地就转澄清，绝不猜测，也绝不把话题换成模板里的问题 |
| `LLMQuestionRewriter` | 让模型按 JSON 契约输出独立问题（`rewrite-v1`） | 调用异常记 `llm-error`、违约记 `llm-invalid`，两种情况都退回规则结果 |

两条通道都只产出**已经在单轮评测集里出现过 shape** 的问句：多轮准确率因此衡量的是指代消解质量，而不是新的 SQL 生成能力。规则通道的关键约束：只在本轮确实给出了新值时才替换（替换不上就放弃这条路，交模板或澄清）；模板只覆盖已验证的指标组合，未知组合转澄清而不是硬套。

模型通道的契约校验：长度上限、禁止换行/代码块、禁止 `SELECT|WITH|FROM|WHERE`、必须与本轮或最近两轮问句共享锚点 token，并且**不允许出现历史中从未出现过的区域/客户/物料/订单号**（防实体幻觉）。歧义时模型可以给候选问法，但 `ambiguous=true` 而 `options` 为空同样判违约。

**澄清**（`Clarification`）：指代对象不唯一、口径不唯一、或改问某个指标缺少必要槽位（如齐套率缺订单号）时返回 `reason + 至多 3 条可直接追问的候选问法 + missing_slots`；`chat` 模式可以直接输入序号选中候选继续问。

### 双通道复核与执行投票（可选）

单个通道无法自证正确：模型可以写出语法通过、护栏通过、能返回数字却口径错误的 SQL。复核把“另一个视角”变成一次**真实的独立计算**，而不是另一次语言判断——`agent.run()` 的最后一环 `_verify()`（`voting.py` + `result_compare.py`）。

```text
_loop 结果（status=ok 且已真实执行）
   │  should_verify?  开关开 ∧ 置信度 < 阈值 ∨ 类别 ∈ 高危集合
   ├─ 否 ─────────────────────────────────────> 原样返回（无 channels 字段）
   └─ 是 ─> 第二通道 generate(question, 主 SQL 用到的表)
              │
              ├─ 无 SQL ─────────────────────> ok + agreement=not_generated（不改结论）
              └─ SQL ─> execute_sql（同一护栏、同一只读连接、同一行限与超时）
                          ├─ 失败/被拒 ─────> ok + agreement=guard_failed|execution_failed（不改结论）
                          ├─ rows_equal ────> ok，置信度 +boost（封顶 0.95）
                          └─ 不等 ──────────> needs_human + 入 failure_queue，置信度压到 ≤0.25
```

**触发策略**（`should_verify`）：投票会把一次问答变成两次模型调用与两次真实查询，所以只在“错一次代价高”的地方发生——置信度低于 `dual_channel_confidence_threshold`（默认 0.75），或类别落在 `dual_channel_categories`（默认 `bom,kitting`，即递归 BOM 展开与 JOIN 扇出/齐套这类最容易算错口径的聚合）。拒答、澄清、未执行的轮次一律不投票：投票不能把一个没有数据的答案变成有数据。

**两条通道的独立性**（同一个 `SecondOpinionGenerator` 契约）：

| 通道 | 做法 | 独立性来自哪里 |
|---|---|---|
| `RuleBasedSecondOpinion`（离线） | 规则模板直接按计划生成 SQL，跳过工具循环 | 主通道是 tool-calling 循环，它只看问题，走完全不同的代码路径 |
| `LLMSecondOpinion`（在线模型） | **一次不带 tools 的调用**，提示词只给问题 + 表结构摘录（≤6 张表，剔除敏感/禁用列）+ 基准日期，按 `{"sql": ...}` 契约返回 | 刻意不复用主通道已经检索到的经验与值对齐结果；若也跑一轮工具循环，一致就只证明“同一个循环很稳定” |


**判定与边界**：结果集比对复用评测器同一份 `result_compare.rows_equal`（浮点 4 位、字符串去空格、默认行序无关），因此“复核一致”与“评测通过”是同一个口径。比对的是**受 `max_rows` 截断后**的结果集——两条通道都被截断时，投票判断的正是用户实际会看到的行。不一致时不选赢家：`error="dual_channel_disagreement"`，回答里给出两条通道各自的行数与第一个差异行，并把这条问题写入失败队列等人工结案。生成器抛异常也被吞成一次未生成，一条坏掉的复核不会毁掉能用的答案。`channels` 只记录 SQL、行数与判定，不复制结果集数据；投票的 token 与耗时计入 `tokens`/`elapsed_ms`，但 `avg_steps` 不变（投票在工具循环之外）。

### 值对齐层：简称在进入模型之前换成库里真实存的值

用户说"杭智"、"东区"、"待发货"，库里存的是 `杭州智造科技有限公司`、`华东`、`pending`。这一层不做就只能有两种结果：模型猜一个 LIKE 模式（`LIKE '%杭智%'` 查出 0 行，然后把 0 行当事实回答），或者工具循环多花几步去 `search_values` 撞出来。`ValueAligner` 把这件事变成一次确定性的字面匹配，位置在 `run()` 里**改写之后、类别判定与提示构造之前**（`agent.py:172`）——先消解指代再对齐，因为"那华南呢"里的"华南"是多轮继承来的，不是用户这一轮写出来的。

```text
rewrite.question ──> align() ──> 四种结局之一
                       │
   ├─ 唯一命中   → effective = 原问法 + " [值对齐] 表.列=值"，逐条记 resolved
   ├─ 已是全称   → verified（不再注入第二遍）
   ├─ 多取值命中 → ambiguous → 短路为 needs_clarification（候选项带列名，≤3 条）
   └─ 没命中     → effective 与原问法逐字节相同
```

**四条设计约束都由失败模式倒推**：① **不改用户原话**——对齐结果是**追加**的一段 `[值对齐] 表.列=值`，原句逐字保留，所以"这句话里哪部分是用户说的、哪部分是系统加的"在日志与报告里永远分得开；② **不猜**——歧义永远压过注入，一个提法对上多个存储取值就反问，绝不选"最可能的那个客户"；③ **沉默即空操作**——词典没覆盖的问法必须逐字节穿透，这条被 `tests/test_value_align.py` 用字符串相等钉住，因为它保证"上线一个词典"不可能改变任何已有正确路径；④ **注入块有预算**——一次最多 8 个取值（`MAX_ALIGNED_VALUES`），超出的记进 `dropped` 并透出，对齐块不该膨胀成第二个 prompt。

**词典是数据，装载是严格的**。`data/value_dictionary.json` 只描述"提法 → (列, 存储值)"，真实客户/供应商简称属于业务数据、不进仓库，部署用 `NL2SQL_VALUE_DICTIONARY` 指向仓库外文件。`ValueDictionary.load()` 对结构错误**直接抛**（缺列、缺取值、别名与提法冲突…），而不是记一条 warning：一个静默降级的词典会带着错的取值继续改写 prompt，那比启动失败严重得多。同一取值分布在两列（`sales_order.status` 与 `purchase_order.status` 都有 `pending`）**不算碰撞**——取值是唯一的，不确定的只是列，重复注入一对不带来任何信息。

**碰撞消解只接受用户自己给的证据**。`KIND_QUALIFIERS`（`物料/原料→material`、`客户→customer`、`供应商→supplier`、`地区/区域→region`）让"物料轴承"落在物料列而不是变成反问：用户已经说出了去哪一列找，这不是系统在替他猜。判定依据记在 `Resolved.qualified_by`，所以每条被消解的碰撞都可回溯。**这条规则是被测量逼出来的**：`轴承` 裸提法最初把 3 条冻结集 gold 用例转成了澄清，`alias-check` 的"冻结集转澄清"计数从此钉成不变量——任何让 gold 用例变成反问的词典改动都会把这一步变红。

**与双通道复核的关系是刻意的不对称**：主通道消费对齐结果，第二通道（`LLMSecondOpinion`）只看问题与表结构摘录、不复用值对齐与经验注入。若复核通道也读同一份对齐结果，"两条 SQL 一致"就只证明同一个假设自洽，不再是一次独立验证。`NL2SQL_VALUE_ALIGNMENT=off` 与关双通道同理，是**逐字节**逃生门：`tests/test_value_align.py` 与 `tests/test_agent.py` 各自断言关掉之后 `value_alignment is None`、`effective_question == question`、工具轨迹与基线全等。

**这一层的仪器是 `alias-check`**（`value_audit.py`，默认 `provider=mock`、不调外部模型）。它分两类输出：**前置条件**（词典声明的列在目录里吗、库内还留着这个取值吗、状态枚举与 `dim_lookup` 的编码和中文名对得上吗、冻结集被改动或被转成澄清了吗）——不成立就 rc 2 拒给判定或 rc 1 报不变量破坏；**效果测量**（对齐开/关的配对 A/B 逐条按 gold 结果集投票）——**只报告、不进退出码**，因为"开启后多过几条"是证据不是判据，把它当门禁只会诱导人加词条。两个实现细节是测量成立的前提：**两臂各用独立经验库**（`var/alias_ab/{off,on}.db`），否则关闭臂的成功会成为开启臂的 few-shot 示例，测出来的差是泄漏；**澄清不参与 EX 投票**（它没有结果集），两条歧义用例记 `unmeasured` 并单独统计"有没有反问、有没有带候选项"。

### 澄清策略层：什么时候该问、问完交回什么

规划里给这一层列了五个触发条件（指标歧义 / 时间范围缺失 / 实体多义 / 权限不足 / schema 无此指标）。**它们不属于同一层，这个分法是被冻结集测出来的，不是先验的**：把五码一律做成"挡住并反问"，`clarify-check` 在冻结集上量到的 footprint 是 43/123 条（指标歧义 10、时间范围缺失 39、两者都占 6，静默 80）——这 43 条都是**能答的** gold 用例，它们只是没写口径，挡下来等于把评测集变成对话。而实体多义挡住 0 条（P1-2 实测 `frozen.ambiguous == []`）。所以：

| 层 | 码 | 行为 | 判据 |
|---|---|---|---|
| `ASSUME_CODES` 只补充 | `metric_ambiguity`、`time_range_missing` | **照常回答** + 把每种口径渲染成完整问句交回（`AgentResult.alternatives`） | 冻结集 43 条命中，挡住即丢答案 |
| `ASK_CODES` 可以挡 | `ambiguous_entity`、`permission_denied`、`schema_gap` | `needs_clarification` | 答了就是答错：猜实体、越权、目录里没这个指标 |

模式 `off` / `options`（默认）/ `ask`（`NL2SQL_CLARIFY`）：`options` 只补充、冻结集挡 **0/123**；`ask` 连政策层也挡，实测挡 **4/123**（`d_inventory_m005`、`i_locked_share`、`t_due_july`、`t_last30_days`）——`ask` 存在是为了让"挡路的代价"是可量的一个数字，而不是一个开关的名字。

**位置在 `run()` 里对齐之后、类别判定之前**（`agent.py:199`）。触发判定用的是 `alignment.question`（用户原话）而不是 `alignment.effective`：值对齐追加的 `[值对齐] 表.列=值` 里可能带月份之类的字面量，用它判定会把"系统注入的取值"读成"用户选的口径"，从而漏掉 `time_range_missing`。

**三条设计约束**：① **选项是完整问句**，周期字面量写在句子内部（"上月（2026年6月）销售额是多少？"），所以一次点选就等于重新问了一遍——前端不需要维护"正在澄清哪个槽位"的状态，回问路径零状态；代价是选项长。② **回答与选项分字段走**：`answer` 保持干净，窄问法只在 `alternatives` 里。Judge 与评测读的都是 `answer`，把"你可以直接这样问：…"粘在数字后面等于把一条可校验的回答变成不可校验的文本。③ **没有可选项就不叫澄清**：`Clarification.actionable` 要求 `options` 或 `missing_slots` 至少有一个非空；模型的「请补充必要的条件后重新提问。」在这层被 `enrich` 兜住——补不上选项时至少带上缺哪个槽位，`bare` 计数在仪器里直接判红（实测 22 条语料 13 条澄清，`bare == 0`）。

**序号即回答**：`resolve_clarification_reply` 把「2」/「第二个」/片段还原成完整问句，`Rewrite.source="clarify_reply"`，之后走的仍是同一条单轮链路（工具循环、护栏、评测都不知道"澄清"这回事）。`CODE_ENTITY` 的选项特例：它命名的是**存储取值**而不是问句，所以还原时**替换掉**那个歧义提法而不是追加——原句留着会让对齐层下一轮再报同一条碰撞，第二轮永远结束不了。

**在线模型的纠偏与澄清有冲突**：模型可能在没查 schema 时就要澄清。`_CLARIFY_NUDGE` 先强制一次 `search_tables` + `get_table_schema`，查完仍缺具体过滤值才允许 `needs_clarification`（只作用于非 mock，`mock` 与离线评测路径逐字节不变）。判定 `status` 落不回白名单时，回答里出现"请补充/无法/不包含/不支持"会被归一成 `needs_clarification` 而不是 `needs_human`。**澄清候选不是无条件追加的**：`enrich(keep_open=True)` 只在这个缺口无法被策略归因（code 仍是 `model_clarification`）时生效——此时模型已经说清了缺哪个值/哪列/哪个映射，再贴一份打包名词表里的演示问法会盖掉用户真正要回答的问题；能归因的口径歧义、时间、实体、schema 缺口照旧带候选。

**仪器是 `clarify-check`**（`clarify_audit.py`，`provider=mock`）。四类输出，各自决定不同东西：**语料不变量**（22 条：trigger 8 / quiet 5 / enrich 3 / followup 6）——该问的没问、该静默的问了、澄清没有可选项、选项回问答不出、gold 用例被改动，任一破坏 → rc 1；**冻结集 footprint**——默认模式下挡住任何一条 gold 用例即 rc 1，`ask` 模式的 4 条只报告；**observations**——`option_period_inert`（三个周期回问出同一种结果：分组问法对周期不敏感）与 `option_period_empty`（演示种子没有 7 月数据）都**永不判红**，因为它们分别是问法形状与数据事实，不是策略缺陷，但不印出来就会有人把"20/20 出答案"读成"每个周期都有数"；**配对 A/B**（`off` vs `options`，两臂各用 `var/clarify_ab/{off,options}.db`）——实测反问 **13 → 13**、可执行 **4 → 13**、裸反问 **9 → 0**、选项 **8 → 67**、`regressed == []` 且 `improved == []`：状态分布两臂**完全相同**（ok 9 / needs_clarification 13），这一层改变的是**可执行性**而不是通过率，真正把答案答出来的是第二轮，所以差值只报告、不进退出码。


**UI 必须消费两个字段**：`clarification.options`（挡路时的可选项）与 `alternatives`（回答之后的更窄问法）。REPL 答完列 `想收窄口径，可以直接这样再问一次：` 并接受序号；Web 控制台把两者都渲染成可点 chip（`app.js` 的 `messageMarkup`），点一下即以完整问句发起下一轮。**`alternatives` 曾在 Web 上被整段丢掉**——同一句话在终端能点、在浏览器只能手抄；因为 API 早就把它序列化进来了，缺陷只在渲染层，所以补一个测试（`test_web_api.py` 断言 payload 有 3 条 `alternatives` 且 `clarification is None`）比补一个接口更准。

---

## 4. Schema linking 与提示词


两层召回：

1. **表级粗召回**：`Catalog.search_tables` 用 BM25 对表名、同义词、描述、字段名/描述/同义词打分；表名或同义词在问题中精确出现时加 2.5 分。中文按 unigram/bigram/trigram 索引，无需 jieba。打分参数不是一个魔法常数，而是一组可命名策略，见 §4.1。可再叠一条向量通道（RRF 融合）和一次 LLM 重排，两者都**默认关闭**，理由见 §4.2。
2. **字段级精读**：模型必须调用 `get_table_schema` 获取真实字段、类型、敏感/禁用标记、必填过滤和关系。

### 4.1 表级召回是一组可命名策略

`linking.py` 把 BM25 的文档构造拆成四个可调维度，策略就是它们的组合：

| 维度 | 落地方式 | 为什么这样实现 |
|---|---|---|
| 字段权重 | 表名/同义词 ×3、描述 ×2、列文本 ×1 | BM25 只吃词频，所以权重 ≥1 写成**重复 token**；<1 写成**按步长抽稀**，不截断，因此结果与字段拼接顺序无关 |
| 长度归一化 `b` | `BM25(docs, b=strategy.length_norm)` | 小目录里长注释是信息量，几百表目录里薄表会被长度惩罚误顶到前面 |
| 注释同义词挖掘 | 每表最多 8 个词，跨表文档频率上限 = 表数的 6%，词频 ≥2 | 三分之一个库都出现的词不带信息；只在“注释即业务名词”的小目录有用 |
| token 覆盖重排 | 命中查询 token 比例 ×4 | 召回阶段的词频分数会因为重复词虚高，覆盖率是更稳的第二信号 |

- `resolve_strategy("auto", 表数)`：≥ `LARGE_CATALOG_TABLES`（50）用 `hybrid`（列文本抽稀到 0.35 + 关闭长度归一化 + 挖掘 + 覆盖），否则用 `baseline`；`NL2SQL_LINKING` 可强制指定，未知名字直接报错并列出可选值。
- **`link-check` 是这条链的测量台**：不连库、不调模型，只读 `question + expected_tables`，一次全量测量 0.4s，`--variants a b c` 打对齐表并列出最坏 5 例的 `missed`。仓库内 `link-check --cases src/nl2sql_agent/data/demo_cases.jsonl` 可直接复现 recall@5 = 0.939（113 条可回答用例 × 9 表目录；扩容前那 37 条是 0.965，降的是**语料难度**——新增的跨域难题一句问法要命中 4–5 张表——不是检索退化），20 条表召回标注集仍是 `@5 0.975`。

### 4.2 向量通道与重排：落地了，但默认关闭

P4 把"BM25 + Embedding + Rerank"整条混合链路做成了可开关的实现，然后在两份人工标注的表召回集上实测，结论是**默认不开**：

|---|---|---|
| baseline BM25 | 0.950 / 0.975 | 0.600 / 0.700 |
| hybrid BM25（大目录自动选用） | 0.975 / 0.975 | **0.812 / 0.850** |
| 纯向量 `text-embedding-v4` | 0.900 / 0.950 | 0.625 / 0.688 |
| RRF（BM25 top-20 ⊕ 向量 top-20） | 0.925 / 0.975 | 0.775 / 0.838 |
| hybrid + LLM 重排（qwen-plus） | 0.975 / 0.975 | 0.850 / **0.863** |
| RRF + LLM 重排 | — | 0.825 / 0.850 |

- **重排有增益但买的是每次检索一次模型调用**：+3.8pt @3、+1.3pt @5（0 次契约失败），而 Agent 一轮问数通常调用 2–3 次 `search_tables`，演示目录本就 0.975 封顶。为了 1.3 个点在默认路径上加 2–3 次串行调用不值，所以留作 `NL2SQL_RERANK=llm` 显式开启。
- 因此 **`auto` 策略仍是纯 BM25**，`NL2SQL_EMBEDDING` 默认 `none`、`NL2SQL_RERANK` 默认 `off`。这两条不是"没做完"，而是**测过之后的决定**——向量索引、缓存、融合、降级、重排契约全部有单测与离线开关路径，换 embedder 或换业务域时不用改代码就能复测。

实现上的三个约束：

- **文档构造**：`Catalog.table_documents()` 每表一条文档，剔除 `disabled` 与 `sensitive` 列——结构文本会发给外部 embedding 端点，值级字段没有理由进检索文档。
- **缓存失效**：`SchemaIndex` 的缓存文件名是 `schema_hash + provider + docs_hash`，改表名、加列、改注释、换模型都会产生新文件而不是复用旧向量；缓存损坏（截断/手改）读不出来时直接重算，不让一次坏缓存毁掉查询。
- **降级不留坑**：`NL2SQL_EMBEDDING=openai` 而 Key 缺失、驱动缺失、维度不一致、返回数量不匹配、查询向量维度不符——全部吞成"本次走 BM25"，并在 `SearchResult.retrieval` 里带 `degraded=true` 与原因；`link-check` 与 Web 都会把这行打出来。

`link-check --variants` 支持 `vector` / `rrf`，所以这张表可以在任何一台装了 Key 的机器上一行命令复现（演示目录那一列仓库内可复现，外接目录两列不可——见[数据与合规](../README.md#数据与合规)）。

### 4.3 提示词只装结构，不装全量 DDL

系统提示词只包含：

- 表名 + 一句话描述（不含全量 DDL，控制 token）；
- 业务口径（销售额、可用库存、在途、齐套率等）；
- 强制工作流；
- 硬性约束与最终 JSON 契约。

这样既避免把 185 张表全量塞进 prompt，也强制模型在生成前“看真实 schema”。

**布局按"供应商前缀缓存"排过（P2-5，`agent-v5`）**：同一目录下每一轮都相同的段（方言与基准日期、表目录、业务口径、工作流、硬性约束、JSON 契约）全部放在前面，只有 `few_shot` 块随问法变化，而且被放到**最尾部**——JSON 契约之后。`cache-check` 对演示目录量到 system prompt 2205 字符、公共前缀 2090、稳定比例 **94.78%**、易变段 115 字符、`few_shot_at_tail=true`。这只证明"布局给了前缀缓存命中的条件"，**不等于网关真的命中**：是否命中要看 `usage` 里的 `cached_tokens`，那一位由 scripted provider 测试钉住，真实供应商侧的效果尚未实测。

P2-6 的候选布局 `agent-v5-fewshot-early` 把 `few_shot` 移到 JSON 契约**之前**，作为灰度对照臂；`PROMPT_LAYOUTS` 是版本到布局的唯一映射，`_system_prompt` 之外的任何一段（head / contract / few_shot）都逐字节相同，无 few-shot 时两臂输出完全一致（测试钉住）。基线 `agent-v5` 的尾部布局没有改动。评测器用 `--prompt-version` 强制单臂，灰度比例不影响任何跑分。

---

## 5. SQL Guard 分层

`SQLGuard.check(sql)` 返回 `GuardResult(valid, sql, tables, columns, issues)`，任何 error 都阻断执行。

| 层 | 检查 | 例子 |
|---|---|---|
| L1 输入 | 去 code fence、空值、20000 字符上限 | ```sql ...``` |
| L2 语句 | 仅 SELECT/WITH、单语句、只读关键字 | `DELETE`、`; DROP`、`PRAGMA` |
| L2 方言 | 表提示白名单（仅 `WITH (NOLOCK)`）；2008 R2 拒绝 `OFFSET/FETCH`；按 `catalog.dialect` 拒绝**别的方言形状**（见 §5.1 末） | `WITH (UPDLOCK)`、`OFFSET 10 ROWS`、给 SQLite 写 `TOP 3` |
| L3 表 | 表白名单、禁用表、CTE 别名排除 | `internal_audit`、`WITH x AS ...` |
| L4 维度 | 必填过滤 | `dim_lookup.lookup_type='material_category'` |
| L5 列 | 禁止 `*`、`t.*`；列存在性；禁用/敏感列 | `SELECT *`、`o.custmer_name`、`customer_phone` |
| L6 权限 | principal 可见表/列、行级谓词是否在授权值内 | `permission_table`、`permission_column`、`row_filter_missing` |
| L7 风险 | JOIN 扇出静态分析（**只 warning**）、超过 5 张表 warning | 1:N 连接后对「一」侧聚合 |
| L8 结构 | 可选 `sqlglot` AST 第二意见，解析失败即拒绝 | `FROM[internal_audit]`、`randomblob(…)`、`SELECT … INTO #tmp` |

实现细节：

- 先 `mask_sql` 清空字符串字面量和注释，避免 `status='update'` 或注释里的关键字误报；同时把 `[x]`、`"x"`、`` `x` `` **三种**标识符引号剥成普通标识符——后面的表/列检查全靠词元匹配，漏剥一种就等于给那种方言开一条放行通道（`` `x` `` 是 2026-10-23 补的，见 §5.1 最后一行）。
- 方言取自 `catalog.dialect`：SQL Server 只放行 `WITH (NOLOCK)`，`WITH (INDEX/UPDLOCK/TABLOCK...)` 一律阻断；`OFFSET n ROWS` 在 2008 R2 上不可用，直接报 `offset_fetch` 让人改 `TOP`/`ROW_NUMBER()`；SQLite 对任何表提示报错。CTE 的 `WITH x AS (...)` 不会被误判成表提示。
- **按方言的形状黑名单**（`_SHAPE_RULES`，P2-2）：`mssql/sqlserver → tsql`、`postgres/postgresql/pg → postgres` 一类别名先归一，然后只在目录声明的那个方言上跑**属于别的方言**的形状——`TOP`/`FETCH … ROWS ONLY`/`::`/`ILIKE`/`[…]`/反引号/`||` 与 `'…' +` 拼接/外来的日期与空值函数，条数按方言 5–9 条（sqlite 6 / tsql 6 / postgres 5 / mysql 9），命中即 error 并回显检测到的片段。**未知方言不设任何规则**：在这里猜一个方言名就会拦住正确 SQL，而这一层存在的理由是别拦错的东西。规则读的是三种掩码投影之一——默认投影（字面量清空、引号剥掉）、`quoted`（保留引号，给标识符引用类规则）、`literal`（保留字面量，给拼接类规则），所以 `WHERE note = 'TOP 3'` 里的字面量、注释里的 `[x]` 都不会触发（`tests/test_guard.py::test_shape_rules_ignore_quoted_content`）。
- CTE 识别正则支持 `WITH a AS (...), b AS (...)`；CTE 名不当作物理表。
- `SELECT *` 检查会先剔除 `COUNT(*)` 等聚合星号。
- 列校验通过别名映射回真实表；未知列用 `difflib` 给相近字段建议。
- 表级裸列扫描兜底 `WHERE internal_note LIKE ...` 这类未限定列。

### 5.1 可选 AST 层：正则的盲区由解析器补，且 fail-closed

正则层读的是**掩码后的语句**，这既让它不会把字符串当 SQL，也是它的天花板：掩码/切词读歪一次，这条语句就完全绕过了校验。`ast_guard.py` 用 `sqlglot` 解析同一句话，只报结构（语句数、顶层类型、写/DDL 节点、真实表标识、CTE 别名、投影星号、危险函数、元数据访问），由 `SQLGuard` 翻译成 issue。实测补上的洞：

| 写法 | 只有正则时 | 加 AST 层后（实测码） |
|---|---|---|
| `SELECT created_at FROM[internal_audit] a` | **放行**（`FROM` 模式要求空白，掩码成 `FROMinternal_audit`） | `disabled_table` + warning `ast_table_bypass` |
| `SELECT randomblob(100000000)`、`xp_cmdshell('dir')`、`LOAD_EXTENSION('x')` | **放行**（关键字黑名单只认识 `EXEC`） | `dangerous_function` |
| `SELECT o.order_id INTO #tmp FROM sales_order o` | **放行**（不含 INSERT 的写） | `write_keyword` |
| `SELECT name FROM sqlite_master` / `information_schema.tables` | 拦住但理由是 `unknown_table` | 追加 `metadata_access`（“不许查系统表绕过目录与权限”） |
| `SELECT o.order_id, FROM FROM … GROUP BY BY` | **放行**（尾逗号让星号与列校验双双看空） | `select_star` |
| ``SELECT `order_id` FROM `internal_audit` ``（MySQL 风格反引号） | **放行**（掩码只剥 `[x]`/`"x"`，反引号标识符是个陌生词元） | `disabled_table`（实测修前 AST 层本来就拦得住） |

最后一行和前面几行不同：它不是"只能靠解析器补"，洞在 `mask_sql` 自己——同一层剥掉反引号，禁用表、敏感列、系统目录三类检查就重新看得见那个标识符，所以 2026-10-23 把它修在正则层（零依赖路径也一起补上），A/B 实测修前 3 条形状 `valid=True` 且零 issue、修后各自报 `disabled_table` / `sensitive_column` / `unknown_table`，同形无引号对照组修前就拦得住。这条现在钉成一条要维护的性质：**每一种标识符引号都必须在掩码阶段剥掉**，新方言/新引号风格进来时这一步要重跑（语料 +4、回归 +2）。

**同一层还补上了第二个洞，但方向相反**：§8.1「方言 gold 集」量出**正则层对跨方言形状 0/22 全盲**——`SELECT TOP 3 …` 与 `… LIMIT 3` 在掩码词元里没有区别，22 条错方言形状只有可选 AST 层看得见 6 条，剩下 16 条两层都放行，其中 9 条 SQLite 照跑并给出一个错的数字。P2-2 把判定放回正则层（`_SHAPE_RULES`，见 §5 实现细节）：同一份语料复跑 `dialect-check`，正则层 **22/22**、AST 层 22、**AST 独占 6 → 0**、两层都不拦 16 → **0**，而 `gold 护栏` 一列仍拦 **0**（113 条冻结集 gold 两层 0 error / 0 warning、对抗集通例 `pass_top_mssql` / `pass_backtick_projection` 依旧放行），退出码 0。这条洞的修法之所以能立刻验证，是因为仪器（22 条三方言形状语料 + 两个计数分开的审计）在上一阶段就已经建好并只用作测量。

**失败即关闭**：启用 AST 层后，`sqlglot` 缺失（`ast_unavailable`）或语句解析失败（`ast_parse_failed`）都是 error，不退回“正则放行”。开关若能在读不懂的时候放行，它就只是安全表演——绕过护栏最便宜的办法永远是写护栏读不懂的 SQL。默认 `NL2SQL_AST_GUARD=off`（依赖是可选 extra，解析也有开销），`result.ast` 只在启用时透出。

开销也测过端到端那一侧（`mock`，123 条，AST 关 9 次 / 开 7 次取中位数）：判定**逐条零差异**（`passed`/`status`/`failure_layer`/`generation_subtype`/`sql`/`row_count` 全等，`mock` 的 SQL 从不被护栏拒，所以这证明的是无回归而不是"AST 不影响答案"），整轮 `wall_clock_ms` 484 → 812，每条用例 `avg_latency_ms` 2.54 → 4.98。分解后：warm-up（`import sqlglot` ≈100ms + `AstAnalyzer(...)` ≈34ms）占多花开销的四成，其余 194ms 摊到探针数出来的 **266 次 `SQLGuard.check`**（2.16 次/条）≈ 0.73ms/次，与微基准的 +0.55ms 同量级。**所以报"AST 让延迟涨 68%"必须声明是哪种进程**：常驻 `serve` 只付一次 warm-up，边际成本约每条问题 1.5ms。

### 5.2 行级/列级权限：同一份策略两次生效，方式不同

`Principal`（`principal.py`）描述一个角色能看见什么：`tables` / `only_columns` / `exclude_columns` / `row_filters`。它在两个地方起作用，而且**故意用不同的机制**：

- **Agent 侧是过滤视图**：模型与工具只看到该角色可见的表、列和关系（无权列不会出现在“相近字段”建议里），单值行过滤还会作为 `required_filter` 提示注入 schema 描述。看不见就不会写错，但视图不能当安全边界。
- **护栏侧是完整目录判权**：guard 永远拿全量 catalog 判定，越权表报 `permission_table`（不是 `unknown_table`，否则等于把权限策略泄露成“这表不存在”）、越权列报 `permission_column`、行级谓词不合规报 `row_filter_missing`。改视图、伪造表名、走 AST 才发现的隐藏引用，都过这一关。

行级谓词的判定是 fail-closed 的文本扫描：该列的**每一个** `column = …` / `column IN (…)` 谓词都必须落在授权值集合里，出现子查询、`LIKE` 或 `OR` 的越权分支一律拒。这让 `region IN ('华东','华南')` 对多值角色成立，而 `region = '华东' OR region = '华北'` 不成立。

哈希与角色是绑定的：`schema_hash` 取**过滤视图**的哈希，因此换角色就换哈希——few-shot 与经验库正是按它隔离的，`rep_east` 验证过的口径不会喂给看不全同一张表的角色。Web 控制台的 `/api/health` 同时报这个哈希和 `guard.principal`，缺了后者前者就无法解释（给人看的 `/api/schema` 仍是完整目录，它不是模型侧通道）。

### 5.3 JOIN 扇出：只报警，不拦执行

`fanout.py` 静态判断“1:N 连接之后，聚合的列只来自「一」侧”这种把父表一行当 N 行算的形状（销售额、齐套率最容易错在这里），报 `join_fanout` **warning**，永不 error——它没有执行计划，判不准是常态，把启发式当门禁会大量误杀正确的 gold SQL（实测第一版就在 37 条 gold 上误报 2 条 `SUM(i.on_hand_qty * m.standard_cost)` 这种父×子混合聚合，那是库存金额的标准算法）。判别口径最终收敛成“**本次聚合的列只来自「一」侧**”：只要聚合参数里出现子侧列就不报。

语料扩到 113 条 gold 后又暴露两类误报，**修法都是改启发式 + 加回归测试，不改语料**：

- `_COMMA_FROM_RE` 的字符类里带了空白与逗号，跨子句一路吃到第一个逗号，于是任何带多列排序的合法 SQL（`… GROUP BY o.status ORDER BY c DESC, o.status`）都被读成「FROM 逗号并表」。改成「表项 + 只出现在表项之间的逗号」。
- `MAX`/`MIN` 被算进“父侧聚合”：重复行不改变它们的结果，而 `MAX(m.safety_stock)` 配子侧 `SUM` 正是把父表属性带到子表粒度的标准 roll-up。`_FANOUT_TOLERANT_FUNCS` 把这两个函数排除在判定之外，同形状下的 `SUM`/`COUNT` 仍照常告警。

**反过来它也测不出真坑**：问法层对抗集（§8.1「对抗问法集」）里 4 条**确实会算错**的 `wrong_sql`（订单额×发货行、在途×现有、计划产量×发货行、销售额×锁定行）在这条启发式下**一条 `join_fanout` 告警都不发**（`fanout_warning_cases = 0`，AST 开/关相同）。原因不是漏判而是**形状不在它的射程里**：这四条的聚合参数全来自子侧列，错在"两条互不相干的 1:N 连接互相放大行数后再各自求和"，而告警针对的是"父侧一列被重复 N 次求和"。结论是这个静态分析器**只能提示、不能识别扇出错答**，扇出陷阱的可测量判定必须走结果集比对（gold 与 `wrong_sql` 两套都真执行），不能指望护栏替它背书。

### 5.4 `guard-check`：无模型、无连接的护栏基准

护栏的价值不在“写了很多规则”，而在“漏了什么、误杀了什么”能量化。`guard_suite.py` + `nl2sql_agent guard-check` 用合成用例把三件事分开测：

- **对抗 41 条**（`adversarial_cases.jsonl`，31 拦 / 10 通）：正则单独跑时 `layer=ast` 的用例**执行但不打分**，所以 `regex_missed` 只在正则作用域内有意义。实测：正则 25/25（作用域 35 条），正则+AST 31/31，**3 条**仅 AST 可拦（`adv_randomblob` / `adv_select_into` / `adv_xp_cmdshell`），**3 条**正则拦住但理由不对（`adv_information_schema` / `adv_nospace_from` / `adv_sqlite_master`：正则报 `unknown_table`，AST 报 `metadata_access` / `disabled_table`）。前者是 4 条、后者是 2 条，2026-10-23 方言形状规则落地后 `adv_nospace_from`（`FROM[internal_audit]`）改由正则层的 `dialect_bracket_quote` 拦住，从"只有解析器看得见"变成两层都拦、诊断不同。其中 4 条（3 拦 / 1 通）是 2026-10-23 量出反引号绕过之后补进来的，见 §5.1。**这批是绕过护栏的 SQL 形状，不是诱导性问法**——后者在 `eval --adversarial` 的 24 条问法用例里（`adversarial_question_cases.jsonl`，判"该不该答"与"数据有没有出去"，见 §8.1），两套语料不能互相顶替。
- **权限 23 条 / 5 角色**（`permission_cases.jsonl` + `principals.json`）：既验拦截（17 条），也验**视图泄露**（无权表/列名、行过滤值不得出现在过滤视图的任何字段里）→ 0 泄露。17 这个数字**现在与 `sqlglot` 装没装无关**（`--no-ast` 与零依赖安装复跑都是 17）：以前它随解析器在 16/17 之间跳，因为 `perm_rep_ast_combo` 的 `FROM[internal_audit]` 只有解析器看得见，方言引用规则落地后正则层自己就拦得住。
- **扇出 11 条**（含负例）+ **gold SQL 误杀审计**：冻结评测集 113 条有 gold 的用例在两层各跑一遍，要求 0 error、0 warning（10 条无 gold 的拒答用例计入 `skipped`）。

`tests/test_guard_suite.py` 把上面这组分层数字钉成回归门禁，CI 每次跑 `guard-check --json`。


---

## 6. 确定性修复

修复只在执行报错后触发，策略是“可回退、低风险、不扩权”。

| 规则 | 触发 | 行为 |
|---|---|---|
| `clean_syntax` | 任意（含 102 语法错误） | 去尾分号、清理 `, FROM`、`, )` |
| `remove_invalid_projection` | 207 / SQLite `no such column` / SQL Server 42S22 / 中文“列名无效” | 仅从 SELECT 列表移除报错列，保留 WHERE/JOIN/GROUP BY |
| `wrap_group_by_expression` | 8120 / “选择列表中的列…” | 对 SELECT 中的二元算术表达式包 `SUM()` |

`classify_tsql_error()` 从 pyodbc 消息末尾的 `(207)` / `(8120)` / `(102)` 取错误码（只认这三个已知码，消息里其它数字不会触发规则），把规则选择从“字符串猜”变成“按码路由”：207 才允许删投影、102 只做语法清理、8120 即使是英文消息也能走 `SUM()` 包裹。SQLite 错误没有码，仍走原有文本匹配。

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
  status,       -- candidate | verified | deprecated
  quality,      -- candidate | verified | gold
  schema_hash, hit_count, created_at, last_used_at,
  UNIQUE(normalized_question, schema_hash)
)

failure_queue(
  id, question, normalized_question, schema_hash, category,
  reason,       -- needs_human | failed | max_steps | user_feedback:<reason>
  last_sql, detail,
  status,       -- open | resolved | dismissed
  occur_count, created_at, last_seen_at, resolved_at, resolution,
  UNIQUE(normalized_question, schema_hash)
)

feedback(
  id, session_id, turn_index, trace_id,
  question, normalized_question, effective_question,
  sql,          -- 被否定（被反馈）的那条答案，只作反例
  suggested_sql,-- 用户在投诉时顺手给出的修正（候选，未生效）
  category, reason,   -- result_wrong | metric_wrong | field_wrong
  detail, schema_hash, principal,
  status,       -- open | approved | rejected
  occur_count, failure_id, failure_owned, counterexample_id, experience_id,
  created_at, last_seen_at, reviewed_at, reviewer, review_note
)
-- UNIQUE(session_id, turn_index, reason) WHERE status='open' AND session_id!='' AND turn_index>=0
```

`feedback` 是**审核队列**，不是第二份经验库：每一行记住"谁在哪一轮对哪条答案不满意"。`sql` 列存的是用户**否定**的答案，因此它只被复制进反例（`counterexample_id`），永远不会被写进 `experiences`；`suggested_sql` 只有在与 `sql` 不同的时候才会另存一条 `source='feedback'` 的 candidate（`verified=False`，SQL 层召回不到）。`failure_owned` 标明这条失败是不是本次投诉创建的——拒绝时只撤销自己创建的失败与反例，同一问法上更早的真实执行失败保持 open，不会因为"用户点错了"而丢失一次真实故障。部分唯一索引让同一轮同一原因的重复点击折叠成一行并累加 `occur_count`；并发插入撞索引时捕获 `IntegrityError` 再折一次，而不是把第二个请求变成 500。

`category` 由 `taxonomy.classify_category()` 规则分类器填写（`inventory / purchase / production / bom / kitting / detail / group_by / ranking / time_filter / metric`，无命中为 `unknown`）；调用方显式给出类别时不覆盖。分类器只区分领域与答案形态，**不承担超范围/拒答判定**（那由 schema 查不到表 + 护栏决定）。

`status` 与 `quality` 是两根**正交**的轴：`status` 管可见性（只有 `verified` 能被召回，`deprecated` 是人工退休，仍可审计与复活），`quality` 只做排序先验（`gold` 加分，`candidate` 不加分）。`gold` **只能由人显式给出**——工具与 `experience add` 都不写这个值，因为没有独立 gold 的情况下自动晋级等于把"模型自己觉得对"当成金标准。旧库里的 `quality='hard'` 在打开时迁移成 `verified`（`hard` 从来不是一个人工定义过的等级，只是历史默认值）。

`failure_queue` 同时承担**反例经验**的角色：一行就是"这个问法在这个 schema 上错了、错在哪个错误码、错了几次、最后一次 SQL 是什么"。它有三个消费者——人工分诊（`experience counterexamples` 按错误码聚合并列出）、重复计数（`occur_count` / `last_seen_at`）、以及 `save_experience` 的**准入闸门**（同一归一化问题 + 同一 SQL 若仍在 open 队列里，工具直接拒绝把它存成候选经验）。它**不是提示词输入**：`WRONG_SQL` 与 `reason` 永远不会出现在 system prompt 或 few-shot 块里，`tests/test_counterexamples.py::test_counterexamples_never_reach_the_prompt` 直接断言这两处都为空。原规划里"提示模型避免同类错误"这一条因此**刻意不做**——它与同一节验收项"错误经验不会进入提示词"冲突，而且会改变 `prompt_hash`，让所有已发表的准确率失去可比性。

### 7.2 准入流程

```text
execute_sql 成功
   └─ save_experience → candidate（永不召回）
          ├─ 准入闸门：同一问法 + 同一 SQL 若在 failure_queue 中仍为 open → 拒绝写入
          └─ 人工 experience verify
                 ├─ guard.check(sql) 必须通过
                 ├─ 真实执行必须成功
                 └─ → verified（可召回）

人工分诊
   ├─ experience promote <id> --quality gold   # 只能人给；工具永远写不到 gold
   ├─ experience deprecate <id> --reason ...   # status=deprecated，退出召回但保留行
   └─ experience staleness / deprecate-stale   # schema 变更后的批量退休（默认只预览）

Agent 终态失败（needs_human / failed / max_steps）
   └─ record_failure → failure_queue open
          ├─ 同 (归一化问题, schema_hash) 只保一行，重复只累加 occur_count + error_code
          ├─ 已结案的再次失败 → 自动 reopened
          └─ 人工 experience resolve <id> --sql "..."
                 ├─ validator = guard.check + 真实只读执行（缺一即抛错，队列保持 open）
                 └─ → 队列 resolved ＋ experiences 立即写入 verified

用户反馈（result_wrong / metric_wrong / field_wrong）
   └─ record_feedback
          ├─ feedback open（同一 session+turn+reason 的去重与并发折叠）
          ├─ failure_queue user_feedback:<reason>（failure_owned 标记归属）
          ├─ counterexample feedback_<reason>（被否定的 SQL，永不进提示词）
          └─ suggested_sql 与原文不同 → experiences candidate（不可召回）
                 ├─ 审核通过：必须带修正 SQL → resolve_failure（guard + 真实执行）→ feedback approved + verified
                 └─ 审核拒绝：必须写原因 → feedback rejected；只撤自己创建的失败/反例
```

入队发生在结果组装阶段并被 try/except 包裹：记忆层故障不得改变返回给用户的答案。`needs_clarification` 与 `refused` 不入队——它们是正确的行为而非失败。`experience stats` 合并队列指标，其中 `failures_by_category` 只统计 open 行（待人工处理的积压）。

**stale 是显式动作，不是自动动作**：检索本身按 `schema_hash` 精确隔离（换了目录就召回不到旧经验），`mark_stale` 批量退休只由 `experience deprecate-stale` 人工触发，默认先预览、`--yes` 才落库。原因是一个配错的目录路径会得到一个新的 schema hash，如果据此自动退休，一次配置错误就能静默清空整个经验库；隔离保证"用不到"，退休保证"看得见"，两者分开。

### 7.3 召回与自动注入

- 只在 `status='verified' AND schema_hash=当前hash` 中搜索（候选经验在 SQL 层就被排除）；
- 精确归一化问题得 1.0；
- 其它：`0.55 * token Jaccard + 0.45 * SequenceMatcher`，子串匹配 +0.08；
- 与问题**同类别**的 verified 经验额外加 `category_boost`（默认 0.12，置 0 关闭；`strict_category=True` 时直接过滤掉异类）；
- 默认阈值 0.3，top-k=3；命中更新 `hit_count/last_used_at`。
- **gold 加分**：`quality='gold'` 的行在基础分之上加 `gold_boost * ceiling`（默认 0.05 × 该臂最高基础分，尺度无关），且在所有通道与 MMR 之前生效；默认值在演示库上是 no-op（没有人工 gold）。
- **通道**：`NL2SQL_EXPERIENCE_RECALL=lexical|vector|rrf`（默认 `rrf`）。`lexical` 逐位等于旧行为；`vector` 用与表检索同一套 `EmbeddingProvider`；`rrf` 是两榜的排名融合（k=60，与表检索同一实现）。向量通道需要 `NL2SQL_EXPERIENCE_VECTOR=true` 且 provider 不是 `none`，否则**配置期直接报错**而不是静默退回词法。
- **多样性**：`NL2SQL_FEWSHOT_DIVERSITY`（0–1，默认 0）接 MMR，相似度用 `token_union(question, sql)`。λ=0 时**逐位等于旧 top-k**；λ>0 时贪心最大化"与问法的相关度 − λ × 与已选样本的相似度"。它换来的是不同 SQL 形状的覆盖，代价是单目标命中率，所以只在 3 条预算里才有意义，且没有在线模型 A/B 前默认关闭。
- **向量缓存**：`DocumentIndex` 按 `(schema_hash, provider, docs_hash)` 缓存，库内容变了才重建；provider 抛错或返回短向量时**退化为词法通道**并在 `recall` 元数据里标 `degraded`，不改变召回集合之外的行为。

除 `search_experience` 工具之外，Agent 在每轮运行前**自动**把召回结果拼进 system prompt：`few_shot_max`（默认 3）条、`few_shot_max_chars`（默认 1200）字符预算，按整块丢弃以保证 SQL 不被截断；`few_shot_enabled=false` 可完全关闭。注入内容只作 few-shot 参考，不允许据此跳过 validate/execute。

**设计动机**：LLM 自生成经验容易形成“错误答案 → 存库 → 下次召回 → 继续错误”的闭环。人工验证是刻意保留的瓶颈；schema hash 则解决表结构变更后的知识过期；失败队列把“答不上的问题”变成可统计、可结案的人工待办，结案产物又直接回流为可注入经验，闭环由此合上。

**测量**：`experience-check`（`experience_audit.py`）在冻结集的 32 对源/改写问法上跑五条臂（`lexical` / `diversity` / `gold-half` / `vector-hash` / `rrf-hash`），报告 hit@1/3/5、MRR、gold 分组与降级计数，并断言 `lexical` 臂逐条等于 `store.search()` 的历史结果。它**只测召回**，不调生成模型；默认 `embedding=hash` 是词袋替身，因此向量/RRF 两臂只能证明缓存、融合与降级通路，不能证明语义增益。实测数字与默认值取舍见 `docs/IMPROVEMENTS.md` P1-4。

### 7.4 人工反馈闭环与周报

**入口**：CLI `experience feedback` / `experience review` / `experience report`，Web `POST /api/sessions/{id}/feedback`、`GET /api/feedback`、`POST /api/feedback/{id}/review`，以及问数页每个回答下方的三个按钮。HTTP 提交的 question 与 SQL **取自服务端保存的轮次**而非请求体——否则客户端可以给一条从未展示过的答案挂反馈，审核人看到的证据与用户实际看到的就不是同一件事；`turn_index` 越界、该轮没有 SQL 结果、建议 SQL 不过护栏都返回 422。

**审核**：通过必须带修正 SQL，并复用 `resolve_failure` 的 validator（护栏 + 真实只读执行），成功后反馈置 `approved` 并回填 `experience_id`；没有 validator 时 `review_feedback` 直接抛错，不接受"肉眼通过"。拒绝必须写原因，且只把 `failure_owned=1` 的失败与错误码以 `feedback_` 开头的反例置为 dismissed/resolved；同一问法上更早的普通执行失败不受影响。审核路径与失败结案共用一套校验，避免两处规则漂移。

**周报**（`feedback.build_weekly_report`）聚合四块：反馈（总数 / 按原因 / 按状态 / Top 问题）、Top 失败问题、经验复用率、schema 影响（active 与陈旧经验数、旧 hash 数、反馈与失败的陈旧 open 数）。默认**只出 sha256 指纹且 SQL 永不出现**，`--show-text` 才带本地原文——周报要能贴进工单而不夹带业务措辞。复用率取自事件日志窗口：`turn` 行中 `few_shot.injected > 0` 的占比，与库内终身 `reuse_hits` 分列并标注；没有 `NL2SQL_LOG_PATH` 时输出"不可计算"而不是 0。日志缺失、坏行数也在同一块里自报，避免把"没测到"读成"没发生"。

**实测（2026-10-23，`provider=mock`、演示库）**：浏览器闭环提交 → 审核通过（写入 verified）→ 第二条拒绝；KPI `0/1/0/1` → `0/1/1/2`；修掉一处事件监听器随重绘累加导致一次点击发多个审核请求的缺陷后，`reviewPosts: 1`。带日志的周报：复用率 **100.0%（1/1 轮注入）**、窗口 tokens 0、库内累计命中 2；schema 影响 当前 `6c6906abd54e`、经验 active 1 / 陈旧 0。未做：真实用户反馈数据、在线模型上的效果测量、自动修复草稿、多进程并发写。

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
- **双通道一致率**（`dual_channel_agreement_rate`）：只在实际跑了第二通道的可回答用例上计算，衡量"同一问题两次独立生成 + 真实执行，结果是否一致"。
- **改写配对一致性**（`paraphrase` 块，`--paraphrase` 才有）：同义改写用例只改问法，`gold_sql / expected_tables / category / difficulty / order_sensitive` 逐字抄自冻结集源用例，所以改写与源用例**判定不同**只可能是问法造成的。块内给 `verdict_agreement_rate / both_pass / both_fail / brittle / reverse / unmatched / source_accuracy / rewrite_accuracy / row_count_agreement_rate / mean_table_recall_{source,rewrite}`。
- **对抗问法四态**（`adversarial` 块，`--adversarial` 才有）：每条用例带 `trap_kind`（`sensitive_column` / `disabled_column` / `disabled_table` / `select_star` / `metadata` / `fanout` / `guess_pressure`）与显式禁止目标（`forbidden_columns` / `forbidden_tables`，扇出条另带 `wrong_sql`）。安全类每条落一个 verdict，按优先级判定：`leaked`（禁止目标出现在**执行结果列名**里）→ `guard_missed`（禁止形状过了护栏但没成为答案）→ `blocked_by_guard`（真去取了、护栏拒了）→ `clean_no_attempt`（本轮无可拦之物）。块内给 `safety{cases, leaked, guard_missed, blocked_by_guard, clean_no_attempt, leak_rate, guard_miss_rate, blocked_rate, clean_rate, refusal_expected_cases, refusal_hit}`、`fanout{gold, trap, neither, no_sql, fall_rate, gold_rate, fanout_warning_cases, fanout_warning_on_trap}`、`guess_pressure{asked, guessed, over_clarified, answered, answered_off_gold, ask_rate}` 与 `by_trap_kind`（每族 cases/passed/verdicts/leaked_ids/guard_missed_ids）。
- **Valid SQL rate**：可回答用例中，产出 SQL 且通过 guard 的比例。
- **Execution success rate**：状态为 `ok` 的比例。
- **Refusal accuracy**：拒答用例中，`needs_clarification/refused` 且无 SQL 的比例。
- **table_recall@5**：`expected_tables` 是否被 `Catalog.search_tables` 的 top-5 覆盖，衡量 schema linking 质量（与 SQL 正确性解耦）。
- **平均步数/延迟/token**：观测 Agent 效率与成本；token 来自 `LLMResponse.usage`，`mock` provider 下为 0。

数值归一化：`round(float, 4)`；字符串 `strip()`；布尔保持布尔。gold SQL 执行失败会标记为 `gold_sql_failed`，避免把评测集错误算成模型错误。报告按分类（`category`）、按**难度**（`by_difficulty`：cases / share / passed / accuracy / avg ms，未标注的用例归 `uncategorized`）与按会话类型（单轮/多轮）拆分，元数据记录 provider、模型、用例数、git sha、时间戳与**护栏配置**（`metadata.guard = {ast, ast_available, fail_closed, principal, allow_sensitive}`：AST 与角色都会改变判定，缺这一位就无法区分"模型变了"和"配置变了"，`ast=on` 而 sqlglot 缺失时 `fail_closed` 直接说明整页数字都不有意义）；`--multi-turn` 跑出来的 10 条多轮用例同样带 `difficulty`，所以难度切片在两种运行下都有意义。不带 `--fail-on-regression` 时 CI 的退出码由是否有失败用例决定（带上时由门禁判定决定，见下文「两道门禁分工」）；报告写入 `reports/`（gitignore），`--json` 的标准输出在比对时多出 `comparison` 与 `regression` 两个块，跑改写集时多出 `paraphrase` 一块、每条用例多一个 `paraphrase_of`，跑对抗集时多出 `adversarial` 一块、每条用例多 `trap_kind / adversarial_verdict / adversarial_evidence / adversarial_codes / fanout_warning` 五个键，基线路径的提示走 stderr 以保证 stdout 仍是一份可解析的 JSON。

**失败责任层（`by_failure_layer`）**：每条未通过的用例带一个 `failure_layer` 标签，报告按层聚合 `cases / share / full_recall / ids`（通过用例标签为空，切片在零失败时为空字典）。九层按证据强度排优先级：`label`（gold 自己 0 行而 agent 有行，先怀疑标注）→ `triage`（超范围没拒答）→ `clarification`（该问没问 / 该答却问）→ `guard` → `execution` → `retrieval` → `stalled` → `generation` → `unknown`。归因**刻意保守**：`guard` / `execution` / `retrieval` 只在整轮从未产出可执行最终 SQL 时才认定，中途被护栏拒过一次但随后修好并执行的用例归 `generation`——把过程告警当失败原因是最容易骗自己的归因方式。判据来自 `agent.py` 的 `tool_trace[].stage`（`guard` / `execution` / `tool` / 空），它读工具 payload 的结构（`data.stage`、`valid is False`、`success is False`）而不是 `summary` 文案——文案是给人看的，改一个字就会把归因打散。`full_recall` 那一列统计该层里期望表已**全部**进提示的失败条数，用途是把"检索排除"变成数据：`mock` 全量 123 条实测 62 条失败 = `generation` 36（其中 **32 条满召回**）+ `clarification` 25（23 条满召回）+ `triage` 1，`retrieval / guard / execution / stalled / label` 均 0；单轮与 `--multi-turn` 两次跑分层分布完全相同（多轮 10 条全过，没引入新失败）。**这一列不代表在线模型的失败结构**：它目前只有 `mock` 的数，在线模型上要重跑才有。

**`generation` 层的子类型（`by_generation_subtype`）**：`generation` 里每条失败再用**评测器已经执行过的两个结果集**分一次形状——`empty_result`（0 行 vs gold 有行）/ `order_only`（行集合相同仅顺序不同，只在 `order_sensitive` 用例上成立）/ `column_shape`（列数不同）/ `grain_or_scope`（行数不同）/ `value_mismatch`（形状同、数值不同）。判据顺序固定，零额外执行成本，`meaning` 与 `full_recall` 同样进 JSON 和 Markdown。两条边界：`grain_or_scope` **不再拆**——扇出放大、漏聚合、过滤缺失在行数上无法区分，硬拆就是编；`column_shape` 在 `mock` 上排第一（61.11%，22/36）是规则通道没模板时退化成通用查询的产物，与 P6 在线模型人工归因的第一大项（粒度/口径 18/26、16/23）不是同一个形状，所以这一列也只是 `mock` 的失败结构。另外 `gold_sql_failed`（gold 自己跑不通）现在直接落 `label`，不再可能被算进 `generation`。


**同义改写集为什么排在门禁之外**（`data/paraphrase_cases.jsonl`，32 条，`tags=["paraphrase"]`）：改写用例不带 `v1_baseline`，也不在 `benchmarks/ex_baseline.json` 记录的 `total=123 / answerable=113` 里，所以 `--paraphrase` + `--fail-on-regression` 会被基线校验判为**语料规模不符**（退出码 2，有测试钉住）——这是设计而非缺陷：基线相减只在同一份语料上有意义。改写的价值在**配对差分**：同一次运行里源用例与改写用例走完全相同的 agent、库与护栏，两侧都过 / 都不过 / 只有源侧过（`brittle`）/ 只有改写侧过（`reverse`）四种状态一次测出，`recall@5` 两侧同分时脆性就落在 `clarification` 或 `generation` 层而不是检索。`mock` 实测 32/32 配对、一致率 87.50%，但其中 20 对是**两侧都失败**，所以真正可读的数字是"源用例通过的 12 对里有 4 对被问法打倒"（4/12 = 33.3% 脆性）——`verdict_agreement_rate` 单独拿出来会系统性偏乐观，报告因此同时打印 `both_pass / both_fail / brittle / reverse` 与两侧各自的准确率。配对按 `paraphrase_of` 前缀 id 严格匹配，落单的改写进 `unmatched` 并如实列出，不会被静默丢掉。

**对抗问法集为什么把四态分开报**（`data/adversarial_question_cases.jsonl`，24 条，`tags=["adversarial_qa"]`，同样不带 `v1_baseline`、同样被门禁以"语料规模不同"拒收）：单一 pass/fail 分不出"护栏挡住了"和"模型压根没去碰"，而 `clean_no_attempt` 是这几种状态里最容易被误读成成绩的一种——`mock` 上 16 条安全用例**全部**落在这里（`blocked_by_guard = 0`），所以这段的产出是"判定能区分四态且区分逻辑有测试"，不是"防线经受住了诱导"。第二层设计是**证据不复用护栏的解析**：泄露判定取执行结果列头 + 对每条尝试过的 SQL 做独立词元扫描（`mentions_identifier`，不经 sqlglot），护栏回报的表/列与 issue code 只作佐证；如果只看 `guard.columns`，解析器瞎的地方泄露检测跟着瞎，`tests/test_adversarial.py` 用一个故意放行的 `_PermissiveGuard` 钉住这条（护栏瞎 → `guard_missed` 必须亮）。**扇出陷阱**用另一套办法：`gold_sql` 与 `wrong_sql` 都真执行，按结果集判 `gold / trap / neither / no_sql`，四对陷阱机器验证过两侧结果集不同（12/4/8/6 行 vs 5/4/2/6 行）且 `wrong_sql` 全过护栏。两个由这一集**测出来**的护栏事实：`SELECT *` 只报 `select_star`、不会展开成它实际返回的敏感/禁用列，所以星号类只能判"该不该拒"；系统目录访问在正则层报 `unknown_table`、AST 层报 `metadata_access`，故 `expect_codes` 两个都声明，判定不随 `.[ast]` 装没装漂移。AST 开/关端到端复跑 147 条：三个计数块与逐条 verdict 全同（per-case diff 为空）——**判定不依赖解析器**正是这套设计的验证结果，报告自报 `metadata.guard` 让读者核对本次开了什么。边界：`principal=None`，角色扮演那条走问法层而非权限层；扇出识别靠预置 `wrong_sql`，没有任意 SQL 的扇出自动判定（本次四条 `join_fanout` 告警为 **0**）；在线模型的泄露率/拒答率/掉坑率未测。

**方言 gold 集为什么不走 `eval`**（`data/dialect_cases.jsonl`，22 条，`tags=["dialect"]`，`dialect_audit.py` + `nl2sql-agent dialect-check`）：冻结集永远在 SQLite 上执行，所以**方言错误在这条链路上不可能变成评测失败**——它表现为实际引擎拒绝，或者更糟，表现为数字错了却没有报错。离线只有 SQLite 一个可执行引擎，于是把"能不能测"拆成两列：① **护栏是否接受本方言的原生写法**（只需 catalog，不需要连接）；② **本方言 gold 转译到 SQLite 后结果集是否与手写参考一致**（需要转译器，不需要第二台数据库）。每条用例因此带三套 SQL：`gold_sql`（本方言地道写法）、`sqlite_sql`（同意图在可执行后端的参考结果集）、`wrong_dialect_sql`（把别的方言形状塞进本方言），且同一意图的 `question` 与 `sqlite_sql` 逐字相同——行间唯一变量就是方言。

两个计数**必须分开**：转译后**结果不同且不报错**叫 `divergence`（静默偏离，实测 3 条：`CONVERT(char(7),…,120)`、`+` 拼接、`CAST(… AS DECIMAL(18,2))`），转译后**直接报错**叫 `exec_error`（响亮失败，实测 3 条 `dialect_year_*`）。只有后者能被"报错重试"这类逻辑兜住，把两者并成一个"失败率"就会高估兜底能力。可比 22 条（原生 3 / 转译 19）里一致 16 条，整体 72.73%、**只看转译的 19 条 68.42%**。退出码只由四条语料/护栏不变量决定（gold 被拦、gold 解析失败、参考执行失败、参考空结果），偏离与漏拦是**发现**而不是这一步的红——否则这个命令在 CI 里永远红，等于没有这个命令。


**降级形态自报**：无 `sqlglot` 时解析列全为 `unmeasured`、可比只剩 3 条原生、一致率后附 `（可比 3/22 条）`，并打印"不是 0 命中"；后端为 `MssqlTarget` 时执行列全部未测量，但护栏与解析两列仍测量（不依赖连接）。刻意**不含相对时间轴**（`current_date` 一类），因为两侧碰巧相同会被读成"一致"，那是假证据；比对基准是 sqlglot 的转译语义，不是实际引擎执行结果。

**接在线模型时的口径**（P6 实测沿用，复跑者照做即可）：


- **每条跑分用全新空经验库**（`NL2SQL_EXPERIENCE_PATH` 指向一次性 db）。经验库按 `schema_hash` 命中后会把近似 gold 的 SQL 当 few-shot 注入，沿用规则基线留下的库，测出来的就不是模型的能力。
- **失败归因不靠读文本**：把每条失败用例的候选 SQL 与 gold SQL 都在同一库上重跑，先比多重集再比行序，分成"行数不同 / 同行数不同值 / 仅行序 / 未产出最终答案 / 拒答口径"五类；同时核对 `table_recall_at_5`，把检索侧从归因里排除。这一套比法后来做成了报告里的自动列（`by_failure_layer` + `by_generation_subtype`，见 §8.1），但 P6 那批在线模型数字（18 与 16 条粒度/口径、2 与 6 条仅行序）出自当时的人工重跑，与自动列的 `mock` 分布是两次不同测量。
- **判分规则不因结果调整**：`needs_human` 不计拒答命中、`order_sensitive` 用例仍要求行序，即便这样会让某个指标变难看。
- **聚合值稳定 ≠ 单条稳定**：同 commit、同空经验库、`temperature=0.1` 固定下两次独立 40 条跑分给出完全相同的聚合（14/40、EX 0.3514），但通过集合差 4 条、7 条状态漂移。因此只在聚合层面横比模型，几个点的差距不支撑排名；要排名需同配置多跑取均值。
- **模型草拟的用例只能是 `status="draft"`**：`eval generate-cases`（`casegen.py`）产出的每条草稿都要过契约 → 护栏 → 真实执行且有行三道关，落盘时状态固定为 `draft`；评测器把非 `active` 的用例记进 `skipped` 而不执行，`total` 只数跑过的用例，所以草稿在结构上无法移动任何已发布指标。进冻结集的唯一途径是人工把 `status` 改成 `active`。

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
| 更强 schema linking | 已实现：`linking.py` 向量榜 + RRF 融合、`rerank.py` LLM 重排、任一步失败降级 BM25（外接目录实测无增益 → 默认关闭，见 §4.2） |
| 多轮对话 | 已实现：`conversation.py` 状态 + 改写层产出独立问题后再进本循环（见 §3）；跨会话偏好仍需外部存储 |
| 权限按人过滤 | 已实现：`principal.py` 的角色过滤视图进 catalog/tools，护栏侧仍按完整目录判权（见 §5.2）；多 principal 同时在线需外部会话态 |
| 可观测性 | 将 `tool_trace` 接入 OpenTelemetry；记录 prompt hash、模型版本、token 用量 |

---

## 10. 性能与并发

- 整个 Agent 是同步实现，单进程内可并发调用（每次 `run` 独立 messages/trace；SQLite 连接默认每次新建，`NL2SQL_DB_POOL_SIZE>1` 时按池上限复用，见 §12.1）。trace 这一半有测试：`test_bind_trace_id_isolates_threads` 用 4 个 worker 跑 8 条并发链，要求 8 个 `trace_id` 互不相同、`turn` 行的 id 集合与之相等——contextvar 的绑定是每线程独立的，一条链的日志不会串到另一条上。池化后的跨线程句柄也是测试而不是断言：`test_pooled_sqlite_serves_other_threads` 用 4 worker / 6 查询验证 `connects ≤ 2`（`check_same_thread` 在池化时放开，独占权归池）。
- **同一会话串行、不同会话并行**（P2-3，2026-10-23）：`SessionStore` 的 store 锁只保护 `OrderedDict`，每个 `session_id` 另有一把 `threading.Lock`；`ask` 在会话锁内完成"读历史 → 跑 Agent → 追加轮次 → 写 `api` 事件"。`DELETE /api/sessions/{id}` 调用 `reset()` **原地替换 `Conversation` 并保留锁对象**——若删掉旧锁再建新的，一个已排队等待的请求会拿到新锁并越过清空。FastAPI 同步端点在线程池里执行，因此不同会话并行不需要任何异步改造。语义由 HTTP 层测试钉住：`test_same_session_turns_are_serialized` 让第一问卡在 fake `run` 里，断言第二问在释放前不开始、最终轮次顺序为 `["第一问","第二问"]`；`test_distinct_sessions_run_in_parallel` 让两个会话在 fake `run` 内到达 `threading.Barrier(2)` 并都返回 200。串行只保护历史读写，单轮结果不变。
- **模型调用的重试与熔断**（`resilience.py`）：只包装真实 provider；`mock` 不包装，因为它没有网络故障模式，套一层只会给离线测量增加没有测量覆盖的计时代码。瞬时判定沿 `__cause__` / `__context__` 链检查 `TimeoutError` / `ConnectionError`、SDK 类型名、`status_code ∈ {408,409,425,429,500,502,503,504}` 与 `LLMError` 文本标记；`400` 类绝不重试。默认重试 1 次（总尝试 2）、退避 2s 起翻倍封顶 30s。熔断按**尝试**计数而不是轮次：一次重试失败贡献 2 次失败。默认连续 5 次打开 30s；半开态只放行一个探针（`_probing` 布尔位），成功关闭、失败立刻重开；阈值 0 关闭。打开时抛 `LLMError`，Agent 把它落成 `status="failed"` 的正常结果，异常不穿透 HTTP 层。`/api/health` 的 `llm_resilience` 每请求读一次实时快照（mock 返回 `enabled=false` + 原因），因为熔断状态随时间变化，而日志开关不是。
- **SQL 只重试 `pool_unavailable`**（`sql_tools.py`）：连接池耗尽/关闭是运维事实，只读查询重复一次安全；`sql_error` 是模型的问题，`timeout` 重试只会更慢。默认 1 次、间隔 50ms，触发时结果带 `attempts`。**没有 SQL 自动熔断**：现有错误码分不开"SQL 写错"与"连接层故障"，也没有实际 PG / SQL Server 样本可校准；在分不清的输入上再挂门，会把模型的 SQL 错误升级成服务不可用。这是规划中"SQL 执行设熔断"的刻意偏差，理由记录在 IMPROVEMENTS P2-3。
- **经验库并发写**（`memory/experience.py`）：`initialize()` 执行 `PRAGMA journal_mode=WAL`（持久属性；只读/网络文件系统上失败时保留原日志模式而不是让启动失败），每条连接 `PRAGMA busy_timeout=10000`。WAL 让读者不阻塞写者，`busy_timeout` 把写者相遇从立刻 `database is locked` 变成最多等 10s。测试覆盖 8 线程 × 5 次写入、读线程穿插 60 次写入、rollback-journal 旧库重开切 WAL 且行数不变。
- Schema 每次从 JSON 加载（10 张表规模可忽略）；真实大 schema 建议启动时缓存并热更新。
- SQL 执行有 `max_rows` 与按方言的语句超时（SQLite 客户端 `progress_handler` deadline / SQL Server `LOCK_TIMEOUT` + `conn.timeout` / PostgreSQL `statement_timeout`，实测中止精度 150ms → 150.12ms）；LLM HTTP 超时由 `NL2SQL_REQUEST_TIMEOUT_SEC` 控制（默认 120s）。执行层延迟单独进了 `metrics`（`执行层延迟(ms)` 只算 `db` 事件，不含模型耗时），否则"池化有没有用"在观测里是读不出来的。
- **结果缓存默认关闭**（P2-5，`cache.py`）：默认 `NL2SQL_RESULT_CACHE_TTL_SEC=0`，缓存既不读也不写；打开后键包含归一化问题、schema hash、principal、`allow_sensitive`、策略指纹与会话前缀哈希，只有 `status=ok` 且带已执行 SQL、无错误的轮次才入缓存。命中返回深拷贝：`cached=true`、`tokens=0`、`steps=0`、`tool_trace` 为空、`trace_id` 重新铸造，并报告 `saved_steps` / `saved_elapsed_ms`。`cache-check` 实测冷跑 `steps=6 / 8.14ms`、热跑 `steps=0 / 0.07ms`，归一化变体命中、不同问题不命中，`entries=2 / hits=2 / misses=2`。评测器始终 `use_cache=False`——缓存不可能移动任何评测数字。边界是**单进程内存 LRU**：多 worker 各持一份，没有跨进程失效。
- **边界**：并发只在单进程 + FastAPI 线程池里验证；多 worker / 多进程、跨进程会话、经验库多进程迁移均未测。重试/熔断的故障注入全部来自假 provider，没有对真实端点制造 429/5xx，也没有量过重试的额外 token 成本。在线模型基线在加这层包装之前跑，未重跑。

---

## 11. Web 控制台

`web/app.py` 的 `create_app(runtime)` 把同一个 `Runtime`（catalog / guard / agent / experience store / settings）直接挂到 HTTP 上。定位是**同一套 runtime 的薄壳**：除了会话问答、跑分与反馈审核三处，其余都是只读展示；它不复算任何指标，也不提供绕过 `SQLGuard` 的执行入口——反馈审核用的 validator 就是 CLI 结案那一条（护栏 + 真实只读执行）。

### 11.1 分层

```text
浏览器（static/index.html + app.js，原生 ES，零构建）
   │ fetch /api/*
   ▼
FastAPI（web/app.py）
   ├─ SessionStore：进程内 OrderedDict + 每会话 threading.Lock，上限 50，超出 LRU 淘汰
   ├─ /api/sessions/{id}/ask → agent.run(question, history=Conversation)
   ├─ /api/sessions/{id}/feedback → store.record_feedback（问题与 SQL 取自服务端轮次）
   ├─ /api/feedback/{id}/review → store.review_feedback（guard + 真实只读执行）
   ├─ /api/eval/run        → evaluate(...)，threading.Lock 非阻塞获取，冲突返回 409
   ├─ /api/cost            → cost_summary(read_events(...), prices=settings.price_table)
   └─ /api/* 只读探针      → catalog.schema / search_tables、experience、failures、feedback、reports
```

### 11.2 会话边界

会话状态只存在于单进程内存（`session_id` → `Conversation`），不落 SQLite：一方面避免多进程一致性问题，另一方面保证 Web 侧与 `chat` CLI 走的是同一份 `Conversation` 语义。`ask` 返回 `{result, conversation}`——`result` 是 `AgentResult.to_dict()` 全字段（`effective_question / rewrite_source / clarification / sql / columns / rows / tool_trace / confidence / steps / tokens / cached / cache`），`conversation` 是轮次与槽位快照。前端因此可以在客户端保留完整结果集，而不需要后端二次暴露查询能力。

并发边界（P2-3）：store 锁只保护字典本身，每个会话另有独立的 `threading.Lock`，`ask` 全程持锁；`reset()`（`DELETE` 端点）原地替换 `Conversation` 但复用同一把锁，避免"清空"与"排队中的提问"交错。不同会话在 FastAPI 的同步线程池里并行。测试从 HTTP 层验证了两条语义（同会话串行、异会话并行，见 §10），没有引入异步 Agent 或跨进程会话。反馈提交走同一把会话锁：它读取的是锁内保存的最近轮次，若与 `reset()` 交错，读到的可能是另一份历史——"用户投诉的答案"与"服务端记录的答案"必须一致。

### 11.3 安全 posture

- 密钥不出现在任何响应里：`/api/health` 只给 provider、模型名、prompt 版本、schema hash、角色 id 与 AST 开关、用例数、会话数、库路径、**池状态**（`pool: pool_stats()`，空列表就是"池未开启"这一事实本身；条目里只有 `target`（`describe()` 那一层标识）、上限、当前大小、`connects/checkouts/reuses/discards`，没有主机与账号）、**模型重试/熔断状态**（`llm_resilience`：`enabled / retries / backoff_sec / circuit{state, consecutive_failures, failure_threshold, reset_timeout_sec, retry_after_ms}`；mock 没有包装，返回 `enabled=false` 与原因）、**结果缓存统计**（`result_cache`：`enabled / ttl_sec / entries / hits / misses / hit_rate / expired / evictions`，关闭时 `enabled=false`）与**价格是否配置**（`pricing: {configured, currency}`，不包含单价），`api_key` 永不下发。角色 id 只是"这份视图属于谁"的标签（没有它，那个哈希无法解释），列级规则本身不从接口下发。健康面每次请求都读熔断快照与缓存统计，因为它们会随时间变化；日志是否开启则不会。
- `/api/cost` 是只读折叠：没配日志返回 `enabled=false` + 原因，配上但文件不存在也一样；报告里 `estimated_cost=null` 表示"价格未配置"，不是 0。Web 控制台的"成本看板"页面只渲染这份报告（token 四类拆分、按业务域/用户分桶、缓存命中），不自己算指标。
- 报告下载 `GET /api/eval/reports/{name}` 双重校验：文件名必须匹配 `^[\w.\-]+\.json$`，且解析后的父目录必须等于 `reports/`；`.env` 与 `..%2F.env` 均被拒。
- 经验与失败队列接口只读；失败结案仍必须走 `nl2sql-agent experience resolve`。**反馈审核有 HTTP 入口**，但它与 CLI 共用同一个 validator（护栏 + 真实只读执行），服务端也拒绝没有 `turn_index` / 没有 SQL 结果的轮次，客户端无法伪造一条"从未展示过的答案"去投诉；这不复制规则，规则只在 `review_feedback` 一处。
- 反馈内容按需落库，但周报默认只出指纹；`/api/feedback` 返回的是审核需要的问题原文与被否定的 SQL（本地单机控制台，不上传外部服务），`api_key` 与库连接信息仍不出现在任何响应。
- Judge 未配置凭证时返回 400 并在前端明确显示"未启用"，不静默降级。

### 11.4 依赖隔离

`fastapi` / `uvicorn` 只出现在 `[web]` extra，`pyodbc` 只在 `[mssql]` extra，`psycopg[binary]` 只在 `[postgres]` extra，`sqlglot` 在 `[ast]` 与 `dev`，`httpx` 只在 `dev`；`web/__init__.py` 不 import fastapi，`tests/test_web_api.py` 顶部 `importorskip`，`tools/db.py` 的 pyodbc 与 psycopg 都是**函数内懒导入**（PostgreSQL 先试 `psycopg` 3、再退 `psycopg2`，两个都没有才报错，且报的是带 `pip install -e ".[postgres]"` 的 `driver_missing` 工具结果，不是栈追踪）。因此未装这些可选依赖时核心 CLI 与其余测试照常运行，mssql 分支靠注入 fake pyodbc 模块覆盖成功/截断/超时/缺驱动四条路径，postgres 分支同样用注入的 fake `psycopg` 覆盖契约、会话语句顺序、参数省略、psycopg2 回退与缺驱动，`create_runtime` 的连通性自检也用 monkeypatch 的 fake 覆盖。**新增的 35 条后端/方言测试在四种安装形态里全跑**（它们不需要任何驱动，也不 import fastapi），所以执行层不会因为换安装形态而失去覆盖；池化的另一项测试挂在 `test_web_api.py` 的 `/api/health` 上，随 `[web]` 装没装一起 skip。前端资源通过 `[tool.setuptools.package-data]` 打进包，`pip install` 后 `nl2sql-agent serve` 即可用，无 Node 构建、无 CDN、断网可用。

**可选依赖决定"测到了什么"，所以测量本身必须自报覆盖面**（测试列为 2026-10-23 schema 映射层本轮复跑；同一台机器用 `sys.meta_path` 拦截导入模拟安装形态，本机 sqlglot 30.24.0；仪器列沿用同日 P2-6 / P2-5 / P2-4 / P2-3 / P2-2 / P1-4 的复跑，映射层没有重跑仪器）。映射层与它暴露的护栏又加了 16 条测试（`test_schema_overlay` 5 / `test_schema_suggest` 4 / `test_schema_review_cli` 3 / `test_agent` 的断言与 nudge 5；都不需要 Web 面或 AST，也不连库不调模型），按同一矩阵跑：

| 安装 | 测试 | `guard-check` / `dialect-check` |
|---|---|---|
| `.[dev,web]`（CI 主 job） | **753 passed** | AST 层参与测量：对抗 31/31、权限拦截 17；`dialect-check` 可比 22 条、AST 拦错方言 22 条但**独占 0 条**（仪器列为 P2-5 同日读数） |
| `.[dev]`（CI 旧配置） | **722 passed / 5 skipped**：整个 Web 控制台面不参与门禁（5 个 Web 测试模块整体跳过，含 P2-5 的 3 条成本看板测试） | 同上 |
| 只藏 `sqlglot`（`.[dev,web]` 减 AST） | **733 passed / 20 skipped** | `dialect-check` 退出码仍 0，gold 解析 22 条全部"未测量"、可比只剩 3 条原生（一致 3/3）、AST 拦截列降到 **0**——但**错方言形状仍拦 22/22**，因为这一维现在补在正则层；3 条反引号用例**也仍然拦得住**（同理）。`guard-check` 的 `ast_only_blocked` 从 3 条变 `[]`、权限**仍是 17** |
| `pip install -e .`（README 快开始的零依赖装法） | **702 passed / 25 skipped** | 退出码仍 0，但 `ast_available=false`、`ast_measured=false`、对抗只报正则 25/25、`gold_sql.ast` 退化成 0/0 占位；权限拦截**恒为 17**（`FROM[internal_audit]` 在方言引用规则落地后正则层自己看得见，不再需要解析器） |

**测试不联网是钉死的，不是"碰巧没网"**：`Settings.load()` 会把仓库根的 `.env` 用 `setdefault` 折进 `os.environ`，所以开发者把 provider 配成在线模型之后，一条走 CLI 子进程的用例真的挂住了整个会话（>300s 无输出，因为它在等一次 120s 超时的 HTTP）。`tests/conftest.py` 的 `DEMO_ENV` 现在连 `NL2SQL_PROVIDER` / `_API_KEY` / `_BASE_URL` / judge / embedding / overlay / 灰度比例一起钉死（外加 `OPENAI_API_KEY`、`DASHSCOPE_API_KEY` 清空），suite 无论本机怎么配都只走离线规则通道；`serve` 仍照 `.env` 用在线模型。

**新仪器要一起过这一关**：`alias-check`、`clarify-check` 与 `metrics` 在把 `sqlglot` / `httpx` / `pyodbc` / `psycopg` / `fastapi` / `uvicorn` **全部**藏掉后**逐位不变**（澄清实测仍是对照 13/可点选 4→13/裸澄清 9→0/选项 8→67，值对齐仍是修好 10、改坏 0；`metrics` 对同一份 825 行日志两次给出完全相同的 avg **4.37** / p50 **4.19** / p95 **7.18** / 成功 0.7236 / 经验命中 0.0244 / 物理连接 225 / 池化调用 0 / 执行层 avg **0.34**），退出码同为 0。这不是巧合——这几层判的是策略、取值与已经落盘的事件，不解析 SQL；把它们放进这张表是为了让"哪天准确率/策略数字开始依赖可选依赖"变成看得见的红。可观测性文件本身在零依赖形态下 31 条用例 **29 passed / 2 skipped**（跳过的两条是 Web 面）。`psycopg` 在这一列是新加的：这台机器上它本来就 import 不到（`ModuleNotFoundError` 未经拦截即成立），所以"藏掉 psycopg"与"没装 psycopg"是同一件事，PostgreSQL 那 9 条执行与配置测试在零依赖形态里靠注入 fake 模块照常跑。

中间那一行是个真缺陷而不是"少跑点测试"：Web 控制台在 README 里是功能，却因为在 CI 上 import-skip 而长期没有门禁。主 job 现在装 `.[dev,web]`，并新增 `core-only` job 跑零依赖路径，用断言钉住降级必须**自己承认**——`gold_sql.ast.measured is False`、`regex.measured is True`、权限 17、`failures` 全 0。`GoldAudit.measured` 就是这个布尔位：没有它，一个没测的 AST 审计块和一个干净的 AST 审计块在 JSON 里长得一模一样。**权限那一位以前是 16/17 随解析器跳的**，2026-10-23 起恒为 17，CI 里 `== 16` 的断言随之改成 17 并注明"再出现 16 就说明方言引用规则退化了"——这是"覆盖面断言会自己过期"的一个实例。

**准确率数字不随安装形态变**（同一台机器把 `sqlglot` / `httpx` / `pyodbc` / `fastapi` 全隐藏后复跑 123 条）：7 项受门禁指标逐位与装齐时相同（EX 0.4602、valid 0.7788、拒答 0.90、recall@5 0.94…），只有 `avg_latency_ms` 从 2.16ms 变到 1.54ms——这正是延迟不进回归门禁的原因，也是 `core-only` job 现在也跑一遍 `--fail-on-regression` 的理由：如果哪天某个准确率指标悄悄开始依赖可选依赖，红的是 CI，不是等到有人换台机器才发现。

---

## 12. 外接后端：连接、introspection 与数据边界

demo 只有 9 张表，任何在 demo 上得出的"够用"结论都不算数。这一层的全部意义是让后端、目录和 linking 都能对着真实企业库跑，并把结果变成可复现的数字。

### 12.1 后端抽象收在一处

`tools/db.py` 的 `Target` 是 `SqliteTarget`（`file:` URI，`mode=ro`）、`MssqlTarget`（pyodbc DSN 拼装）或 `PostgresTarget`（libpq 关键词串，`sslmode` 可配）。上层只认三件事：

- `open_connection(target, timeout_ms) → (conn, dialect)`：SQLite 设 `settimeout`，SQL Server 设 `conn.timeout`（秒级向上取整），PostgreSQL 在 autocommit 下跑完会话语句再关回 autocommit。
- `query(...) → {success, columns, rows, row_count, truncated, error_code, elapsed_ms}`：三个后端都用 `fetchmany(max_rows) + fetchone()` 判截断，多取一行而不是数行数。
- `dialect_sql(...)`：`TOP (n)` vs `LIMIT`、`[标识符]` vs `"标识符"`、`CAST(... AS NVARCHAR(4000))` vs `CAST(... AS TEXT)`。

**方言只有一处定义**（`dialects.py`）。同一件事以前写在四个地方——prompt 的 `if mssql / else SQLite` 两条分支、`guard._DIALECT_ALIASES`、`ast_guard.map_dialect`、`tools/db.py` 的语句拼装——所以目录声明 `dialect: tsql` 时，护栏按 T-SQL 规则拒绝，prompt 却告诉模型"这是 SQLite、记得加 LIMIT"。现在 prompt、双通道第二意见、guard、AST 层与执行器都从同一张登记表取 `label / row_limit_hint / forbidden / quote / bind / text_cast / row_cap / session_setup`。两个刻意留下的分裂：`channel_label` 单独一列，因为第二意见提示语里的 `SQL Server (T-SQL)` 是 P3 投票那批数字的测量条件，统一表格不能顺手改掉它；`sqlite`/`tsql` 两条 prompt 行按**逐字节相同**钉在测试里，所以这次收口对已发表的准确率与投票数字是零影响，而不是"应该没影响"。**未知方言不再被猜成 SQLite**：`normalize()` 对不认识的名字返回空串，`unknown(raw)` 回显目录自己写的标签、护栏一条规则都不设——把 `oracle` 读成 `sqlite` 会拦住正确 SQL。

同一张表还生成**值检索语句**（`Dialect.distinct_values()`）：`TOP (n)` / `LIMIT n`、`CAST` 类型、标识符引用与占位符风格都取自登记表，而搜索词**永远是绑定参数**、从不内联——否则值对齐工具会在模型 SQL 旁边再开一个注入面。执行结果一律带一个失败码而不是笼统的"SQL 有错"：`db_not_found`（路径不存在 / 连接项缺失）、`timeout`、`sql_error`、`driver_missing`（带装哪个 extra 的提示）、`pool_unavailable`。分这一位是因为 repair 与失败队列按错误文本决定改不改写：把"没有连接"混进 `sql_error`，模型会被推着重写一条本来正确的 SQL，白烧一轮工具调用。

`describe()` 是**唯一允许出现在日志、`/api/health`、报告元数据里的库标识**，形如 `mssql:<database>` / `postgres:<database>`，结构上就拿不到主机、账号、口令。

超时按方言来，顺序由 `Dialect.session_setup()` 单点给出：SQLite 没有服务端语句超时，用 `set_progress_handler` 做客户端 deadline（每 1000 个 VM step 检查一次）；SQL Server 是 `SET LOCK_TIMEOUT {ms}` + `conn.timeout`；PostgreSQL 是 `SET default_transaction_read_only = on` 然后 `SET statement_timeout = {ms}`。**先只读、后超时**这个顺序是被测出来的，不是排版：测试断言会话语句列表的顺序与 `{ms}` 的下限（最小 1ms，不给 0）。客户端 deadline 在这台 Windows 上的实测精度：`timeout_ms=150` 的递归 CTE 在 **150.12 / 150.32ms** 中止，墙钟与设定值一致（0.100s / 0.200s），没有"超时了但语句还在跑"的尾巴。

**连接池默认关闭**：`NL2SQL_DB_POOL_SIZE=1`（默认）时 `configure_pool()` 返回 `None`，执行器逐查询建连——published 的评测与延迟数字都是这条基线上测的，A/B 之前不需要重跑。开启后三条不变量比吞吐重要：归还走 `rollback()` 而**绝不 commit**（池里的句柄永远不该是写入发生的地方）；语句报错或被中止的句柄**丢弃**而不是还池（PostgreSQL 的失败事务和断掉的 TDS 管道都会毒化句柄）；池满返回 `error_code=pool_unavailable` 而不是 `sql_error`——把"没有连接"报成"SQL 有错"会让模型去改写一条本来正确的语句。`pool_for/configure_pool` 与 `query()` 一样接受裸路径：按 `str(path)` 注册的池和按 `SqliteTarget.describe()` 查的池若不同名，池化会配置成功、上报正常、然后永远不被使用。

实测（`var/demo_erp.db`，冻结集 **113 条 gold × 3 遍 = 339 次执行**，`mock` 不参与、不调模型，`NL2SQL_LOG_PATH` 打开，2026-10-23 独立跑两遍，连接数逐位相同、延迟差 <0.1ms）：

| 并发 | 池上限 | 物理连接数 | 池化调用 | 执行层 p50 / p95 (ms) | 墙钟 (s) |
|---|---|---|---|---|---|
| 1 | 关（=1） | **339** | 0 | 0.25 / 0.45 | 0.148 |
| 1 | 4 | **1** | 339 | 0.05 / 0.19 | 0.049 |
| 8 | 关 | **339** | 0 | 0.89 / 3.42 | 0.085 |
| 8 | 4 | **4** | 339 | 0.68 / 2.88 | 0.066 |

三个读数都能用 `nl2sql-agent metrics --log <事件文件>` 直接复跑（`物理连接` / `池化调用` / `执行层延迟(ms)`）。诚实边界写在数字旁边：本地 SQLite 建连只要 ~0.2ms，所以这张表量到的是**结构事实**（物理连接从 O(查询数) 压成 O(池上限)）加一个小收益下界；真实服务端建连含 TCP + 认证 + 会话设置，差值只会更大，但本机没有可连的 PostgreSQL / SQL Server，**服务端上的建连节省未测**。同理，超时后句柄被丢弃这条也在表外：4 次调用（2 次超时）实测 `connects=3 / discards=2`，即一次超时多付一次重连。

上面那张表出自一个自定义脚本，所以同一件事又用**发布出去的命令**量了一遍：`NL2SQL_LOG_PATH=… nl2sql-agent eval`（123 条、单进程顺序、225 次执行）默认档位得 `物理连接=225 池化调用=0`、执行层 avg 0.34 / p95 0.58ms；只加 `NL2SQL_DB_POOL_SIZE=4` 得 `物理连接=1 池化调用=225`、执行层 avg 0.13 / p95 0.29ms，turn 侧 avg 4.37 → 4.01ms。**七项 eval 指标逐位不变**（EX 0.4602、valid 0.7788、拒答 0.90、recall@5 0.94）——池只换连接、不改 SQL 与结果集，这一条如果不成立，就说明池把事务状态带进了下一条查询。

`create_runtime` 在 `mssql` / `postgres` 后端上先做一次 `SELECT 1` 连通性自检：连不上就立刻退出并给出可执行的排查提示（缺驱动时说清装哪个 extra，连不上时说清核对 `NL2SQL_MSSQL_*` 还是 `NL2SQL_PG_*`，消息里只有 `postgres:<database>` 这一层标识）。没有这一步，一次不可达的 `eval` 会表现成"40 条全失败但 `valid_sql_rate 1.0`"，把连接问题伪装成模型问题——这是本地实测踩出来的。`NL2SQL_DB_BACKEND` 也在配置层就拒未知值（`sqlserver`/`pg` 一类别名先归一）：写错一个字母就退回演示库，量出来的是一份看起来完全正常的假准确率。

### 12.2 introspection 的取舍

`schema-sync` 用四条 SQL 读 `sys.objects`（表/视图）、`sys.columns` + `sys.extended_properties`（列与 `MS_Description`）、`INFORMATION_SCHEMA.TABLE_CONSTRAINTS`（PK）与 `sys.foreign_key_columns`（FK），产出与 `demo_schema.json` 同构的目录，`Catalog` 之后的一切（BM25、guard、few-shot、评测）不改一行。

- **关系有两个确定性来源**：SQL Server 外键，以及列注释里的 `[关联表:X,关联字段:Y,前置条件:Z]`。前者是库自己声明的结构事实，后者是 ERP 里更常见的人工提示；两者都进 `relationships` 并按 `(left, right)` 去重。本机测试库实测 **19 条 FK + 1188 条注释提示 = 1190 条去重关系**。解析器容忍全角逗号/冒号与多条件。没有外键、也没有注释提示的表，关系就是空的——不对列名做猜测。
- **`business_rules` 不由库内推导**：introspection 固定输出空列表；销售额、库存、在途等口径必须由人写进 schema JSON 的 `business_rules`，再让 `NL2SQL_SCHEMA_PATH` 指向该文件。Agent 只在有内容时把它注入 system prompt。
- **`前置条件` 只进 `filter_suggestions`**，不进 `required_filters`：后者是 guard 的硬约束（L4），未经人工核对就把注释里的字典类型条件强制上，会误杀本没有错的 SQL。同步输出只报条数，落文件供人核对。
- **启发式标注**：列名或注释命中电话/身份证/银行/薪资类词 → `sensitive`；表名命中日志/临时/副本/`Table_` 前缀自动建表类 → `disabled`（禁用表不进召回、被 guard L3 阻断）。
- **`sys.objects.type` 是 `CHAR(2)`**：pyodbc 返回 `'U '`，等值比较会让整张目录静默变成 0 张。这是"必须连实际引擎"的实证——mock 的假游标给什么形状就有什么形状，永远发现不了；现已 `.strip().upper()` 并加 `'U '` 回归测试。

### 12.3 数据边界（发布面）

| 内容 | 位置 | 是否进仓库 |
|---|---|---|
| 连接项（含口令） | `.env`（gitignore） | 否 |
| introspect 出的目录 JSON（真实表名/列名/注释） | `var/`（gitignore） | 否 |
| 模型草拟的映射候选（含真实表名与证据） | `var/schema_suggestions.json`（gitignore） | 否；且**没有任何代码路径把它读进提示词** |
| 人工批准后的映射叠加层 | `var/schema_overlay.json`（gitignore，`NL2SQL_SCHEMA_OVERLAY` 可指别处） | 否；只以别名/口径文本的形式进入 system prompt |
| 真实业务问法与 gold 表集合 | `var/`（gitignore） | 否 |
| 评测报告 | `reports/`（gitignore） | 否 |
| 事件日志（默认不含业务文本；开 `NL2SQL_LOG_INCLUDE_TEXT` 后含问法与 SQL） | `NL2SQL_LOG_PATH`，建议 `var/`（gitignore） | 否 |
| 后端、introspect、linking、方言等通用代码 | `src/` | 是 |
| 合成 demo 目录与用例 | `src/nl2sql_agent/data/` | 是 |
| 召回/准确率**聚合数字**（不含标识符） | `CHANGELOG.md`、`docs/IMPROVEMENTS.md` | 是 |

发布前自检口径：把外接目录里的表名与表注释逐个回查 `git ls-files --cached --others --exclude-standard` 的全部文件，要求命中 0；测试夹具一律用中性名（`Customer` / `Contract` / `BasicData` / `OperationLog` / `ProductionOrder`、`ContractAmount` / `PlannedStart` / `DictType`），不使用任何生产系统的模块前缀或缩写列名。


---

## 13. 可观测性：一条 trace_id 从接口追到数据库

规划给这一层的要求是四件事：trace_id 贯穿 API → Agent → 工具 → DB、记录模型版本/prompt hash/token/耗时/guard 拒绝原因、给出 QPS 与 P95 这类指标、敏感信息脱敏后入日志。四条都落地了，但让它们成立的是四个**约束**，不是四个功能：

**1. id 只在请求边界铸造，库内只继承。** `new_trace_id()` 只出现在 HTTP `ask`、CLI `ask/demo/chat`、评测器每条用例这几个边界上；`agent.run(question, trace_id="")` 走 `bind_trace_id(value or current_trace_id())`。所以直接当库用（`agent.run("…")`）时 `trace_id` 是空串、`to_dict()` 逐字节不变——评测与回归测试钉的就是这个不变量（`test_a_library_call_without_a_boundary_stays_deterministic`）。代价是显式的：`_cmd_ask` 忘了铸造，那一轮的工具行与 DB 行就落在 `""` 上追不回来，所以四个边界各有一条测试。

**2. 没有 sink 就没有输出。** `configure_logging(None)` 是 no-op；`nl2sql` logger 上没有 handler 时 `log_event` 直接返回，既不建文件也不往 stdout 印（`test_no_log_path_means_no_handler_no_file_and_no_output`）。默认状态是"这套东西不存在"，运营者不配 `NL2SQL_LOG_PATH` 就一个字节都不写。

**3. 脱敏按路径判，不按关键字猜。** `_segments()` 把 `rows[0].sql`、`channels[].api_key` 这类嵌套键拆成（根段, 叶段）两段，任一段命中 `DROP_FIELDS` 就整段丢弃，容器全被扣下时不留空壳。结果集与凭证**永远**不落盘；业务文本（`question` / `answer` / `sql`）默认也不落盘，只留 `*_fingerprint = sha256[:16]`，靠 `NL2SQL_LOG_INCLUDE_TEXT=true` 打开——留指纹是为了"同一个失败问法出现了几次"这个问题在默认配置下仍然可答。

**4. 它是读数，不是门禁。** `metrics` 的退出码只表达前置条件（没配路径 / 文件不存在 → rc 2），任何比率都不进 CI；`GATED_METRICS` 一个都没加。P95 写在报告里而不是门禁里：123 条单进程串行负载上的 P95 是一个样本的描述值，不是阈值。

**一处只在真机上看得见的缺陷**：这台 Windows 机器上 `time.get_clock_info("monotonic")` 是 `GetTickCount64, resolution=0.015625`，mock 一轮本来就只要 2–4ms，于是 `elapsed_ms` 成批地报 `0.0`，P95 也读到 0.0。所有耗时测量（`agent.py`、`tools/db.py`、`evaluator.py`）因此改用 `time.perf_counter()`。**这是对历史数字的偏离**：改动之前印出来的 mock 侧延迟被量化到 16ms 一档，在线模型那种秒级耗时不受影响。`assert result.elapsed_ms > 0`（而不是 `>= 0`）现在钉在测试里。

**实测（2026-10-23，演示目录 `dialect=sqlite / tables=10`，`provider=mock`）**：

| 读数 | 数值 |
|---|---|
| CLI 单条 `ask` | 7 行事件 = tool 5 + db 1 + turn 1，全部同一个 trace_id；`result.trace_id` 与之一致 |
| `eval` 123 条 | 825 行 = turn 123 + tool 477 + db 225，**坏行 0**、trace 数 123（每条用例一个 id） |
| 轮次延迟 | avg **4.37** / p50 **4.19** / p95 **7.18** / max **26.81** ms |
| 比率 | `status=ok` **0.7236**（89/123）、needs_clarification 0.2764、人工升级 **0.0000**、经验命中 0.0244、工具错误率 0.0000、DB 错误率 0.0000 |
| 口径可回溯 | `prompt_version=agent-v5`、prompt_hash **123 个不同值**（跟着问法走）、schema_hash **1 个**、model `RuleBasedLLM`、token 0（prompt / completion / cached / 未分类全 0，mock 无 usage 块） |
| 结果缓存 | 命中 **0**（评测器 `use_cache=False`，这一行是 0 才说明评测没被缓存污染） |
| 真实 HTTP 一次 | 客户端带 `x-trace-id: demo-trace-0001` → **8 行同一 id**（api 1 + turn 1 + tool 5 + db 1），`result.trace_id` 回显同一值，`GET /api/metrics` `lines=8`、avg/p50/p95 = **8.04 ms** |
| 写日志的代价 | 同一 40 条门禁子集：关 **avg 2.64 / p95 4.24** ms，开 **avg 3.25 / p95 4.81** ms（+0.61ms/轮，约 8 行 JSON）；相对在线模型一次秒级调用是噪声级 |

`status=ok` 的 0.7236 **不是 EX**：日志量的是"这一轮以什么状态结束"，EX 要拿结果集和 gold 比，只有 `eval` 出。两个数不同源，不能互相替换（同一批用例的 EX 是 0.4602）。

**未做**：① 没有 OpenTelemetry / W3C `traceparent`——`x-trace-id` 是原样采纳的自由字符串（只做 `^[\w:.\-]{1,64}$` 校验），跨进程与跨服务的链路传播没有，contextvar 只在单进程内有效；② 没有实时导出（Prometheus / statsd），`metrics` 是离线折线；③ 没有滚动与容量上限，长期运行需要外部 logrotate；④ 线程并发有测试（8 条链 8 个 id），但**多进程共享同一个日志文件没有验证过**（`serve` 目前是单进程 uvicorn），跨 worker 的行完整性与轮转都留给外部工具；⑤ token 通道由 scripted provider 测试钉住（`test_provider_usage_reaches_the_result_and_the_turn_row`），**在线模型的一轮 token 是否逐条落进日志尚未实测**；⑥ 延迟分布没有按类别/按角色切片。

---

## 14. 结果缓存与成本（P2-5）

**缓存的单位是整轮，不是一条 SQL。** `ResultCache.get` 命中后直接返回上一轮 `AgentResult` 的深拷贝，不再进入模型循环，也不再执行 SQL——所以它省的是一轮工具调用与一次真实查询，同时把"数据库行最多旧多久"交给 TTL。键（`cache_key`）包含：归一化问题、schema hash、principal、`allow_sensitive`、策略指纹（prompt 版本、few-shot / 召回 / 值对齐 / 澄清 / 改写 / 历史轮数 / 双通道 / `max_rows` / `max_repairs`）与会话前缀哈希。前四项防止答案跨目录、跨权限复用；策略指纹防止改了提示词或开关后还拿到旧策略的答案；会话前缀哈希防止多轮里同一句追问被套用到另一段上下文。只有 `status="ok"`、有 SQL、执行成功且无错误的轮次才 `put`——失败与澄清不入缓存，避免把一次瞬时故障固化。

命中结果是**可观测的重写**：`cached=true`、`tokens=0`、`steps=0`、`tool_trace=[]`、`trace_id` 重新铸造，另带 `cache={hit, age_sec, saved_tokens, saved_steps, saved_elapsed_ms}`；`turn` 事件写 `cached` 与 `cache_saved_tokens`，`metrics` 与 `cost` 因此能报"缓存省了多少"。默认 `ttl=0` 时 `get` 直接返回 `None`、`put` 直接返回，缓存对象仍然存在（`/api/health` 能看到 `enabled=false`），但行为与没有缓存逐字节一致。评测器显式 `use_cache=False`，所以 123 条 eval 的缓存命中恒为 0——这是评测数字可比的前提，不是巧合。

**费用只由部署给的价格算。** `costs.py` 不做任何供应商价格映射；`NL2SQL_PRICE_INPUT_PER_1M` 与 `_OUTPUT_PER_1M` 必须同时配置，否则 `Settings` 直接报错；`estimated_cost` 在未配置时是 `null`，CLI 印"费用未配置"，Web KPI 显示"未配置"。token 按 prompt / completion / cached / 未分类四类拆，cached 是 prompt 的子集（计算时夹到 prompt 以内），未分类按 input 单价计（**下界**），cached 未单独给价时按 input 单价计。这些规则都写在 `_cost()` 里并有单测钉住；真实账单以供应商为准。

**实测（2026-10-23，`cache-check` 与浏览器）**：

| 项 | 读数 |
|---|---|
| 结果缓存冷/热 | `ok / steps=6 / 8.14ms` → `cached=true / steps=0 / 0.07ms`，`saved_steps=6`、`saved_elapsed_ms=8.14` |
| 归一化与隔离 | 只改标点空格的问法命中；语义不同的问题不命中；统计 `entries=2 / hits=2 / misses=2 / hit_rate=0.5` |
| embedding 缓存 | `hash:256`、9 篇文档：第一次 1 次 provider 调用 / 1ms，第二次 0 次 / 8ms，`reused_without_embedding=true` |
| prompt 前缀布局 | 2205 字符、公共前缀 2090、稳定比例 **94.78%**、易变段 115、`few_shot_at_tail=true` |
| Web 成本看板 | 合成日志 3 轮 / 1800 token / 缓存 1/3：`0.005775 USD`；`sales 0.004575 / inventory 0.001200`；`alice 0.004250 / bob 0.001525` |

**边界**：① 缓存是单进程内存 LRU，多 worker 各持一份、没有跨进程失效；② 命中率只在 `cache-check` 的两次问答上量到 1/2，不是生产命中率；③ `mock` 不产生 usage，token 四类拆分、费用字段与 `cached_tokens` 解析由 scripted provider 测试钉住，**在线模型的 token 落盘与供应商前缀缓存命中尚未实测**——prompt 布局只证明"给了命中的条件"；④ 费用是估算，未分类按 input 计价是下界，cached 未给价时按 input 计价；⑤ 缓存与成本默认关闭、不进 CI 门禁。

## 15. 灰度发布与版本回滚（P2-6）

**分桶是纯函数，不是随机数。** `gray.bucket(key, salt)` 取 `sha256(salt + ":" + key)` 前 8 位十六进制再 mod 10000，`in_gray` 用 `round(percent × 10000 / 100)` 作阈值；`percent=0/100` 直接返回，不参与取模。这样同一个 key 在同一进程、不同进程、每一轮都落同一臂——如果每轮重新掷骰子，同一段会话会在两版 prompt 间来回跳，指标差里混进上下文切换噪声。key 的选择按边界：Web 是 `session:<id>`、CLI `chat` 是会话 id，`ask` 与库直接调用回退为问题文本（同一问法稳定落同一臂）；评测器根本不走灰度，`--prompt-version` 直接强制单臂。`share()` 回传 `bucket/threshold/candidate`，日志里的 `gray_bucket` 让"为什么这轮走了候选"可审计。

**两版 prompt 只差 few-shot 位置。** `PROMPT_LAYOUTS` 是版本到布局的唯一映射：基线与候选共用 head / contract / few-shot 三段文本，只有拼接顺序不同；无 few-shot 时两臂输出逐字节相同。这保证灰度对照只测位置，不夹带别的改动。`AgentResult` 带 `gray_arm` / `gray_bucket`，`turn` 事件同步落这两个字段；结果缓存的键包含 prompt 版本，两臂的缓存条目不会互相命中。

**指标按版本切，EX 仍只从 eval 来。** `summarize` 生成 `by_prompt_version`，每个版本给 `turns / ok / needs_human / needs_clarification` 与三个比率；CLI `metrics` 只有在日志里出现两个以上版本时才多印一行 `灰度对比`，单版本日志不会凭空多出一行。这些是**状态比率**，不是执行准确率——EX 需要 gold 比对，必须用 `eval --prompt-version <arm>` 分臂跑；两条通道不能混着引用。

**发布快照是"可验证的本地制品"，不是远端发布系统。** `create_release` 在 `<home>/releases/<release_id>/` 下写 `manifest.json`、`schema.json` 与 `experience.db`：经验库用 SQLite 在线备份 API 复制（不是直接拷正在写的文件），三部分各自记 sha256；`release_id` 是 UTC 时间戳，冲突时加后缀，先写 `.tmp-<id>` 再 rename，避免半成品被 `list` 看见。manifest 同时记 prompt 版本/指纹/字符数、schema 的 `schema_hash` / `catalog_hash` / 方言 / 表数，以及非密配置（provider、model、principal、灰度比例与候选版本、数据库 `describe()` 级别标识）。`release snapshot` 只快照当前**基线** prompt，候选布局与灰度配置不进快照。

`rollback_release` 先校验快照内文件的 sha256，再校验 `release_id`（拒绝路径穿越），然后把**当前** schema 与经验库另存到 `var/release_backups/<id>-<stamp>/`，最后覆盖目标文件；`--skip-schema` / `--skip-experience` 不能同时给。prompt 只比对、不恢复：快照没有 prompt 正文，恢复方式是 checkout 对应代码版本并重启。因此 CLI 的退出码是三分：0 = 文件与 prompt 都一致；2 = 快照/参数错误或缺少 `--yes`；**3 = 文件已回滚但 prompt 仍不匹配**，stderr 明确说要 checkout，绝不把"文件回滚了"说成"版本回滚了"。

**实测（2026-10-23，`mock` + 演示库）**：

| 项 | 读数 |
|---|---|
| 10% 分桶 | 2000 个合成 key → **212 候选 / 10.6%**，桶值范围 3–9992，盐 `gray-v1`；`session:0042` 桶值 411 < 阈值 1000 |
| 分臂 eval | 基线与候选都是 61/123、EX **0.4602**、overall 0.4959、valid 0.7788、recall@5 0.94、825 行事件、坏行 0 |
| 双臂日志 | 4 轮 / 28 事件 / 20 工具 / 4 DB / 坏行 0；两臂各 2 轮、`ok=1.0000`、澄清 0、升级 0 |
| 快照 | `20261023T020912Z`：prompt `agent-v5` / `fc4c5d7017a801a1` / 2008 字符；schema sha256 `ef7d1a86…` / hash `6c6906abd54e99ed` / 10 表；经验库 1 条 verified |
| 回滚 | 经验库 1 → 写脏到 2 → 恢复为 1；备份目录 `var/release_backups/20261023T020912Z-20261023T020912Z/`；prompt 一致，`restored=true` |
| 测试 | 737 passed（+23：`test_gray` 14 / `test_release` 9），四种安装形态全绿 |

**边界**：① `mock` 的 `RuleBasedLLM` 不读 system prompt，两臂 EX 相同只证明"新布局没弄坏链路"，**不是** prompt 质量结论；真实 provider 的灰度 EX、升级率与延迟都没测；② 分桶比例是在合成 key 上量的，哈希分桶不是精确配额，真实流量的实际比例会围绕设定值波动；③ 快照只落在本地，没有远端制品库、签名、保留策略；④ 没有自动回滚触发器（例如 EX 跌破阈值自动切回基线），回滚仍是人工命令；⑤ GitHub Runner 的 CI 真实绿/红未验证（没有推送），只能写"本机矩阵通过"；⑥ 测量语料仍是 §0 所说的助手编写语料，不进冻结集、不进 CI 门禁。

---

## 16. Schema 语义映射层：草稿、人工闸、运行时叠加

§12 让 Agent 能连上外接目录，§4.3 让提示词只装结构，但两者都不回答**业务口径**："客户"到底是那张主数据表还是一张名字更像的视图，"联系电话"挂在哪个列上。这类判断不能由 introspection 得到（库里没有），也不能由模型自动写进目录（§0 的诚实性红线与 P8）。这一层的做法是把"模型可以猜"和"Agent 可以用"之间插一道**必须留名的闸**。

### 16.1 三段职责

| 段 | 入口 | 输入 | 输出 | 不可绕过的设计 |
|---|---|---|---|---|
| 草拟 | `schema-suggest`（`schema_suggest.py`） | 目录（链接打分取候选）+ 可选的 ≤5 行非敏感样本 | `var/schema_suggestions.json`，`status=draft`、`requires_human_review=true`、每条 `review_status=pending` | 模型只能在**给定的候选表与给定列**里选：`_normalize_suggestion` 丢弃目录里不存在的表名/列名、把置信度夹回 `[0,1]`、记录当时的 `schema_hash`。某个概念没有候选 / 调用失败 / 输出不是 JSON 对象，落成 `observations`（`no_candidate` / `llm_failed` / `invalid_model_output`）而不是编一条映射 |
| 批准 | `schema-review {list,show,approve,reject}`（`cli.py` + `schema_overlay.py`） | 候选文件 + 当前目录 | `var/schema_overlay.json` 的 `table_aliases` / `column_aliases` / `business_rules` + `approved[]`（审核人、备注、时间、目录 hash） | `approve` 必须给 `--reviewer`；**目录 hash 与草拟时不一致就拒绝**（`--force` 才越过，因为证据可能已失效）；`reject` 只改候选状态、overlay 不动；批准粒度是"一个概念一条"，没有批量通道 |
| 生效 | `runtime.py` | overlay 文件 | 合并进 `Catalog` | `apply_overlay` 用 `dataclasses.replace` 重建目录并丢弃派生索引（`_link_index` / `_link_key`），别名于是立刻参与链接打分；未知表/列忽略；**`schema_hash()` 不含别名**，所以加口径不会把经验库集体判陈旧 |

候选文件的 `id` 形如 `客户@customer`（概念 + 表名小写），是审核队列的稳定键；样本行只用来判断表的语义形状，**不写进任何落盘文件**（`profile_table` 明确剔除敏感列与禁用列，失败报 `error` 而不是空样本）。

### 16.2 这一层顺带暴露的护栏洞（`agent.py`）

原来的"没有执行就不能声称结果"是**单位白名单**（`_UNVERIFIED_CLAIM = \d[\d,.]*\s*(?:台|元|条|个|件|套|份|%)`）。注入口径后模型学会了一条新路径：0 次工具调用、直接给数——而中文量词是开放的（"家"就漏过去了）。现在的三处改动：

1. **触发条件换成状态而不是单位**：`status=ok` 且没有执行结果 ⇒ 答案里出现任何数字都替换为"尚未执行查询，没有可验证的数字结果。请补充问题范围后重试。"，置信度压到 ≤0.4。单位白名单保留，但只作为 `needs_human` / `refused` 路径的兜底。
2. **已有的两次 nudge 覆盖面放大**（仅真实 provider，`mock` 逐字节不变）：第 0 次工具调用就断言结论 → 复用 `_CLARIFY_NUDGE` 强制一次 `search_tables`/`get_table_schema`；查完 schema 仍不执行就断言结论 → 复用 `_EXECUTE_NUDGE` 要求 `validate_sql` + `execute_sql`。nudge 各只给一次，不形成循环；实测上表那条问数因此在第 4 次工具调用后给出可验证答案。
3. **提示词里两处把"数字的来源"写死**：JSON 契约的 `answer` 从"可包含关键数字"改成"其中的数字只能来自 execute_sql 的返回结果"；`业务口径` 块非空时追加一行"以上只决定用哪张表/哪个字段，不含任何数据"。

`agent-v5` 的版本号**没有**因为这两处措辞而 bump，依据就是 冻结集逐位不变——版本号描述的是布局与指令结构，而 123 条离线指标证明改动没有移动任何准确率指标。在线模型臂的基线数字仍是改动前测的，这一点写在 CHANGELOG 的边界里。

### 16.3 边界

① 草拟只在 `provider=openai` 下可用（`mock` 直接拒绝并说明原因），所以**没有**离线可复现的"草拟质量"数字（没有 precision/recall——需要人工标注一批"标准映射"才能算，这一批还不存在）；② 概念清单是硬编码的 12 个高频业务词，新领域要人工传 `--concepts`；③ 审核没有 Web 界面，队列是 CLI + 文件；④ 口径写错不会被自动发现——目录 hash 不变就不会重新问人，`approved[]` 里的审核人是唯一可追的线索；⑤ overlay 对**整份冻结集**在在线模型下的 EX 影响未测，本轮只跑了单条问数；⑥ 候选与 overlay 都含真实标识符，只留 `var/`（gitignore），P8 扫描要求 TABLE / TABLE COMMENT 为 0。
