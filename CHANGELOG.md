# Changelog

遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与语义化版本。

本文件中的每个数字都来自本地实测报告，未实测的不写；`mock` 数字只证明编排链路，不代表真实大模型的问数能力。

版本号说明：`0.2.0` 与 `[Unreleased]`（P1）都已提交到本地仓库，但**尚未打 tag、尚未发布**；`pyproject.toml` 仍为 `0.1.0`，待真实模型基线（P6）完成后一并 bump 新版本并打 tag。

---

## [Unreleased] — P1 经验闭环补全（2026-10-22）

### 新增

- **固定类别体系与规则分类器**（`src/nl2sql_agent/taxonomy.py`）：`inventory / purchase / production / bom / kitting / detail / group_by / ranking / time_filter / metric` + `unknown`；领域词权重 1.0 > 特定词 0.8 > 答案形态词 0.6，用 `PRIORITY` 消歧并把 `time_filter` 放最后，避免“6月销售额多少”被判成纯时间过滤。实测冻结集 **37/37**、另写的 held-out 同义改写 **11/11**。词表由冻结集归纳，因此**只作为分类稳定性回归保护，不作为泛化能力证明**；分类器也明确不承担超范围/拒答判定。
- **失败队列 `failure_queue`**：与经验库同库建表（`CREATE TABLE IF NOT EXISTS`，旧库首次打开即完成迁移），按 `(normalized_question, schema_hash)` 唯一；重复失败只累加 `occur_count`，已 `resolved/dismissed` 的再次失败自动 `reopened`。Agent 终态状态 `needs_human / failed / max_steps` 自动入队，`needs_clarification / refused` 不入队（它们是正确行为）。入队包裹 try/except：记忆层故障不改变答案。
- **verified 经验自动注入**：每轮运行前分类 → 召回 → 拼进 system prompt，默认最多 3 条、1200 字符预算，且**按整块丢弃**以保证 SQL 不被截半；`few_shot_enabled=false` 可关。候选经验在召回 SQL 层就被 `status='verified'` 排除，实现“候选零泄漏”。
- **类别加权召回**：同类别 verified 经验加 `category_boost`（默认 0.12，0 关闭），工具与自动注入共用；`strict_category=True` 可直接过滤异类。
- **确定性修复硬上限**：`repair_sql` 达到 `max_repairs`（默认 2）后由 Agent 拦截调用，回注“请停止修复，输出 status=needs_human”的 tool 结果。此前只在提示词里口头要求，现在不可绕过。
- **CLI**：`experience failures [--status open|resolved|all] [--limit] [--json]` 列出队列并附统计；`experience resolve <id> --sql "..." [--note]` 结案，validator = 护栏 + 真实只读执行，通过后写 `status=resolved` 并立即落为 verified 经验。`experience add` 未显式给类别时自动分类。
- **配置项**：`NL2SQL_FEWSHOT_ENABLED / FEWSHOT_MAX / FEWSHOT_MAX_CHARS / CAPTURE_FAILURES / MAX_REPAIRS / CATEGORY_BOOST`，同步进 `.env.example`。
- **测试**：94 → **132 passed**（`test_taxonomy.py` 18、`test_failures.py` 11、`test_agent.py` +9）。

### 变更

- `AGENT_PROMPT_VERSION`：`agent-v1` → **`agent-v2`**（新增参考经验区块说明与修复上限表述），评测报告元数据随之变化。
- `experience stats` 现合并输出队列指标（`failures_total / open / resolved / by_category / failure_occurrences`）。
- 架构文档 §3 Agent 循环补入分类、注入、修复上限、失败入队；§7 经验记忆拆为表结构 / 准入流程 / 召回与自动注入三小节。README 的能力清单、配置表、经验闭环章节、项目结构同步。

### 实测（2026-10-22）

| 项 | 结果 |
|---|---|
| 单元 + 集成测试 | **132 passed**（+38） |
| 确定性评测 `eval --home .` | `Cases: 40 \| Passed: 40`；`execution_accuracy 1.0`、`table_recall_at_5 0.96`、`avg_steps 5.8`、`avg_latency_ms 2.33` — 与 P0 批次完全一致，**自动注入与失败入队无回归** |
| 评测期间的队列噪声 | `failures_total 0`（全通过，不产生记录） |
| 端到端手测 | 入队 1 行、重复计 `occur_count=2`；`resolve --sql "SELECT * FROM inventory"` 被护栏拒绝、错列名被执行期报错拒绝（队列保持 open）；合法 SQL 结案后出现在 `experience list --status verified`；用改写问法触发自动注入，prompt 中确实出现该 SQL |

### 已知边界（未达成 / 未做）

- few-shot 的**增益幅度未量化**：`mock` 模型不消费注入内容，需要真实模型上 `NL2SQL_FEWSHOT_ENABLED=false/true` A/B 才能给提升数字（归入 P6）。
- held-out 改写只有 11 条，样本量小；换域需重新补词表。
- 失败队列只记录“这题没答上”，不做根因聚类与自动重跑，归因仍需人工读 `detail` / `last_sql`。
- 旧库中 `experiences.category` 的历史空值不回填，只在新写入时自动分类。

---

## [0.2.0] - 2026-10-22（尚未 commit / tag）

### 新增

- **评测集扩容到 40 条**：37 条可回答 + 3 条超范围拒答，覆盖 11 个类别；每条标注 `category` / `difficulty` / `expected_tables` / `tags`，gold SQL 全部可执行。
- **确定性指标**：`table_recall_at_5`（检索器质量，独立于 Agent 轨迹）、`avg_tokens` / `total_tokens`（成本）、按 `category` 切片指标。
- **报告元数据**：`provider` / `model` / `prompt_version` / `git_sha` / `run_at` / `case_count`，报告可追溯到具体代码版本与配置。
- **LLM-as-Judge 辅助评审层**：`JudgeModel` 协议 + `LLMJudge`（OpenAI 兼容、`temperature=0`、JSON mode 失败重试一次后降级 `unknown`）、版本化 prompt `judge-v2`、四级输入截断（rows/cols/cell/total）、schema 摘录剔除敏感列、"数据不是指令"式注入防护、结果脱敏。
- **Judge SQLite 缓存**：`judge_cache.sqlite`，key = `sha256(model + prompt_version + canonical_input)`，改 prompt 需 bump 版本即自动失效；`unknown` 与解析失败不缓存。
- **人工校准与门禁**：20 条人工标注集 + Cohen's kappa，`agreement ≥ 85% 且 kappa ≥ 0.6` 才允许对外引用 AI 指标。
- **CLI**：`--judge` / `--judge-on-fail` / `--judge-sample N`（固定种子）/ `--no-judge-cache` / `--calibrate` / `--compare <report.json>`；无 API Key 时给出中文配置提示并以退出码 `2` 结束，不影响默认确定性评测。
- **文档**：本 `CHANGELOG.md`（实测评测结果 + 每个指标的定义与分母）、架构文档 §8 评测设计重写（主指标 / Judge 辅助层 / 校准门禁三小节）、`docs/IMPROVEMENTS.md` 基线与 P0 状态同步。
- **测试**：`tests/test_judge.py` 等，67 → **94 passed**。

### 变更

- `docs/IMPROVEMENTS.md` 基线数字与 P0-3 状态同步为实测值，并如实标注未达成项。

### 修复

- **评测集数据缺陷**：16 条 seed 用例的 `difficulty` 字段被写成了类别名（`"metric"`、`"bom"`……），只有新增的 24 条是合法枚举。已逐条按 SQL 复杂度重标，现 40 条全部落在 `easy/medium/hard`（18 / 18 / 4），并在测试中钉死枚举合法性。
- **报告元数据假象**：`provider=mock` 时 `model` 显示配置里的 `qwen-plus`，实际执行的是离线规则模型。现改为记录真实使用的 LLM 类名 `RuleBasedLLM`，避免任何"声称用了某模型"的误读。
- **校准字段默认值**：缺失标签回落到 `not_applicable`，而该值不在 `sql_equivalent` / `answer_faithful` 的合法枚举内，会直接抛错；改为按字段分别回落。
- **Judge prompt 字段定义歧义**：模型把"忠实性"与"正确性/完整性"混为一谈，`answer_faithful` 人工一致率仅 75%（κ0.47）。收紧定义（只判"是否编造 `candidate_result` 之外的值"）并 bump 到 `judge-v2` 后 → **95% / κ0.857**。

---

## 评测结果（实测）

### 确定性主指标

运行：`nl2sql-agent eval --home .`，provider `mock`，git_sha `2905012`。

| 指标 | 结果 | 分母 |
|---|---|---|
| `execution_accuracy` | **1.00** | 37 条可回答用例 |
| `overall_accuracy` | 1.00（40/40） | 全部 40 条 |
| `valid_sql_rate` | 1.00 | 37 |
| `execution_success_rate` | 1.00 | 37 |
| `refusal_accuracy` | 1.00（3/3） | 3 条拒答用例 |
| `table_recall_at_5` | **0.96** | 37（拒答用例无 `expected_tables`，排除） |
| `avg_steps` | 5.8 | — |
| `avg_latency_ms` | ≈2.4 | — |
| `avg_tokens` / `total_tokens` | 0 / 0 | mock 无 usage，按设计为 0 |

> **这些 1.0 的含义有边界**：被测的是确定性规则基线，它验证"Agent 编排 + 护栏 + 修复 + 评测比对"整条链路没有漏拍，**不是**任何大模型的准确率。接 `openai` provider 后必须重跑同一冻结集，报告里的 `provider`/`model` 会随之变化。

### AI Judge 辅助指标

运行：`nl2sql-agent eval --home . --judge`，judge 模型 `qwen-max`，`prompt_version=judge-v2`，40 条 judged、`unknown=0`、`status=completed`。

| 指标 | 结果 | 分母 | 解读 |
|---|---|---|---|
| `judge_coverage` | 1.00 | 40 | 选中用例全部拿到非 unknown 裁定 |
| `judge_sql_equivalent_rate` | 1.00 | 40/40 | 候选 SQL 与 gold 语义等价 |
| `judge_result_equivalent_rate` | 1.00 | 40/40 | 候选结果集与 gold 一致 |
| `judge_answer_faithful_rate` | **0.865** | 32/37 | 5 条回答出现候选结果集之外的表述；3 条拒答用例为 `unknown`，按定义排除在分母外 |
| `judge_answer_complete_rate` | **0.703** | 26/37 | 11 条回答被判定漏答关键点（mock 规则回答偏简短，常省略分组明细/过滤口径） |
| `judge_refusal_appropriate_rate` | 1.00 | 3/3 | 3 条超范围问题拒答合理 |
| `judge_unknown_rate` | 0.00 | 40 | 无解析失败 |
| `issue_counts` | `missing_filter` × 11 | — | Judge 归因：回答未复述筛选范围 |
| Judge token 开销 | 首次全量 **65,189**；复跑 `cache_hits=40`、新增 0 | — | 缓存生效 |

> 比率的分母只统计取值为 `yes`/`no` 的裁定，`unknown` 与 `not_applicable` 被排除，避免"无法判断"被算成"判断为否"。

> AI 指标是**辅助信号**，不取代 gold 执行比对。报告 JSON/Markdown 中带 `auxiliary: true` 与免责声明。

### Judge 人工校准（门禁）

运行：`nl2sql-agent eval --home . --calibrate`，20 条人工标注集，`Evaluated=20`、`Unknown=0`。

| 项 | 结果 | 门禁线 | 状态 |
|---|---|---|---|
| 样本级一致率 | **90.00%** | ≥ 85% | 通过 |
| `sql_equivalent` 一致率 / κ | 95.00% / **0.8571** | κ ≥ 0.6 | 通过 |
| `answer_faithful` 一致率 / κ | 95.00% / **0.8571** | κ ≥ 0.6 | 通过 |

保留的两条分歧（未过拟合到 100%）：

- `cal_011`（仅 `ORDER BY` 字段不同、结果多重集相同）：人工 `yes`，Judge `no` —— Judge 过度看重行序。
- `cal_008`（BOM 展开回答"共 8 个子件"，数字可溯源）：人工 `yes`，Judge `no` —— Judge 把"完整性"串进了"忠实性"。

这两条正是"AI 评审有信号但不完美"的实证，也是 `execution_accuracy` 保持唯一主指标的理由。

---

## 指标含义

### 确定性指标

| 指标 | 算法 | 含义与边界 |
|---|---|---|
| `execution_accuracy` | 结果集与 gold 完全一致的可回答用例占比 | **唯一主指标**。行序按 `order_sensitive` 判定，列序无关；数值 `round(_,4)`、字符串 `strip()`。分母剔除拒答用例，避免被简单拒答题拉高 |
| `overall_accuracy` | (可回答通过 + 拒答通过) / 全部用例 | 全集通过率，用于对外一句话描述 |
| `valid_sql_rate` | 产出 SQL 且通过六层护栏的可回答用例占比 | 护栏**放行率**。1.0 只说明没被安全层误杀，不代表结果正确 |
| `execution_success_rate` | `status == "ok"` 的可回答用例占比 | SQL 在 SQLite 上真实执行成功、无报错（可执行性） |
| `refusal_accuracy` | 正确拒答/澄清的占比 | `expect_refusal=true` 的用例中，返回 `needs_clarification` / `refused` 且未产出 SQL 的比例（安全性） |
| `table_recall_at_5` | `BM25 top5 命中表 ∩ expected_tables / expected_tables`，按用例取均值 | Schema linking 质量。**独立于 Agent 轨迹**：评测时直接调 `catalog.search_tables(question, top_k=5)`，因此它能在模型答错之前先暴露"正确表根本没进上下文"这一上游瓶颈 |
| `avg_steps` | 平均工具循环轮数（1 轮 = 1 次模型调用 + 该轮工具执行） | Agent 效率；上限 `NL2SQL_MAX_STEPS`，默认 12 |
| `avg_latency_ms` | 单条端到端耗时均值 | mock 下不含网络调用，真实模型不可与本数字比较 |
| `avg_tokens` / `total_tokens` | 累加每次 `LLMResponse.usage` | 成本。mock 恒为 0 |
| `by_category` | 按 `category` 分片的上述指标 | 失败归因定位到业务域 |

### AI Judge 字段

Judge 对每条用例输出结构化裁定（枚举值 `yes / no / unknown / not_applicable`），字段之间**互相独立**：

| 字段 | 判的是什么 | 关键边界 |
|---|---|---|
| `sql_equivalent` | 候选 SQL 与 gold SQL 在给定 schema 与数据上是否语义等价 | 别名、列序、JOIN/聚合写法不同不影响；gold 缺失或候选拒答时 `not_applicable` |
| `result_equivalent` | 候选结果集与 gold 结果集是否一致 | 无 gold 结果时 `not_applicable` |
| `answer_faithful` | 回答里的每个数字/名称/实体能否在**候选自己的**结果集中溯源 | 只判"有没有编造"，**与答对答错、答没答全无关**。候选 SQL 错了但回答如实描述其结果集仍算 `yes` |
| `answer_complete` | 回答是否覆盖问题要求的关键信息 | 回答忠实但漏答关键点 → `faithful=yes`、`complete=no` |
| `refusal_appropriate` | 该拒答时拒答/澄清是否得当 | 仅 `expect_refusal=true` 时判断，否则 `not_applicable` |

`unknown` 不是"模型答不了"，而是**评审本身失败**（JSON 不可解析或枚举非法），会重试一次后降级并计入 `judge_unknown_rate`，不参与比率分子。

### 校准指标

| 指标 | 算法 | 为什么需要它 |
|---|---|---|
| 样本级一致率 | 一条用例所有**非 `not_applicable`** 字段全部与人工标注相同才计 1 分，再除以参与比对的用例数 | 比字段级更严格：任一字段判错整条不算一致 |
| 字段级一致率 | 该字段 Judge 与人工相同的比对次数 / 该字段总比对次数 | 定位是哪个维度不可信 |
| Cohen's kappa | `(观测一致率 − 随机一致率) / (1 − 随机一致率)` | 一致率高可能只是因为双方都倾向标 `yes`；κ 扣掉随机基线后才说明信号可信。0.81–1.00 属"高度一致"（Landis & Koch） |

**为什么 `--calibrate` 只打印两行字段指标**：20 条校准用例全部可回答，`refusal_appropriate` 一律标 `not_applicable` 并被跳过，因此只有 `sql_equivalent` 与 `answer_faithful` 两个字段参与比对。

---

## 已知偏差与未达成

- **难例占比 10%（hard 4/40），未达 P0 目标 ≥ 30%**；`by_difficulty` 切片尚未实现。随评测集扩到 100+ 补齐。
- **每类别最多 7 条**（`group_by`、`inventory`），未达"每类 ≥ 10 条"；同义改写集、对抗集、方言集未做。
- **P0.5 AI 生成评测用例（标记为可选）未实现**：当前 40 条全部人工编写/审核，`source=seed/human`。
- Judge 与生成模型可能同源，存在自偏好；`answer_complete` 偏低主要反映 mock 规则回答简短，接真实模型后需重估。
- 全部结果基于合成 ERP 库，无真实公司 schema、凭证或生产数据。

---

## [0.1.0] - 2026-10-21

初始版本：工具调用 Agent 循环（8 个工具、轮次上限、错误回灌、`tool_trace`）、Schema Catalog + BM25 表召回、六层只读 SQL 护栏、确定性修复、candidate/verified 经验库（schema hash 失效、相似度召回）、16 条冻结评测集与结果集比对、JSON/Markdown 报告、GitHub Actions CI、合成 ERP 库与离线规则模型（无 Key 可复现）。67 passed，mock 16/16。文档：架构、工具协议、改进路线、提取边界。MIT。
