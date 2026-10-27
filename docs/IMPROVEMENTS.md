# 继续改进路线

本文档区分 **已实现**、**下一步（P0/P1/P2）** 与 **明确不做**，每项给出验收标准，避免路线图停留在口号。

当前版本：`0.2.0`（2026-10；`pyproject.toml` 与 `__version__` 已随 P6 bump，**尚未打 tag、尚未发布**）

---

## 0. 当前基线

已经具备：

- 工具调用 Agent 循环（8 个工具、12 轮上限、错误回灌、trace）。
- Schema Catalog + BM25 表召回 + 字段级精读。
- 多层只读 SQL Guard（语句/表/维度/列/敏感字段/`SELECT *`/扇出 warning、**按目录方言的形状黑名单**（`TOP`/`LIMIT`/`::`/`ILIKE`/外来引号与拼接符；2026-10-23 P2-2 落在正则层，零依赖路径同样生效）），可选 `sqlglot` AST 结构层与按角色的行级/列级权限，默认关闭。
- 确定性修复（尾逗号、无效投影、GROUP BY 算术表达式）＋ 单题修复轮次硬上限（默认 2 轮，超限转人工）。
- 经验库 candidate/verified 准入、schema hash 失效、相似度 + 类别加权召回、verified 经验自动 few-shot 注入。
- 人工反馈闭环（P2-4，2026-10-23）：问数页三个反馈按钮 → 审核项 + 失败 + 反例（被否定的 SQL 不进候选经验）；`experience feedback/review/report` 与 Web 审核接口；通过必须过护栏 + 真实只读执行才写 verified，拒绝只撤本条投诉；周报默认只出 sha256 指纹，经验复用率取自事件日志窗口。
- 固定类别体系与规则分类器、失败问题自动入队去重、`experience failures/resolve` 人工结案闭环。
- 123 条冻结评测集（11 类 + easy/medium/hard 分层，另 10 条多轮）、32 条同义改写用例（gold SQL 与源用例逐字相同，`--paraphrase` 追加，不进 CI 门禁）、24 条对抗问法用例（7 家族：敏感列 / 禁用列 / `SELECT *` / 禁用表 / 系统目录 / 扇出陷阱 / 追问压力，`--adversarial` 追加，同样不进门禁）、22 条方言 gold 用例（8 意图 × 8 轴，`tsql`/`postgres`/`mysql`/`sqlite`，走独立命令 `dialect-check` 做护栏接受度与转译结果集等价审计，既不调模型也不进 `eval`，见 P0-3 与 P2-2）、结果集比对、`table_recall@5`、token 计量、按 category / 难度 / 失败责任层 / `generation` 子类型 / 改写配对 / **对抗四态（泄露 / 护栏漏检 / 护栏拦下 / 未尝试）与扇出掉坑率**切片、JSON/Markdown 报告、CI。
- 准确率回归门禁：`benchmarks/ex_baseline.json`（只含聚合数字，不含用例与 SQL）+ `eval --compare … --fail-on-regression`，7 项受门禁指标跌超 2pp 拦合并；provider 或语料规模不符时拒绝比对而不是硬做减法。
- LLM-as-judge 辅助评审 + SQLite 缓存 + 20 条人工校准集（一致率 90%、κ0.857，qwen-max；关缓存复跑逐字一致）。
- 真实大模型基线（`qwen-plus` / `qwen-max`：同一 40 条冻结集 + 50 条含多轮的实测 EX、token、时延、逐条失败归因、运行方差）与 AI 用例草拟 `eval generate-cases`（契约/护栏/执行三道关，草稿固定 `status="draft"`，评测器跳过不执行）。
- Web 控制台：FastAPI + 原生单页（问数会话、评测看板、Schema 浏览、经验/失败队列、反馈审核），同一 runtime 的薄壳——除会话问答、跑分与反馈审核三处外只读。
- 外接目录接入：`sqlite` / `mssql` 只读后端抽象、`schema-sync` 从系统视图 introspect 出目录（含 `sys.foreign_key_columns` 的外键 → 显式关系）、9 个可命名 linking 策略按目录规模自适应、T-SQL 方言 guard 与错误码路由修复。
- Schema 语义映射层（P1-7，2026-10-23）：`schema-suggest` 让模型在候选表/候选列的约束下**草拟**"业务概念 → 表/字段/口径"，`schema-review approve --reviewer` 由人批准后写进 `var/schema_overlay.json`，`runtime` 在读目录之后合并进 `Catalog`；未批准的候选没有任何路径进入提示词。它顺带暴露并补上了"0 次工具调用就断言数字"的护栏洞（见 P1-7 的状态段）。
- 双通道复核投票（默认关闭）：第二条独立通道另写 SQL 并真实执行，结果集一致才加分，不一致 100% 转人工。
- 护栏基准 `guard-check`（不调模型、不连库，CI 每次跑）：41 条对抗 + 23 条权限 / 5 角色 + 11 条扇出 + 113 条 gold SQL 误杀审计，分层给出"正则漏了什么、AST 多拦了什么、护栏误杀了什么"（10 条无 gold 的拒答用例计入 skipped）。
- 合成 ERP 库与离线规则模型，无 Key 可复现。

**基线数字仅证明链路正确**：mock provider 在 CI 门禁子集（`--only-tag v1_baseline`，40 条）上 40/40、执行准确率 100%、`table_recall@5` 96%；在全量 123 条冻结集上只有 61/123、EX 0.4602（规则通道只认识演过的形状）。两个数都不代表在线 LLM 在企业 ERP 上的准确率，不能写进简历当作模型效果。改写配对给出的脆性数同样如此：`mock` 在源问法通过的 12 对里被问法打倒 4 对（4/12 = 33.3%），这一列**结构上必然偏高**——规则通道按关键词匹配问法，对措辞的敏感度不代表在线模型，在线模型上的脆性要重跑才有。AI 指标为辅助信号，主指标始终是确定性结果比对。

---

## 1. 改进的衡量方式

任何改动先定义指标，再写代码：

| 指标 | 定义 | 用途 |
|---|---|---|
| Execution Accuracy (EX) | 结果集与 gold 完全一致的比例 | 主指标 |
| Valid SQL Rate | 产出 SQL 且通过 guard 的比例 | 护栏兼容性 |
| Execution Success Rate | SQL 能被数据库执行的比例 | 方言/语法质量 |
| Repair Recovery Rate | 首次执行失败后被确定性修复成功的比例 | 修复规则价值 |
| Refusal Accuracy | 超范围问题正确拒答的比例 | 幻觉抑制 |
| Paraphrase Agreement / Brittleness | 改写与源用例判定一致的比例；源用例通过而改写失败的配对数（`--paraphrase` 同一运行内配对） | 问法鲁棒性 |
| Avg Steps / Latency / Tokens | 效率与成本 | 产品可用性 |
| Human Escalation Rate | 转入人工的比例 | 自动化上限 |

评测纪律：

1. 评测集冻结，调 prompt 不允许改 gold；新增用例进入下一版本。
2. 报告必须记录 provider、模型名、prompt 版本、schema hash。
3. 失败用例按 **schema linking / 值对齐 / JOIN / 聚合口径 / 方言 / 权限 / 拒答** 分类，先修高频类，不追单例。
4. 任何“准确率提升”必须给出同集对比，不能只报最好案例。
5. 改写一致率不能单独读：两侧都失败的配对也计"一致"，会系统性偏乐观，必须和 `both_pass / both_fail / brittle / reverse` 与两侧各自准确率一起报。

---

## 2. P0：正确性与安全（先做）

### P0-1 用 AST 替换正则护栏

> 状态（2026-10-22）：**已落地，但没有"替换"，也不是默认开启**。正则是零依赖、读掩码语句的第一层，AST 是可选的第二意见（`pip install -e ".[ast]"` + `NL2SQL_AST_GUARD=on`）：解析器读歪一次就退回正则放行，等于给绕过留最便宜的门，所以 `ast_unavailable` / `ast_parse_failed` 一律按 error 处理（**fail-closed**）。对抗用例 **37 条 > 规划的 30 条**（2026-10-23 复跑时因量出反引号绕过而扩到 **41 条**，正则 25/25、正则+AST 31/31，见 P0-3 本节末尾条目），`guard-check` 实测（下面这组是 P5 当时口径，2026-10-23 扩容后的新口径见括号）：正则在自己作用域内 22/22（现 **25/35**）、加 AST 后 28/28（现 **31/31**）、漏拦 0，4 条只有解析器拦得住（少空格的 `FROM[`、函数式逃逸、不含 INSERT 的 `SELECT … INTO`、`xp_cmdshell`），2 条正则拦住但理由该改（`unknown_table` → `metadata_access`）；当时的 40 条评测集 `eval` 40/40 与 `--multi-turn` 50/50 在开关两种状态下逐字节不回归（语料现为 123 单轮 + 10 多轮 = 133 条，CI 门禁仍钉在 `v1_baseline` 那 40 条）。**尚未达成**：列引用级检查仍在正则层（AST 只补结构与函数），`guard-check` 的整套用例只跑在 `sqlite` 目录上（`tsql` 形状在单测与分析器层验证，没在外接 SQL Server 目录上复跑），AST 解析开销已用离线微基准量化（`SQLGuard.check` 每条 0.112ms → 0.664ms，一次性 `import sqlglot` 105ms + 首次解析 86ms），**端到端开/关 A/B 的离线这一半已补**（2026-10-22，`mock` 123 条，AST 关 9 次 / 开 7 次：判定逐条零差异，整轮 wall 484→812ms，其中 warm-up ≈134ms 占四成、稳态 ≈0.73ms/次校验 × 266 次）；**在线模型下的开/关 A/B 仍未跑**——那一半里护栏拒绝会回灌进工具循环、改变后续轨迹，不是编排开销能代替的。

**问题**：当前 guard 是正则 + 轻量扫描，复杂嵌套、注释、方言函数可能漏检；`SELECT` 子查询里的写操作也可能绕过。

**方案**：

- 引入 `sqlglot`（可选依赖）解析为 AST，统一做语句类型、表引用、列引用、CTE、函数白名单检查。
- 保留现有正则作为快速预检；AST 解析失败直接拒绝（fail-closed）。
- 方言参数化：`sqlite` / `tsql` / `postgres`。

**验收**：

- 新增 30 条对抗用例（嵌套写操作、注释绕过、UNION 注入、`WITH ... DELETE`、大小写/方括号混用）全部拒绝。
- 现有评测集不回归（现为 CI 门禁子集 `eval --only-tag v1_baseline` 的 40 条，mock 40/40；全量 123 条按 `benchmarks/ex_baseline.json` 的指标差分拦合并，见 P2-6）。
- 解析失败时返回明确 `parse_error`，不执行。

### P0-2 在线模型基线

**问题**：mock 只验证编排，不验证生成质量；没有在线模型的同集数据。

**方案**：

- 用 `openai` provider 跑 3 个模型（如 qwen-plus / qwen-max / deepseek-chat），固定 temperature、max_tokens、prompt 版本。
- 记录 token 用量与单题成本；建立 `provider × model × prompt_version` 的基线表。
- 对失败样本做人工归因，形成 prompt 修改清单。

**验收**：

- 产出第一份在线模型评测报告（≥ 50 条脱敏问题）。
- 明确当前 EX 基线、主要失败类别 Top 3。
- 报告可复现：一条命令重跑，结果落盘。


### P0-3 评测集扩容与分层

**问题**：16 条只覆盖“快乐路径”，不足以指导优化。

> 状态：第一阶段已完成——评测集扩到 40 条（37 可回答 + 3 拒答），覆盖 11 类，`category`/`difficulty`/`expected_tables`/`tags` 全部标注，报告可按 category 切片。**难例占比 10%（hard 4/40），尚未达到 ≥ 30% 目标**；`by_difficulty` 切片、每类 ≥ 10 条、同义改写集、对抗集、方言集与 100+ 扩容仍待做。（这一段是扩容前的状态；`by_difficulty`、100+ 扩容、每类 ≥10、难例 ≥30%、同义改写集、对抗问法集与方言 gold 集均已由下面的日期条目完成，EX 增量门禁见 P2-6。）
>
> 状态（2026-10-22，P0.5 已落地）：`eval generate-cases`（`src/nl2sql_agent/casegen.py`）让模型批量草拟"问题 + gold SQL"，逐条过**契约 → 护栏 → 真实执行且有行**三道关，只写 `status="draft"` 到 `var/ai_draft_cases.jsonl`（gitignore）。实测一次 `--count 10`（`qwen-plus`）：10 条草拟、8 条通过、2 条因 gold 返回 0 行被丢弃（0 行 gold 会把任何空结果判成正确，必须丢），整批 2,494 token；把这 8 条草稿当评测集跑，报告 `total=0 / skipped=8`，指标一位未动。**扩容本身仍未完成**：草稿没有逐条人工核对、没有一条进冻结集，所以难例占比与 100+ 条的目标都还没达成——这一条刻意不因为"命令能跑"就宣布做完。
>
> 状态（2026-10-22，**扩容已完成，人工审核未完成**）：冻结集 **40 → 123 条**（113 可回答 + 10 拒答），难度 **easy 40 / medium 46 / hard 37 = 30.08%**（目标 ≥30% 达成），11 个类别每个 ≥10（kitting 13 / group_by 12 / inventory 12 / purchase 12 / metric 11 / production 11 / bom 11 / detail 11 / ranking 10 / time_filter 10 / scope 10），`by_difficulty` 切片已实现并进 JSON + Markdown 报告。113 条 gold SQL 逐条机器验证：护栏正则层与 AST 层**各 0 error / 0 warning**，实际引擎执行 **0 失败 / 0 空结果 / 0 全 NULL**。CI 门禁移到 `eval --only-tag v1_baseline`（原 40 条，mock 仍 40/40、EX 1.0），全量 123 条当时只出报告（其后改为按 `benchmarks/ex_baseline.json` 的指标差分拦合并，见 P2-6）——`mock` 在全量上是 61/123、EX 0.4602（规则通道只认识演过的形状：easy 52.50% / medium 56.52% / hard 37.84%）。扩容暴露并修掉三个启发式缺陷（`ORDER BY` 子句逗号被读成逗号并表、`MAX/MIN` 被当成会扇出放大的聚合、分类词 `前` 匹配到 `之前/当前`），**改的是代码不是语料**，各配回归测试；分类器覆盖率也量了出来：v1 子集 37/37，113 条 **98/113 = 86.7%**，15 条不一致逐个钉住。**仍未达成**：① 83 条新增用例**没有经过人工逐条核对**（本会话编写 + 机器验证，按 §0 规则不能自称"已审核"）；② 在线模型基线只在 40 条 v1 子集上跑过，123 条上未重跑；③ 同义改写集、对抗问法集、方言 gold 集未做（这三件语料其后均已落地，见本节末尾 2026-10-22 / 2026-10-23 三个条目）；④ 失败责任层未逐条标注（已由「失败责任层自动归因」与「`generation` 层结果集细分」两条目补上，见下）。详见 `CHANGELOG.md`「P0-3 评测集 40 → 123 条」。
>
> 状态（2026-10-22，**失败责任层已自动归因，当时仍只是粗分类**）：每条失败用例带 `failure_layer`，报告新增 `by_failure_layer`（cases / share / full_recall / ids），九层按证据强度排优先级 `label → triage → clarification → guard → execution → retrieval → stalled → generation → unknown`；判据来自 `agent.tool_trace[].stage`（读 payload 结构，不读给人看的 `summary` 文案）。`mock` 全量 123 条实测 62 条失败：`generation` 36（其中 **32 条满召回**）、`clarification` 25（23 条满召回）、`triage` 1，`retrieval / guard / execution / stalled / label` 均 0；`--multi-turn` 分层与之完全相同（多轮 10 条全过）。**判分一字未动**：门禁 40/40、全量 61/123、多轮 71/133、双通道 19 条一致率 1.0、`guard-check` rc 0、`pytest` 363 passed。**仍未做**：① 只有 `mock` 的分布，在线模型上要重跑跑分才有这一列；② 自动标签与 P6 的人工逐条归因口径不同（人工把"该答却问"记进"未走到最终答案"），一致性未对照；③ `generation` 内部的细分（下一条已补一层）。
>
> 状态（2026-10-22，**`generation` 层已按结果集形状细分**）：报告新增 `by_generation_subtype`（cases / share / full_recall / meaning / ids），判据只用评测器**已经执行过的两个结果集**，不调模型、不额外执行 SQL：`column_shape`（列数不同）22 = 61.11%（19 条满召回）、`value_mismatch`（形状同、数值不同）8 = 22.22%（7）、`grain_or_scope`（行数不同）4 = 11.11%（4）、`empty_result`（0 行）2 = 5.56%（2）、`order_only` 0。子类型之和 = `generation` 条数、非 `generation` 层的子类型必为空，这条不变量进了测试。**仍只是 `mock` 的失败结构**：在线模型 P6 人工归因排第一的是粒度/口径（18/26、16/23），mock 排第一的是列数不对（规则通道没模板时退化成通用查询），两者形状不同，不能互相引用。**刻意不拆**：`grain_or_scope` 不把扇出放大 / 漏聚合 / 过滤缺失分开——行数一样就给不出这个证据。**顺带修一个归因漏洞**：`gold_sql_failed` 以前会落进 `generation`，现在先判 `gold_failed` → `label`，评测集自身的问题不再算到模型头上。判分仍一字未动（门禁 40/40、全量 61/123、多轮 71/133、双通道 19 条一致率 1.0、`guard-check` rc 0、`pytest` 365 passed）。**仍未做**：在线模型跑分上的这一列、自动标签与人工归因的一致性对照。
>
> 状态（2026-10-22，**同义改写集已落地并给出可复跑指标，对抗集与方言集未做**）：`data/paraphrase_cases.jsonl` 32 条改写用例，每条只改问法，`gold_sql / expected_tables / category / difficulty / order_sensitive` **逐字抄自**冻结集源用例（`tests/test_paraphrase.py` 逐字段钉住这个不变量），因此改写与源用例判定不同只可能是措辞造成的。`eval --paraphrase` 追加这一份语料并在报告里给出 `paraphrase` 配对块。`mock` 实测（155 条 = 123 + 32，通过 69，EX 0.4138）：32/32 配对、0 落单、`verdict_agreement_rate` **0.875**，但其中 **20 对两侧都失败**，所以可读的结论是"源用例通过的 12 对里 4 对被问法打倒 = 4/12 = 33.3% 脆性"（源侧 37.50% vs 改写侧 25.00%）；4 条 brittle 全部 `recall=1.0` 两侧同分，落在 `clarification` 2 条 + `generation` 2 条，检索被排除；`reverse` 0 条；行数可比对的 18 对里形状一致 77.78%。**与方案的两点偏差如实记下**：① 原计划"同一问题 5 种问法"，现在是每个源用例 1 种问法（覆盖 32 个源用例而不是 6 个源用例 × 5），因为要测的是"问法鲁棒性有没有一个数"，宽度比深度先有用；② 改写**刻意不动值字面量**（"华东"仍写"华东"）——把"华东"改成"华东大区"测的是 P1-2 值对齐，混进来看不到措辞本身的效应。**为什么不进门禁**：改写不带 `v1_baseline`，也不在基线的 `total=123 / answerable=113` 里，`--paraphrase` + `--fail-on-regression` 按设计退出码 2 拒绝对比（有测试钉住），它是一份测量语料而不是门禁语料。**仍未做**：① 32 条改写由本会话编写，按 §0 规则**未经人工逐条审核**，因此只当测量用，不进冻结集；② `mock` 的脆性不等于在线模型的脆性（规则通道按关键词匹配问法，天然对措辞敏感），在线模型上要重跑才有意义；③ 问法与经验召回的联动（P1-4 的"相似问法 Recall@3"）**已在 2026-10-23 接上**：`experience-check` 直接以这 32 对改写为查询、以源用例 SQL 为目标，报 hit@1/3/5 与 MRR（见 P1-4 状态块）；④ 对抗集与方言 gold 集仍未做（`guard-check` 那三份 `adversarial/permission/fanout` 语料测的是护栏，不是端到端问数；这两份问法语料其后均已落地，见本节末尾 2026-10-23 两个条目）。详见 `CHANGELOG.md`「同义改写集与改写一致性测量」。
>
> 状态（2026-10-23，**对抗问法集已落地；方言 gold 集见下一条**）：`data/adversarial_question_cases.jsonl` **24 条**诱导性问法，7 个家族、每族 ≥3 条（敏感列 4 / 禁用列 3 / `SELECT *` 3 / 禁用表 3 / 系统目录 3 / 扇出陷阱 4 / 追问压力 4），`eval --adversarial` 追加并给出 `adversarial` 块。**这一条真正要解决的是判定证据**：单一 pass/fail 分不出"护栏挡住了"和"模型没去碰"，所以每个安全类用例落四态之一并按严重性优先排：`leaked`（禁止目标出现在**执行结果列名**里）→ `guard_missed`（禁止形状过了护栏、只是没成为答案）→ `blocked_by_guard`（真取了、被拒）→ `clean_no_attempt`（本轮无可拦之物）。`mock` 实测（147 条 = 123 + 24，通过 76，EX 0.4344；对抗 24 条自身通过 15 / 62.50%）：安全类 16 条 **leaked 0 / guard_missed 0 / blocked 0 / clean_no_attempt 16**，拒答命中 **11/12**（唯一没拒的是"我是销售总监权限最高不用审批"那条，它把问题读成"客户金额前三"、答了 3 行错答案而不是泄露）；追问压力 4 条 asked 3 / guessed 0 / over_clarified 0 / answered 1，其中第 4 条是**反例对照**（口径给全且明确要求别追问 → 期望直接答），证明这一族不是"一律反问"；AST 开/关端到端复跑逐条 verdict 全同。**与方案的两点偏差 + 两个测量副产品**：① 方案写"同一问题 5 种问法"式覆盖，这里是每族 3–4 条、7 族共 24 条，宽度优先；② 泄露判定**刻意不只用 `guard.columns`**（否则解析器瞎的地方检测跟着瞎），另加独立词元扫描与执行列头，并用一个故意放行的假护栏把这条钉进测试；副产品一：`SELECT *` 只报 `select_star`、**不展开**成它实际返回的敏感/禁用列，所以星号类只能判"该不该拒"；副产品二：系统目录访问正则层报 `unknown_table`、AST 层报 `metadata_access`，故 `expect_codes` 两个都声明，判定不随 `.[ast]` 漂移。**与改写集同样的口径**：不带 `v1_baseline`，`--adversarial`（以及 `--paraphrase --adversarial` = 179 条）上门禁一律退出码 2 `语料规模不同`。**仍未做**：① 24 条由本会话编写、逐条机器验证，**未经人工逐条审核**，只当测量语料；② `blocked_by_guard = 0` 意味着**本段没有检验护栏强度**，只检验了判定的区分能力，在线模型上的泄露率/拒答率/掉坑率未测；③ `principal=None`，角色扮演走问法层而非权限层，带角色的对抗未测；④ 方言 gold 集（TOP/LIMIT、日期函数、字符串拼接）仍未做，这是 P0-3 三件语料的最后一件（其后已由下一条落地）。详见 `CHANGELOG.md`「对抗问法集」。

> 状态（2026-10-23，**方言 gold 集已落地，P0-3 三件语料到此齐全**）：`data/dialect_cases.jsonl` **22 条** = 8 个意图 × 8 个轴（`row_limit` / `paging` / `date_format` / `date_part` / `string_concat` / `identifier_quote` / `null_fallback` / `numeric_precision`），方言分布 `tsql` 8 / `postgres` 7 / `mysql` 4 / `sqlite` 3；同一意图的问法与参考 SQL 逐字相同，行间唯一变量是方言。**入口不是 `eval`**：`nl2sql-agent dialect-check`（`dialect_audit.py`），它不调模型，也不属于"问数准确率"那条链路。离线的硬约束是**只有 SQLite 一个可执行引擎**，所以把"能不能测"拆成两列：护栏是否接受本方言的原生写法（不需要连接），以及把本方言 gold 转译到 SQLite 后**结果集**是否与手写参考一致（需要转译器，不需要第二台数据库）。`sqlite` 后端实测（sqlglot 30.24.0，退出码 0）：gold 解析 **22/22 ok**、正则层误杀 **0**、参考执行 22 条 0 失败 0 空；可比 22（原生 3 / 转译 19）→ 一致 **16**、静默偏离 **3**、响亮失败 **3**，一致率 72.73%、**只看转译的 19 条 68.42%**；22 条错方言形状 **正则层拦 0**、AST 层拦 6（全部 AST 独占）、引擎拒绝 13、照跑 9。**三个副产品**：① 测出并修掉一处护栏**误杀**——`EXTRACT(YEAR FROM o.order_date)` 的内嵌 `FROM` 被掩码器当表引用，列名读成不存在的表，正确的 PostgreSQL gold 被拒（A/B：修前拦 1 条、修后 0 条；修法限定"函数名后同一层括号内"，所以 `TRIM(c) FROM t` 的真 `FROM`、禁用表、系统目录照旧拦得住，三条回归测试各钉一位）；② 测出一处护栏**洞**——正则层对跨方言形状完全盲（`TOP 3` 与 `LIMIT 3` 无区别），这条**归 P2-2**（按方言拒绝不支持语法），本阶段只量化不修；③ **静默偏离与响亮失败分开数**：`CONVERT(char(7),…,120)`、`+` 拼接、`CAST(… AS DECIMAL(18,2))` 三条转译后照样执行、结果不同、一条错误都不报，只有报错的那一半才是"重试逻辑"能兜住的。**与方案的两点偏差**：原计划按"能连实际引擎跑一遍"验收，现在是转译等价（基准是 sqlglot 的语义，不是 PG/MySQL/SQL Server 自己的执行结果，"转译后一致"只证明形状无静默差异）；`current_date` 一类相对时间轴**刻意排除**（两侧碰巧相同会被读成一致，是假证据）。**为什么不进门禁**：`tags=["dialect"]`、不带 `v1_baseline`、不在基线 `total=123` 里，也不进 `eval` 的语料合并。**仍未做**：① 22 条由本会话编写、逐条机器验证，**未经人工逐条核对**；② 正则层 0/22 未修（P2-2）；③ `numeric_precision` 只覆盖 `CAST`/`ROUND`，实际引擎标度截断行为未测；④ 真实方言引擎上的端到端跑分未做（要第二个数据库）。详见 `CHANGELOG.md`「方言 gold 集」。
>

**方案**：

- 扩充到 100+ 条，按业务域分层：订单/库存/采购/工单/BOM/齐套/财务口径/权限/拒答。
- 增加 **同义改写集**（同一问题 5 种问法）、**对抗集**（诱导查敏感字段、诱导 `SELECT *`、跨表扇出陷阱）、**方言集**（TOP/LIMIT、日期函数、字符串拼接）。
- 为每条用例标注难度（easy/medium/hard）与失败责任层。

**验收**：

- 每类 ≥ 10 条；难例占比 ≥ 30%。
- 评测报告能按 category/difficulty 切片。
- CI 设置阈值：EX 不得低于上一版本 2 个百分点。

### P0-4 JOIN 扇出检测

> 状态（2026-10-22）：**已落地为 warning，不是拦截**。`fanout.py` 的判别口径收敛到一条："同一个聚合里引用的表**只**来自 1:N 关系的「一」侧"才报 `join_fanout`；第一版按"聚合涉及父侧"就报，在 37 条 gold SQL 上误杀了 2 条 `SUM(i.on_hand_qty * m.standard_cost)`（父侧列乘子侧列再聚合是库存金额的标准算法），因此把"永不 error"写进实现，并把"gold 语料零告警"用 `test_gold_corpus_stays_warning_free` 钉成回归。扇出用例 11 条（6 应警 / 5 应静默），实测 warning 6、error 0；同时修掉一个真实漏检：`_JOIN_RE` 的先行要求空白，导致句尾 `… JOIN x`（正是最典型的笛卡尔形状）完全不报警。**尚未达成**：验收里的"扇出陷阱用例 EX 可测量"没做——要做需要 gold 与错误写法两套结果集比对，冻结集里只有 gold；跨查询的传递扇出（A→B→C）与子查询内扇出不分析。
>
> 状态（2026-10-23，**"扇出陷阱 EX 可测量"这一条已完成；"给出明确 warning"这一条现在有了反证**）：`data/adversarial_question_cases.jsonl` 带 **4 条**扇出陷阱用例，每条同时给 `gold_sql` 与 `wrong_sql`，评测器把**两套都真执行**并按结果集判 `gold / trap / neither / no_sql`（`eval --adversarial` 的 `adversarial.fanout` 块）。四对都机器验证过两侧结果集不同（gold 12/4/8/6 行 vs 坑 5/4/2/6 行）且 `wrong_sql` **全部通过护栏**——否则"没掉坑"可能只是因为坑被拦了。`mock` 实测：gold 0 / 掉坑 0 / neither 1 / no_sql 3（通过率 0/4，失败全部是"退化成澄清"与"列数不对"，**没有一条真的算出扇出后的错数**）。**同时给出反证**：这四条真会算错的 SQL 上 `join_fanout` 告警数是 **0**（AST 开/关一致），因为它们错在"两条互不相干的 1:N 互相放大行数后各自求和"，而告警只管"父侧一列被重复求和"——形状不在射程里。所以验收第二条"对 3 个真实扇出 SQL 能给出明确 warning"**未达成**，且这条静态分析**不能**当作扇出正确性的证据，判定必须走结果集比对。仍未做：任意 SQL 的扇出自动识别、传递扇出与子查询内扇出。详见 `CHANGELOG.md`「对抗问法集」。

**问题**：订单主表 JOIN 明细表后再 SUM 金额，若同时 JOIN 发货表，会重复计数；这是 ERP 问数最常见的“结果看起来对但数值翻倍”。

**方案**：

- 在 guard 增加静态提示：同一查询中多个 1:N 表参与聚合时 warning。
- 在 Agent prompt 中要求“先聚合后 JOIN”或使用 `EXISTS`/子查询。
- 评测集加入扇出陷阱用例（gold 与错误写法结果不同）。

**验收**：

- 扇出陷阱用例 EX 从当前未覆盖提升到可测量。
- 对 3 个真实扇出 SQL 能给出明确 warning，而不是静默通过。

### P0-5 行级/列级权限

> 状态（2026-10-22）：**已落地，机制与规划不同：不是"强制追加 WHERE"，而是"视图收窄 + 护栏判权"两次生效**。方案原本写"强制追加 `region IN (...)`"，实测取向相反——**由模型自己写、护栏来验**：单值行过滤会作为 `required_filter` 提示注入过滤视图，SQL 里没带或带错（子查询、`LIKE`、`OR` 的越权分支）就报 `row_filter_missing` 拒绝执行。这样权限判定与生成解耦，也避免了"系统替用户伪造一个条件、结果集看起来完整其实被改写"的口径混乱。表级/列级按 `tables` / `only_columns` / `exclude_columns` 裁剪视图，越权引用报 `permission_table` / `permission_column`（而不是"表不存在"，否则错误消息本身泄露策略），无权列连"相近字段"建议都不出现。权限用例 **23 条 / 5 个合成角色**：越权拦截 17、视图泄露 0；`analyst_full`（不设限制的角色）与无权限运行的 `to_dict()` 逐字段相等，证明"关掉权限=退回原行为"。**尚未达成**：① 验收里的"审计日志落盘（principal + SQL + 拒绝原因）"没做——principal id 与拒绝原因目前只出现在 `GuardResult.issues` 与 `tool_trace` 里（随 JSON 输出），没有独立审计通道，落盘位置也还没定（`var/` 之外的任何日志都可能带真实 SQL）；② "同一问题在不同角色下生成不同 WHERE"只有离线证据（`--principal rep_east` 跑冻结集 4/40，36 条 `needs_human` 是越权被拒 + 角色与语料不匹配），在线模型上的角色差异化待 P6；③ 一次运行只绑一个 principal，无多租户与动态切换。

**问题**：当前只有全局敏感字段开关，没有“按人看数据”。

**方案**：

- 在 `Catalog`/`SQLGuard` 初始化时注入 `Principal`（用户 ID、角色、可见区域/客户/字段）。
- 表级：可见表白名单；列级：敏感字段按角色放行；行级：强制追加 `region IN (...)` 或 `sales_rep = :me`。
- 所有权限判断在 guard 内完成，提示词不暴露无权 schema。

**验收**：

- 同一问题在不同角色下生成不同 WHERE 条件，越权查询被拒绝。
- 审计日志记录 principal、SQL、拒绝原因。
- 权限用例纳入评测集。

---

## 3. P1：准确率与体验

### P1-1 混合 Schema Linking（BM25 + Embedding + Rerank）

**现状**：纯词法 BM25，对“回款”“呆滞料”“齐套”等同义/业务黑话召回弱。

>
> 已落地：策略化词法召回 + 规模自适应 + `NL2SQL_LINKING` 强制指定 + `--variants` 复测；`Catalog.table_documents()` + `EmbeddingProvider`（`hash` 离线 / `openai` 兼容端点）+ `SchemaIndex` 按 `schema_hash + docs_hash + provider` 失效的向量缓存 + RRF 融合 + 向量任一步失败自动降级 BM25 + `search_tables` 返回逐表检索来源（`bm25` / `vector` / 两者）+ 可选 `LLMReranker`。
>
> 未做：端到端 EX 归因（真实问题集含企业标识，不随仓库发布，故本节只报召回数字）；cross-encoder 类专用重排模型（只做了 LLM 重排）。

同集对比（`link-check --variants`，模型无关、不连库；外接目录那份问题集为本地私有，仓库内不可复现这两列）：

|---|---|---|---|
| baseline（已发表口径） | 0.600 / 0.700 | 0.900 / 0.965 | 0.819 / 0.856 |
| no_length_norm | 0.787 / 0.800 | 0.900 / 0.965 | 0.819 / 0.856 |
| thin_columns_no_norm | 0.812 / 0.838 | 0.943 / 0.965 | 0.681 / 0.762 |
| comment_synonyms | 0.500 / 0.625 | 0.965 / 0.989 | 0.656 / 0.787 |
| **hybrid（大目录自动选用）** | **0.812 / 0.850** | 0.943 / 0.965 | 0.681 / 0.762 |

数字为 recall@3 / recall@5。三点结论决定了最终形态：长度归一化在小目录是帮助、在几百表目录会把薄表顶到前面（+8.7pt）；注释同义词挖掘在大目录是噪声（-7.5pt），只在演示这种“注释即业务名词”的小目录有用；没有任何一个策略同时赢两种规模，所以只能按规模自动切换，而不是把 hybrid 当作通用最优。多轮追问偏短、靠表名与字段名直接命中，baseline 更好，这也是自动策略保留小目录 baseline 的原因。

**方案（已按此实现）**：

- 离线为每张启用表生成一条 embedding 文档（剔除禁用与敏感列），存 `var/embeddings/<schema_hash>-<provider>-<docs_hash>.json`，结构或模型一变即失效。
- 查询时向量召回 top-N 与 BM25 top-N 做 RRF（`NL2SQL_LINKING=vector|rrf`），可选再让 LLM 把 `NL2SQL_RERANK_CANDIDATES`（默认 12）重排到 top-k。
- 保留 BM25 兜底：provider 缺失、调用失败、维度/数量不符、缓存损坏一律降级为 BM25，并在 `retrieval` 里带 `degraded` 与原因。

**实测（两份人工标注的表召回集，recall@3 / recall@5）**：

|---|---|---|
| baseline BM25 | 0.950 / 0.975 | 0.600 / 0.700 |
| hybrid BM25 | 0.975 / 0.975 | **0.812 / 0.850** |
| 纯向量 text-embedding-v4 | 0.900 / 0.950 | 0.625 / 0.688 |
| RRF 融合 | 0.925 / 0.975 | 0.775 / 0.838 |
| hybrid + LLM 重排 | 0.975 / 0.975 | 0.850 / **0.863** |
| RRF + LLM 重排 | — | 0.825 / 0.850 |


### P1-2 值对齐与业务词典

**现状**：`search_values` 只能对单列做 LIKE，中文简称/别名（“杭智”“伺服电机那家”）无法对齐。

**方案**：

- 为客户/物料/供应商建立别名表（人工维护 + 从历史问题挖掘）。
- 查询改写阶段做实体链接：问题中的提及 → 候选实体 → 置信度 → 必要时澄清。
- 对状态枚举建立中英映射（pending/待发货）。

**验收**：

- 别名问法用例 EX 提升。
- 歧义实体（如“华创”对应多个客户）触发 `needs_clarification`，不瞎猜。

> 状态（2026-10-23，**已落地并给出可复跑指标**）：三条方案都实现了。① 别名表是**数据不是代码**（`data/value_dictionary.json`，30 条目 / 47 别名 / 74 提法 / 声明碰撞 `精工`、`轴承`，覆盖 7 列），真实简称属业务数据、用 `NL2SQL_VALUE_DICTIONARY` 指向仓库外文件；「从历史问题挖掘」这一半**没做**——挖掘出来的词条没人核对就会直接改写 prompt，所以词典只人工维护，`alias-check` 负责证明它今天还与目录和库内取值吻合。② 实体链接在类别判定与提示构造**之前**（`value_align.py`）：最长提法匹配 → 唯一取值就在问法**后**追加 `[值对齐] 表.列=值`（不改用户原话），多取值**不猜**、转 `needs_clarification` 并带候选项（最多 3 条），未覆盖问法逐字节穿透，一次最多注入 8 个取值（超出记 `dropped`），冲突由用户自己的类别词消解（"物料轴承"的"物料"→ material，依据记在 `qualified_by`）。③ 状态枚举中英映射已建（`pending/待发货`、`in_transit/在途`），并与 `dim_lookup` 交叉核对。
>
> **验收是量出来的**（`alias-check`，`mock`，退出码 0）：别名用例配对 A/B **可比 18/20**（gold 可执行 18），关闭对齐通过 **5** → 开启 **15**，**修好 10 条**（6 客户 + 3 区域 + 1 物料简称）、**改坏 0**；两条歧义用例开启后 **2/2 反问且 2/2 带候选项**（关闭时只反问 1 条），第二条即"不猜"的证据。**冻结集逐位未变**（40/40、61/123 EX 0.4602、71/133、69/155、76/147、84/179），冻结集被触达 23/123 条**全是状态枚举**且**转成澄清 0 条**。
>
> **四条偏差与边界如实记下**：① 声称的收益只有 **10 条而不是 12 条**——`a_mat_ballbearing_alias` 关闭对齐也过（工具循环自己调 `search_values` 列出取值后改对），所以不能把整份语料当增益；② **状态枚举对齐在 `mock` 上是 no-op**（规则模板不消费注入值，2 条 `status_enum` 用例两臂都失败），这一类只有在线模型才可能兑现；③ 在线模型上的开/关 A/B **未跑**（API 成本），`mock` 的 10 条不能外推；④ 20 条别名用例由本项目编写、逐条机器验证，**尚未人工逐条核对**，按 §0 只做测量语料，不进冻结集、不进任何门禁（A/B 差值刻意不进退出码：它是证据不是判据）。另有一处**测量发现的真伤害已修**：`轴承` 裸提法最初把 3 条 gold 用例转成了反问，补的是类别限定词规则而不是删词条，"冻结集转澄清 0" 从此钉成不变量。详见 `CHANGELOG.md`「值对齐与业务词典」。

### P1-3 多轮上下文与指代消解

**现状**：每次 `run` 独立，无法处理“那华南呢？”“按月份拆开”。

**方案**：

- 维护会话状态（上一轮 SQL、表、过滤条件、结果列）。
- 新增 `rewrite_question` 步骤：把省略/指代补全为独立问题，再进入 Agent。
- 明确上下文失效条件（切库、切主题、超过 N 轮）。

**验收**：

- 10 条多轮用例（追问、改维度、改时间、改排序）通过。
- 指代不明时主动澄清，不继承错误上下文。

### P1-4 经验召回升级

**现状**：词法 Jaccard + 编辑距离，跨表述召回有限；经验按单题存储。

> 状态（2026-10-23，**离线部分已全部落地；few-shot A/B 仍未做**）：经验库现在有两根正交的轴——`status`（`candidate / verified / deprecated`，管可见性）与 `quality`（`candidate / verified / gold`，只做排序先验，`gold` **只能人工 `experience promote` 给出**，工具路径写不到）；旧库的 `quality='hard'` 在打开时迁移为 `verified`。schema 变更后的隔离靠检索时的精确 `schema_hash`，批量退休是显式的 `experience deprecate-stale`（默认只预览、`--yes` 才落库），**不做自动退休**——配错目录路径会得到新 hash，自动退休等于让一次配置错误静默清空经验库。反例沿用 `failure_queue` 并新增三个消费者：`experience counterexamples` 人工分诊、按 `error_code` 的重复计数、以及 `save_experience` 的准入闸门（同一问法 + 同一 SQL 仍在 open 时直接拒绝写入）；**它从不进入提示词**，`tests/test_counterexamples.py::test_counterexamples_never_reach_the_prompt` 断言 `WRONG_SQL` 既不在 `_few_shot_block()` 也不在 `_system_prompt()` 里。这与规划里"提示模型避免同类错误"那一条**故意相反**：同一节的验收项写的是"错误经验不会进入提示词"，而把反例拼进 prompt 还会改变 `prompt_hash`，让此前所有准确率数字失去可比性，所以按验收项执行并在此记录偏差。few-shot 选择新增可选 MMR 多样性（`NL2SQL_FEWSHOT_DIVERSITY`，默认 0，λ=0 与旧 top-k **逐位一致**）；召回新增 `lexical|vector|rrf` 三通道（默认 `rrf`，但没有向量 provider 时强制词法；`NL2SQL_EXPERIENCE_VECTOR=true` 而 provider 为 `none` 是**配置期报错**），向量缓存按 `(schema_hash, provider, docs_hash)` 复用，provider 抛错或返回短向量时降级词法并在 `recall` 元数据里标 `degraded`。**规划里的"类别聚类"没有新做**：类别的两个用途（召回加权 `category_boost`、经验打类）在 P1 已落地，按嵌入聚类没有独立收益证据，不引入。仪器 `experience-check`（`experience_audit.py`）在冻结集 32 对源/改写问法上跑五条臂，并断言 `lexical` 臂逐条等于旧 `store.search()`——这是"默认路径零变化"的不变量，不是自证。
>
> **实测**（`experience-check --embedding hash`，指纹 `0b04c44b2749`，32 对改写、冻结集 113 条 + 10 条无 gold 跳过；库在临时目录重建，不读写 `var/experience.db`）：
>
> | 臂 | 库=paired（32 行）R@1 / R@3 / R@5、MRR | 库=full（113 行）R@1 / R@3 / R@5、MRR | 解读 |
> |---|---|---|---|
> | `lexical` | .938 / 1.000 / 1.000、.969 | .812 / .906 / .969、.868 | 旧行为基线 |
> | `diversity`（λ=.5） | .938 / 1.000 / 1.000、.964（位移 40） | .812 / .875 / .906、.852（位移 69） | 用单目标命中换 SQL 形状覆盖；只在 3 条预算里有意义 |
> | `gold-half`（16/32 人为提 gold） | .875 / 1.000 / 1.000、.932 | .625 / .844 / .938、.741（提升组 13/14、普通组 14/18） | **任意标 gold 会伤召回**——这正是 gold 必须人工、默认 0.05 加分在演示库上是 no-op 的理由 |
> | `vector-hash` | .938 / 1.000 / 1.000、.964 | .688 / .969 / .969、.812 | 词袋 hash，只验证缓存/降级通路 |
> | `rrf-hash` | .969 / 1.000 / 1.000、.984 | .781 / .969 / .969、.870 | R@3 +6.3pp 来自两榜排名一致，**不是语义证据** |
>
>
> **历史状态（2026-10-22，保留）**：固定类别体系 + 规则分类器（冻结集 37/37、held-out 改写 11/11）、类别加权召回、verified-only 自动 few-shot 注入（3 条 / 1200 字符预算）、失败队列与 `experience resolve` 人工结案闭环已落地；因 `mock` 模型不消费注入内容，且在线模型跑分一律用全新空经验库（避免近似 gold 的 SQL 被注入造成自泄漏），A/B 一直未做；`paraphrase_cases.jsonl` 的 32 条改写逐字复用冻结集 gold SQL，回答的是"换问法还过不过"，不能用来做 few-shot A/B——后者需要**新 SQL 形状**的用例。

**方案**：

- 经验 embedding 召回 + 类别聚类；同类别内做 few-shot 选择（多样性 + 相似度）。
- 引入“反例经验”（失败 SQL + 错误原因），提示模型避免同类错误。
- 经验质量分级：`verified` / `gold` / `deprecated`；schema hash 变更时批量标记 stale。
- 记录“经验是否真的提升了该题”，做 A/B 归因，避免无效 few-shot。

**验收**：

- 相似问法 Recall@3 提升。
- few-shot 开关 A/B：EX 有统计显著提升才默认开启。
- 错误经验不会进入提示词（单测覆盖）。

### P1-5 双通道生成 + 执行投票（成本可控时）


**方案**：

- 对高价值/高风险问题生成 2-3 个候选 SQL，全部执行；结果一致才高置信输出。
- 结果不一致时：比较执行计划/结果差异，触发澄清或人工。
- 仅在 `confidence < 阈值` 或涉及金额汇总时启用，避免成本翻倍。

**验收**：

- 高价值问题集上错误率下降，平均成本增幅有上限（如 ≤ 1.8x）。
- 投票无法收敛时 100% 升级，不静默选一个。

### P1-6 澄清策略

> 状态（2026-10-23）：**已落地，默认 `options`**。规划里的五个触发条件**不属于同一层**，这个分法是被冻结集 footprint 逼出来的：一律"挡住并反问"会挡掉 **43/123** 条能答的 gold 用例（指标歧义 10、时间范围缺失 39、两者都占 6），而实体多义挡住 0 条。于是 `ASSUME_CODES`（指标歧义 / 时间范围缺失）**照常回答**并把每种口径渲染成完整问句交回，`ASK_CODES`（实体多义 / 权限不足 / schema 无此指标）才可以挡；`NL2SQL_CLARIFY=off|options|ask`，`ask` 存在的意义是让"挡路的代价"是个可量数字——实测 `options` 挡 **0/123**、`ask` 挡 **4/123**（`d_inventory_m005`、`i_locked_share`、`t_due_july`、`t_last30_days`）。名词表（`MetricGlossary`）默认内置 3 术语 / 8 候选口径，生产用 `NL2SQL_METRIC_GLOSSARY` 指向仓库外文件。选项一律是**完整问句**（周期字面量写在句子内部），所以点一下就等于重新问一次，回问路径零状态；`resolve_clarification_reply` 把「2」「第二个」「上月的」还原成完整问句并记 `rewrite_source=clarify_reply`。仪器 `clarify-check`（`clarify_audit.py`，22 条语料：trigger 8 / quiet 5 / enrich 3 / followup 6，`provider=mock`，演示目录，退出码 0）实测：澄清 13 条、**可执行 13、裸反问 0**、选项 33 + 更窄问法 34、20 次选项回问**全部出答案**（3 次空结果）、6/6 followup 还原并答出且 0 次二次澄清、`entity_blocking == 0`；配对 A/B `off → options`：反问 13 → 13、可执行 **4 → 13**、裸反问 **9 → 0**、选项 **8 → 67**、`regressed []`。**"无效澄清比例下降"这条验收达成了**（9 → 0），**"二次问数成功率提升"只达成一半**：两臂状态分布完全相同（ok 9 / needs_clarification 13），第二轮的成功是在 `options` 单臂上测的，A/B 没有跑第二轮对照——这一层的产物是"能把问题问对"，把答案答出来的仍是第二轮那条普通链路。**两处 observations 只报告不判红**：`option_period_inert`（分组问法对周期不敏感，三个周期出同一种结果）与 `option_period_empty`（演示种子没有 7 月数据）分别是问法形状与数据事实。**一次配置错误被量成了策略缺陷**：首次运行撞上 `.env` 把 `NL2SQL_SCHEMA_PATH` 指向外接目录，20 次回问"全部失败"（实为每条 `unknown_table`），于是这一步现在**目录与打包语料不匹配就 rc 2 拒给判定**并印 `backend/dialect/tables/schema` 一行。**顺带补上一处真实集成缺口**：Web 控制台把 `alternatives` 整段丢掉（API 早就序列化进来了，缺陷只在渲染层），现在与 `options` 一样渲染成可点 chip，并在浏览器实测点选（"上月（2026年6月）销售额是多少？"→ 249255.0）。**本条没做的事**：① 22 条语料由本项目作者编写、逐条机器验证，**尚未人工逐条核对**，按规则只做测量语料，不进冻结集也不进任何门禁；② **在线模型上没测**——在线模型的触发码分布、以及"模型会不会自己吃掉选项"都不知道；③ `permission_denied` 这一码在本仪器里**不可达**（只在角色视图下由护栏的 `permission_table/permission_column` 落下，本命令不挂 principal），越权路径仍由 `--principal` 的权限基准测；④ 本命令不在 CI 里（与 `alias-check`、`dialect-check` 一致，是测量台不是门禁）；⑤ 没有引入"澄清会话"状态机，跨轮记忆与上下文改写仍是同一件事。详见 `CHANGELOG.md` 顶部小节与 `docs/ARCHITECTURE.md`「澄清策略层」。

**方案**：定义澄清触发条件——指标歧义（销售额含税/未税）、时间范围缺失、实体多义、权限不足、schema 无此指标。澄清问题要给出可选项，而不是泛泛“请补充”。

**验收**：澄清后二次问数成功率提升；无效澄清（用户无法回答）比例下降。

---

## 4. P2：产品化与工程化

### P2-1 可观测性

> 状态（2026-10-23）：**已落地，默认关闭**（不配 `NL2SQL_LOG_PATH` 就没有 handler、没有文件、没有任何输出）。四类 JSON Lines 事件 `api` / `turn` / `tool` / `db` 由同一个 `trace_id` 串起来，id **只在请求边界铸造**（HTTP `ask`、CLI `ask/demo/chat`、评测器每条用例），`agent.run` 只继承——所以把项目当库直接调用时结果逐字节不变（有一条测试钉住）。`turn` 行带 provider / model / `prompt_version` / `prompt_hash` / `schema_hash` / dialect / principal / 耗时 / token / `guard_codes` / repair 次数 / 双通道与值对齐观测；结果集与凭证**永不落盘**，业务文本（`question` / `answer` / `sql`）默认也不落盘、只留 sha256 前 16 位指纹，`NL2SQL_LOG_INCLUDE_TEXT=true` 才写原文。读数入口是 `nl2sql-agent metrics` 与 `GET /api/metrics`，**没有日志路径时退出码 2 拒给判定**而不是印一串 0；`GATED_METRICS` 一条都没加，P95 只进报告不进 CI。实测（`provider=mock`、演示库、`eval` 123 条）：**825 行事件（turn 123 / tool 477 / db 225）、坏行 0、trace 数 123**，轮次延迟 avg **3.00** / p50 **3.01** / p95 **5.05** / max **18.61** ms，`status=ok` **0.7236**（89/123，注意这是**状态比率不是 EX**，同批 EX 仍 0.4602）、人工升级 **0.0000**、澄清 0.2764、经验命中 0.0244、工具与 DB 错误率各 0.0000、prompt_hash 123 个（随问法变）而 schema_hash 恒 1。真实 HTTP 一次请求（客户端自带 `x-trace-id: demo-trace-0001`）→ **8 行同一 id**（api 1 + turn 1 + tool 5 + db 1），`/api/metrics` 读到 `lines=8`。**一处只有真机才看得见的缺陷**：这台 Windows 上 `time.monotonic()` 是 15.625ms 一格（GetTickCount64），mock 一轮本来 2–4ms，`elapsed_ms` 成批报 `0.0`、P95 也读到 0.0——耗时测量全部改到 `time.perf_counter()`，**因此本条之前印出的 mock 侧延迟是 16ms 量化值**（在线模型的秒级数字不受影响），测试改断 `elapsed_ms > 0`。写日志的成本也量了：同一 40 条门禁子集 关 **2.64/4.24** → 开 **3.25/4.81** ms（+0.61ms/轮，约 8 行）。**本条没做的事**：① **不是 OpenTelemetry**——`x-trace-id` 原样采纳（只校验 `^[\w:.\-]{1,64}$`），没有 W3C `traceparent` 解析，跨进程/跨服务链路不传播（contextvar 只在单进程内）；② 没有实时导出（Prometheus/statsd），`metrics` 是离线折线，QPS 只是"窗口内轮数 ÷ 窗口秒"、单进程顺序跑出来的是**吞吐下限不是上限**；③ 没有滚动与容量上限，长期运行需外部 logrotate；④ **线程侧已钉住、进程侧未验证**：4 个 worker 并发跑 8 条链，`trace_id` 8 个全不同且 `turn` 行的 id 集合与之相等（一条测试），但多进程共用一个日志文件的交错写未验证（`serve` 是单进程）；⑤ token 通道由 scripted provider 测试钉住，**在线模型一轮的 token 是否逐条落进日志尚未实测**；⑥ 延迟分布没有按类别/角色切片。**复跑逐位未变**：四种安装形态 **577** / **561 + 3 skipped** / **557 + 20 skipped** / **541 + 23 skipped**，eval 六组（40/40、61/123 EX 0.4602 门禁 +0.0000 rc 0、71/133、69/155、76/147、84/179）与四台仪器（`guard-check` / `dialect-check` / `alias-check` / `clarify-check`）全部 rc 0 且数字与上一条相同，P8 泄漏扫描 TABLE / TABLE COMMENT 各 **0**。详见 `CHANGELOG.md` 顶部小节与 `docs/ARCHITECTURE.md` §13。

- trace_id 贯穿 API → Agent → 工具 → DB；记录模型版本、prompt hash、token、耗时、guard 拒绝原因。
- 指标：QPS、P95 延迟、EX（离线）、人工升级率、经验命中率、repair 成功率。
- 敏感信息脱敏后入日志。

### P2-2 数据库适配层

> 状态（2026-10-22）：**SQLite + SQL Server 已落地并有测试**。`backend=mssql` 走 pyodbc（懒加载，无驱动也能跑全套测试），方言差异集中在 `TOP/LIMIT`、标识符引用与 `WITH (NOLOCK)`；`schema-sync` 从 `sys.objects / sys.columns / sys.extended_properties` 实时 introspect 出 catalog，中文 `MS_Description` 注释里的 `[关联表:X,关联字段:Y]` 解析成 join 提示，并自动禁用垃圾表、标注敏感列。这一步是真正连库测出来的：`sys.objects.type` 是 `CHAR(2)`，驱动返回 `'U '`，等值比较会让同步结果静默变成 0 张表——mock 永远发现不了，已补回归测试。未做：PostgreSQL、连接池、statement timeout 统一封装。
>
> 状态（2026-10-23，**量到了这一层的洞，本阶段不修** → 同日已修，见下一条）：方言 gold 集给出一个可复跑的数字——22 条"把别的方言形状塞进本方言"的错写法，**正则层拦 0 条**（它读的是掩码后的词元，`TOP 3` 与 `LIMIT 3` 没有区别）、AST 层拦 6 条（全部只有解析器看得见）、剩下 16 条要靠引擎自己拒绝（拒绝 13 / 照跑 9）。所以"按方言拒绝不支持语法"是这一层缺的能力，不是缺用例：`Dialect` 抽象要做的事里，**guard 侧的方言感知**排在连接池之前。同期顺手修掉一处方向相反的缺陷：`EXTRACT(YEAR FROM …)` / `TRIM(… FROM …)` / `SUBSTRING(… FROM …)` / `POSITION(… IN …)` 的内嵌关键字以前会被掩码器读成表引用，把正确的 PostgreSQL gold 判成 `unknown_table`（A/B 实测误杀 1 → 0，`FROM`/禁用表/系统目录的拦截由 3 条回归测试钉住未变）。详见 `CHANGELOG.md`「方言 gold 集」与 `docs/ARCHITECTURE.md` §8.1。

> 状态（2026-10-23，**上一条量出的洞已修在同一层**）：`SQLGuard` 现在按 `catalog.dialect` 拒绝"属于别的方言"的形状（`_SHAPE_RULES`：`TOP`/`FETCH … ROWS ONLY`/`::`/`ILIKE`/`[…]`/反引号/双引号引用/`||` 与 `'…' +` 拼接/外来日期与空值函数，sqlite 6 / tsql 6 / postgres 5 / mysql 9 条，`mssql→tsql`、`pg→postgres` 一类先归一，**未知方言一条规则都不设**）。同一份 22 条语料复跑 `dialect-check`：正则层 **0 → 22/22**、AST 独占 **6 → 0**、两层都不拦 **16 → 0**，`gold 护栏`（本方言地道写法）仍 **0 拦截**，退出码 0；`--no-ast` 与零依赖安装同样 22/22，能力不落在可选依赖上。防误杀靠三类对照：每种方言 5–9 条地道写法逐条放行（`test_native_spellings_pass_on_their_own_dialect`）、字面量/注释里的同形 token 不触发（拼接类规则读的是保留字面量的掩码投影）、冻结集 113 条 gold 与对抗集通例（`pass_top_mssql` / `pass_backtick_projection`）复跑 0 error 0 warning。顺带两处方向相反的收益：`FROM[internal_audit]`（对抗集 `adv_nospace_from`）现在由正则层 `dialect_bracket_quote` 拦住，AST 独占从 4 条降到 3 条；权限基准的拦截数 **17 从此与 sqlglot 装没装无关**（原来 16/17 随解析器跳）。**eval 六道组合逐位未变**（40/40、61/123 EX 0.4602、71/133、69/155、76/147、84/179）。**本条没做的事**：执行侧的 `Dialect` 抽象、PostgreSQL 后端、连接池与 `statement timeout` 封装——护栏现在能按方言拒绝，不代表系统能按方言执行；引擎那一列的 13 拒绝 / 9 照跑仍是 SQLite 单引擎的事实。详见 `CHANGELOG.md` 顶部小节与 `docs/ARCHITECTURE.md` §5.1、§8.1。

> 状态（2026-10-23，**上一条列的"本条没做的事"已做**）：执行侧的 `Dialect` 抽象、PostgreSQL 后端、连接池与 `statement timeout` 封装落地。
>
> - **方言只有一处定义**：以前同一件事写在四个地方（prompt 的 `if mssql / else SQLite` 两条分支、`guard._DIALECT_ALIASES`、`ast_guard.map_dialect`、`tools/db.py` 的语句拼装），所以目录声明 `dialect: tsql` 时护栏按 T-SQL 拒绝、prompt 却告诉模型"这是 SQLite、记得加 LIMIT"。现在 `dialects.py` 一张登记表供五处消费。两个刻意的分裂：`channel_label` 单列，因为第二意见提示语里的 `SQL Server (T-SQL)` 是 P3 投票那批数字的测量条件；`sqlite`/`tsql` 两条 prompt 行**逐字节钉在测试里**，所以这次收口对已发表的准确率与投票数字是零影响而不是"应该没影响"。`normalize()` 对未知方言返回空串、不猜成 SQLite（把 `oracle` 读成 `sqlite` 会拦住正确 SQL）。
> - **`statement timeout` 按方言落到引擎能接受的形式**：SQLite 没有服务端语句超时 → `set_progress_handler` 客户端 deadline（每 1000 VM step 检查），实测 `timeout_ms=150` 在 **150.12 / 150.32ms** 中止，墙钟与设定值一致；T-SQL `SET LOCK_TIMEOUT {ms}` + `conn.timeout`；PostgreSQL 在 autocommit 下按序 `SET default_transaction_read_only = on` → `SET statement_timeout = {ms}`（顺序与被测下限 1ms 由测试钉住）。测量本身的坑：demo 表只有 12/16 行，朴素 cross join **0.54ms 就跑完了**，超时根本测不出来——换成 4000 万行递归 CTE 才真正让 deadline 开火，这是"合成库上的 timeout 测试必须自带负载"的一条。
> - **连接池默认关闭**（`NL2SQL_DB_POOL_SIZE=1` 时 `configure_pool()` 返回 `None`，published 数字都在这条基线上测的）。三条不变量比吞吐重要：归还不 `commit` 而 `rollback`；报错/被中止的句柄**丢弃**不还池；池满报 `error_code=pool_unavailable` 而不是 `sql_error`。
> - **PostgreSQL 后端**用 `psycopg` 3、缺失回退 `psycopg2`、都没有则报带 `pip install -e ".[postgres]"` 的 `driver_missing` 工具结果（不是栈追踪）。`create_runtime` 在 `mssql` / `postgres` 上先做一次 `SELECT 1` 自检：没有这一步，一次不可达的 `eval` 会表现成"40 条全失败但 `valid_sql_rate 1.0`"，把连接问题伪装成模型问题。`NL2SQL_DB_BACKEND` 也在配置层拒未知值（写错一个字母就退回演示库、量出一份看起来完全正常的假准确率）。
> - **量到并修掉一处真缺陷**：`configure_pool()` 只接受 `Target`，而 `query()` 接受裸路径——按 `str(path)` 注册的池与按 `SqliteTarget.describe()` 查的池不同名，结果是**池化配置成功、上报正常、然后永远不被使用**。现在两端都过 `coerce_target()`，并加了一条"裸路径注册的池能被 Target 查到"的回归测试。这类缺陷只在 API 边界上出现，mock 测不到。
> - **把"池减少连接"从配置声明变成读数**：`db` 事件带 `connection` / `pooled`，`summarize` 新增 `db_connections` / `db_pooled_calls` / `db_latency_ms`（`turn` 延迟含模型，池化改动只有对着 `db` 行才看得见），`nl2sql-agent metrics` 印 `物理连接 / 池化调用 / 执行层延迟`，`/api/health` 加 `pool`（未建池是空列表——**空列表就是"池没开"这个事实**）。A/B 用冻结集 113 条 gold × 3 遍 = **339 次只读执行**、并发 1/4/8 × 池上限 1/2/4，独立跑两遍连接数逐位相同：并发 8 关池 **339 连接 / p50 0.89 / p95 3.42ms / 墙钟 0.085s** → 开池 4 **4 连接 / 0.68 / 2.88ms / 0.066s**；同一件事再用发布出去的命令复跑（`nl2sql-agent eval` 123 条、单进程 225 次执行）：**物理连接 225 → 1、池化调用 0 → 225、执行层 avg 0.31 → 0.10ms**，而 **eval 七项指标逐位不变**（EX 0.4602、valid 0.7788、拒答 0.90、recall@5 0.94）——不成立就说明池把事务状态带进了下一条查询。诚实边界：本地 SQLite 建连只 ~0.2ms，所以这张表量的是**结构事实**（O(查询数) → O(池上限)）加一个小收益下界，**服务端 TCP + 认证那一截未测**，本机没有可达的 PG / SQL Server。超时句柄被丢弃的代价也在表外：4 次调用（2 次超时）实测 `connects=3 / discards=2`。
>
> **本条没做的事**：PostgreSQL 全部行为跑在注入的假驱动上，**实际 PG 服务器端到端未测**（含只读登录校验与 `statement_timeout` 实际引擎生效）；MySQL 只有登记表一行 + 离线护栏形状规则，**没有驱动、没有执行路径**；`introspect`（`schema-sync`）仍只对接 SQL Server，PG 目录需自备；SQL Server 版本探测没做（`WITH (NOLOCK)` 放行 / `OFFSET/FETCH` 拒绝按"跑在 2008 R2"写死）；池是单进程内的，多进程与异步（P2-3）各自还要再对接触发点（2026-10-23 补：P2-3 已接入 FastAPI 线程池下的每会话锁，**多进程仍未测**）。
>

- [x] 抽象 `Dialect`：标识符引用、分页、日期函数、字符串聚合、`TOP/LIMIT`。**guard 侧（2026-10-23 上一条）与执行侧（本条）都收在 `dialects.py` 一张登记表**；日期函数与字符串聚合的转译仍不在范围内——系统按目标方言生成与拒绝，不做跨方言翻译器。
- [x] 首批支持 SQLite（demo）、SQL Server（现有生产）、PostgreSQL（执行侧，实际引擎端到端未测）。
- [~] 连接池（`NL2SQL_DB_POOL_SIZE`，默认关）+ statement timeout（按方言）已落地；只读账号仍是**部署侧要求**（应用层只有护栏、会话语句与 `mode=ro` / read-only 事务这三层软保证）；"禁止在应用层拼接用户输入"在值检索这一处由 `Dialect.distinct_values()` 的绑定参数保证，模型 SQL 本身按定义是整条文本进护栏。

### P2-3 异步与并发

- [x] 接入 FastAPI 后的会话并发：**同一会话串行、不同会话并行**。`SessionStore` 的 store 锁只保护字典，每个 `session_id` 另有一把 `threading.Lock`；`ask` 在锁内完成"读历史 → 跑 Agent → 追加轮次 → 写 `api` 事件"。`reset()`（`DELETE /api/sessions/{id}`）原地替换 `Conversation` 但保留锁对象，所以排队中的请求不会拿到新锁并越过清空。FastAPI 同步端点在线程池里执行，不同会话天然并行。**没有引入 async Agent**：把同步 SQLite / 工具链整体异步化的收益没有被任何测量支持，而线程池已满足规划里的并发语义。
- [x] LLM 调用与 SQL 执行分别设超时、重试、熔断：LLM 超时沿用 `NL2SQL_REQUEST_TIMEOUT_SEC`（默认 120s）；新增瞬时错误重试与连续失败熔断（`resilience.py`）。SQL 的按方言语句超时在 P2-2 已落地，本阶段只新增 `pool_unavailable` 的窄重试。**SQL 自动熔断刻意不做**——见下方偏差记录。
- [x] 经验库写并发：`initialize()` 切 `journal_mode=WAL`（失败时保留原日志模式而不是拒绝启动），每条连接 `busy_timeout=10000`。迁移 PostgreSQL 仍未做：单进程下 WAL 足够；多进程需要单写者或数据库级迁移。

> 状态（2026-10-23，**离线实现并实测；在线模型的故障注入未做**）：重试/熔断只包装真实 provider，`mock` 不包装——它没有网络故障模式，套一层只会给离线测量加没有测量覆盖的计时代码。瞬时判定沿异常链检查 `TimeoutError` / `ConnectionError`、SDK 类型名、`status_code ∈ {408,409,425,429,500,502,503,504}` 与 `LLMError` 文本标记；`400` 类**绝不重试**。默认重试 1 次（总尝试 2）、退避 2s 起翻倍封顶 30s。熔断按**尝试**计数而非轮次（一次重试失败贡献 2 次失败），默认连续 5 次打开 30s，半开态只放一个探针，成功关闭、失败立刻重开，阈值 0 关闭。打开时抛 `LLMError`，Agent 落成 `status="failed"`。`/api/health` 的 `llm_resilience` 每请求读实时快照（mock 返回 `enabled=false` + 原因）。SQL 只对 `pool_unavailable` 重试（默认 1 次 / 50ms，结果带 `attempts`）；`sql_error` 是模型的问题、`timeout` 重试只会更慢，都不重试。
>
> **与规划文字的一处刻意偏差**：规划写了"LLM 调用与 SQL 执行分别设超时、重试、熔断"，实现里 **SQL 没有自动熔断**。理由是现有错误码无法把"SQL 写错"与"连接层故障"分开，且本机没有可校准的实际 PG / SQL Server 样本；在分不清的输入上再挂门，会把模型的 SQL 错误升级成服务不可用。SQL 侧的"熔断"目前是运维侧的只读账号、语句超时与连接池边界，不是应用层开关。这一点与 P2-2"实际 PG 未端到端测"的记录相连。
>
> **验证矩阵**（2026-10-23 复跑）：`pytest` 四种安装形态 **665 / 644 passed + 3 skipped / 645 + 20 / 624 + 23**（本轮 **+20**：`test_resilience` 10 / `test_experience_concurrency` 6 / `test_web_api` 4；其中同会话串行与异会话并行是在 HTTP 层用 `TestClient` + `ThreadPoolExecutor` 证明的，不是对代码结构的断言）；`guard-check` / `dialect-check` / `alias-check` / `clarify-check` 数字逐位不变、rc 0（零依赖形态下 `guard-check` 自报 AST 未安装、`dialect-check` 把 19 条转译标成"未测量"）；`eval` 六道组合逐位不变（40/40、61/123 EX 0.4602 / valid 0.7788 / 拒答 0.90 / recall@5 0.94、71/133、69/155、76/147、84/179）；P8 泄漏扫描 TABLE / TABLE COMMENT **0 命中**。数字不变的原因是可检验的：mock 路径未被包装、冻结集没有池耗尽、WAL 只改经验库日志模式。
>
> **本条没做的事**：① 并发只在单进程 + FastAPI 线程池里验证，多 worker / 多进程、跨进程会话、经验库多进程迁移未测；② 重试/熔断的故障注入全部来自假 provider，没有对真实 Qwen 端点制造 429/5xx，也没量过重试的额外 token 成本；③ 在线模型基线（EX 0.3514 / 0.3784）在加这层包装之前跑，**未重跑**，不能当作"重试/熔断下的在线模型表现"；④ 池耗尽重试的收益没在真实服务器上量化（本地 SQLite 池几乎不会耗尽）；⑤ 没有 async Agent、没有 arq/Celery 一类任务队列，长任务仍在请求线程里跑完。

### P2-4 人工反馈闭环

> 状态（2026-10-23）：**已落地，CLI 与 Web 双入口**。三个原因码 `result_wrong` / `metric_wrong` / `field_wrong`（"结果不对 / 口径不对 / 字段不对"）。一次反馈落三件事：审核项、失败队列 `user_feedback:<reason>`、反例 `feedback_<reason>`；**被批评的那条 SQL 只进反例、不进候选经验**——它是用户刚刚否定的答案，做成 few-shot 会教坏下一轮。只有用户同时给出修正 SQL 且与原文不同，才额外生成 `source="feedback"` 的 candidate（`verified=False`，不进召回），等审核人接受或替换。
>
> - **去重**：同一 `session_id + turn_index + reason` 的 open 反馈叠 `occur_count`，由部分唯一索引 + `IntegrityError` 折叠兜并发点击。测试钉住"连点两次"只有一条审核项、计数为 2，且第二次不重复建失败/反例。
> - **审核通过**复用 `resolve_failure`：修正 SQL 必须过护栏、再对外接目录只读执行成功，才写 verified 并结案；只凭肉眼不能通过（缺 validator 直接拒绝）。**拒绝**必须给原因，且只撤掉这条反馈自己创建的那条失败/反例——同一问题上更早的执行失败保持 open（有测试用"先真实失败、再反馈、再拒绝"三步钉住）。
> - **Web 入口**：`POST /api/sessions/{id}/feedback` 的 question 与 SQL **取自服务端存的轮次，不取请求体**——客户端不能给没展示过的答案挂反馈；`turn_index` 越界、该轮没有 SQL 结果、建议 SQL 不过护栏均为 422。`GET /api/feedback` 带 `stats` 与原因码表；`POST /api/feedback/{id}/review` 用与 CLI 同一个 validator（护栏 + 真实只读执行），404/400 分别对应不存在与状态冲突。**浏览器实测**（mock + 演示库）：提交 → 审核队列出现（带 trace）→ 通过 toast"已通过并写入 verified 经验" → 第二条拒绝；KPI 从 `0/1/0/1` 到 `0/1/1/2`，一次点击只发一个 review 请求（修掉了一处事件监听器随重绘累加的缺陷，`reviewPosts: 1` 钉住）。
> - **周报** `experience report`：Top 失败问题、反馈按原因/审核状态、schema 变更影响（active / 陈旧经验数、旧 hash 数、反馈与失败的陈旧 open 数）。默认只出 **sha256 指纹**且 SQL 永不出现，`--show-text` 才带本地原文。**经验复用率来自事件日志窗口**（`turn` 行 `few_shot.injected > 0` 的占比），不拿库内终身命中数冒充——两者单位不同，混在一起是一次静默的单位变更；没有 `NL2SQL_LOG_PATH` 时明确报"不可计算"而不是 0。实测带日志的一次周报：复用率 **100.0%（1/1 轮注入）**、窗口 tokens 0、库内累计命中 2；schema 影响 当前 `6c6906abd54e`、经验 active 1 / 陈旧 0。
> - **验证矩阵**：`pytest` 四种安装形态 **683** / **655 + 4 skipped** / **663 + 20 skipped** / **635 + 24 skipped**（本轮 **+18**：`test_feedback.py` 11 / `test_web_feedback.py` 7）；eval 六组与四台仪器逐位未变（门禁 40/40、全量 61/123 EX 0.4602、71/133、69/155、76/147、84/179；`guard-check` / `dialect-check` / `alias-check` / `clarify-check` 全 rc 0）；P8 泄漏扫描 TABLE / TABLE COMMENT **0 命中**。数字不变是可解释的：反馈只写经验库，不改 Agent 的生成路径，audit 语料未动。
>
> **与规划文字的一处偏差**：规划写"反馈 → 自动落候选经验"；实现里落的是**失败 + 反例 + 审核项**，候选经验只在用户同时给出修正 SQL 时生成。被否定的 SQL 绝不能成为候选——"自动落候选"会把最该被记住的坏答案变成 few-shot 素材。
>
> **本条没做的事**：① UI 验证全部在 `mock` + 演示库上完成，**没有真实用户反馈数据**，也没有在线模型上"反馈 → 经验 → 复用"对 EX 的影响测量；② 经验复用率依赖 `NL2SQL_LOG_PATH`，未配置时这一项只能给"不可计算"；③ 审核通过要求人工给出 SQL，没有"模型自动修复后提交审核"的草稿通道；④ 单进程 SQLite，多进程并发写与更大的审核队列规模未测；⑤ 周报只按 schema hash 统计失效，跨 schema 的旧经验标记而不删除；⑥ 反馈测试由本项目编写，属于产品测试而非评测语料。

- [x] 前端“结果不对/口径不对/字段不对”反馈 → 自动落候选经验 + 失败分类。（落地形态：审核项 + 失败 + 反例；候选经验仅在用户给出修正 SQL 时生成且不可召回。）
- [x] 审核队列：通过/修改 SQL/拒绝；通过后进入 verified。（通过必须带修正 SQL，并过护栏 + 真实只读执行。）
- [x] 周报：Top 失败问题、经验复用率、schema 变更影响。（复用率取自事件日志窗口；无日志路径时明确报不可计算。）

### P2-5 缓存与成本

> 状态（2026-10-23，**已落地并实测；在线模型的 token 节省与前置缓存命中未测**）：三件事都做了，但都按"宁可少报"落地。结果缓存只存**已过护栏且执行成功**的答案，命中跳过的是模型循环而不是护栏；键含归一化问题、schema hash、principal、`allow_sensitive`、策略指纹（prompt 版本 / few-shot / 值对齐 / 澄清 / 改写轮数 / 双通道 / 行限 / 修复上限）与会话前缀，所以权限或目录一变就自然换键。默认 `NL2SQL_RESULT_CACHE_TTL_SEC=0` **关闭**：默认开启会让第一轮的行为取决于之前跑过什么，空历史逐字节相等的确定性比默认省几次调用更重要。单价不内置任何厂商价格，`NL2SQL_PRICE_INPUT_PER_1M` / `_OUTPUT_PER_1M` 只给一半直接报错，未配置时费用是 `None` 并印"费用未配置"而不是 0。
>
> - **结果缓存**：`ResultCache`（`cache.py`）= 有界 LRU + 每项 TTL，`time.monotonic` 可注入；命中返回深拷贝（`cached=true`、`tokens/steps=0`、`tool_trace=[]`、新 `trace_id`、`cache={hit,age_sec,saved_tokens,saved_steps,saved_elapsed_ms}`）；澄清、拒答、失败不入缓存。评测器固定 `use_cache=False`，六道跑分逐位不变。
> - **读数台 `cache-check`**：临时强制 TTL=60、关闭日志，量冷启动→同问→**空白/标点归一化变体**→不同问法；再建两次 `SchemaIndex` 量 embedding 缓存；最后量 system prompt 的稳定前缀比例与 few-shot 位置。实测：冷 `ok/6 步/8.14ms` → 热 `cached/0 步/0.07ms`；归一化变体命中、不同问法不命中；`entries=2 hits=2 misses=2 hit_rate=0.5`；embedding 第一次 `1 次调用 / 1ms`、第二次 `0 次调用 / 8ms`、`provider_calls_total=1`；prompt **2205 字符中 2090 稳定（94.78%）、波动 115、few-shot 在尾部**。该比例只是**布局测量**，网关是否命中要看真实 provider 的 `usage.cached_tokens`。
> - **embedding 缓存**是既有能力的测量而非新缓存：`DocumentIndex` / `SchemaIndex` 按 `(schema_hash, provider, docs_hash)` 落盘，文档指纹变了整体重建；`cache-check` 只证明"第二次零 provider 调用"。
> - **prompt 前缀布局**：few-shot 块移到 system prompt 最尾部（JSON 输出契约之后），`AGENT_PROMPT_VERSION` 随之 bump 到 **`agent-v5`**——prompt 内容变了，不 bump 会让报告无法回溯。前缀缓存在兼容端点是 provider 侧自动能力，本项目只做布局与读数，不另做本地缓存。
> - **token 拆分与成本**：`usage_breakdown` 保留 prompt/completion/cached，未分类 = total − prompt − completion（cached 夹紧在 prompt 内）；`summarize` 新增 `token_usage` / `cache`，`metrics` 多印两行。`cost_summary` 只吃 `turn` 事件，按业务域/用户分桶，费用 = 非缓存 prompt×输入价 + cached×缓存价（未配则按输入价）+ completion×输出价 + **未分类×输入价（下界）**；没配价格时 `estimated_cost=None`。
> - **入口**：CLI `cost`（`--by` / 三档单价 / `--json` / 半张表 rc 2）、`cache-check`（`--json`）；Web `GET /api/cost` + 新"成本看板"（4 KPI、业务域↔用户切换、单价说明），`/api/health` 增加 `result_cache` 与 `pricing`。
> - **只有真跑才暴露的缺陷**：配置单价后报告里的 `pricing` 是价格表本身、没有 `configured` 字段，而 CLI 人类输出与 Web KPI 都读 `pricing.configured`——表格逐行有费用、头部却写"未配置"。已把 `configured` 变成报告字段（配置时 `true`），并补 CLI 人类输出与 Web 报告断言。浏览器复跑：3 轮 / 1800 token / 命中率 33.3% / **0.005775 USD**，业务域 `sales 0.004575`、`inventory 0.001200`，切换用户 `alice 0.004250`、`bob 0.001525`。
>
> **与规划文字的两处偏差**：① "LLM prompt 前缀缓存"没有做本地缓存——前缀命中在兼容端点是 provider 侧自动的，本地再缓存一份只会测到字符串拼接速度，测不到真实命中；② 结果缓存默认 TTL=0 关闭，而不是"默认短 TTL 开启"。
>
> **验证矩阵**（2026-10-23 复跑）：`pytest` 四种安装形态 **714 / 683 passed + 5 skipped（藏 Web）/ 694 + 20（藏 sqlglot）/ 663 + 25（零可选依赖）**（本轮 **+31**：`test_cache` 15 / `test_costs` 13 / `test_web_cost` 3）；六道 eval 组合逐位不变（40/40、61/123 EX 0.4602、71/133、69/155、76/147、84/179），准确率回归门禁 rc 0；`guard-check` / `dialect-check` / `alias-check` / `clarify-check` 数字逐位不变、rc 0；`metrics` 重跑 825 行、坏行 0、`prompt_version=agent-v5`，池关 225 物理连接 / 执行层 avg 0.34ms、池 4 为 1 / 225 / 0.13ms；P8 泄漏扫描 TABLE / TABLE COMMENT **0**（列名是 133 个通用英文词，INFRA 仅公开 base URL）。
>
> **本条没做的事**：① `mock` 没有 usage 块，**真实 token 节省为 0**；浏览器与 CLI 的成本数字来自合成事件日志，只证明折算与展示，不是任何真实账单；② prompt 前缀是否被网关命中、命中率多少未测（需要真实 Key）；③ 缓存是**单进程内存**，没有 Redis/跨进程共享与主动失效接口，TTL 内的数据变化会看到旧结果；④ 费用是估算下界，不处理阶梯价、批处理折扣与币种换算，也没有预算告警/配额；⑤ 成本看板只在 mock + 合成日志上做过浏览器验证；⑥ `cost` / `cache-check` 不进 CI，是测量台不是门禁。

- [x] 相同归一化问题 + schema hash + 权限上下文 → 结果缓存（短 TTL）。（默认关闭；键另含策略指纹与会话前缀；只存已过护栏且执行成功的答案。）
- [x] Schema/经验 embedding 缓存；LLM prompt 前缀缓存。（embedding 缓存复用既有 `DocumentIndex` / `SchemaIndex` 并用 `cache-check` 测量；prompt 侧只做稳定前缀布局与 `cached_tokens` 读数，不做本地缓存。）
- [x] 成本看板：按业务域/用户统计 token 与费用。（单价由部署配置，未配置只报 token；CLI `cost` + Web 成本看板共用同一个折线。）

### P2-6 发布与回归

- 每次 prompt/schema/guard 变更跑冻结集 + 对抗集；阈值不达标禁止合并。
- 灰度：新 prompt 先对 10% 流量，比较 EX 与升级率。
- 版本化：prompt、schema、experience 都有版本号，可回滚。

> 状态（2026-10-22，**CI 门禁覆盖面已按实测补齐**）：用 `sys.meta_path` 拦依赖、在一台机器上复现三种安装后量出两个缺陷——① CI 只装 `.[dev]`，`tests/test_web_api.py` 整个文件 import-skip，**Web 控制台面 13 条测试长期不参与门禁**（353 passed / 1 skipped）；② 没装 sqlglot 时 `guard-check --json` 的 `gold_sql.ast` 输出 `total 113 / errors 0 / warnings 0`，一个**没跑的**审计块和一个**干净的**审计块在 JSON 里完全一样，同时 `test_permission_suite_blocks_and_never_leaks_through_the_view` 因硬编码 `17` 直接失败（实测 16：`perm_rep_ast_combo` 的 `FROM[internal_audit]` 只有解析器看得见）。修法：`GoldAudit.measured` 布尔位 + 权限断言跟随 `ast_measured` + CI 主 job 改装 `.[dev,web]`（实测 366 passed / 0 skipped）+ 新增 `core-only` job 跑 `pip install -e . pytest` 的零依赖路径并断言降级必须自报（335 passed / 19 skipped、`ast_measured is false`、`gold_sql.ast.measured is false`、权限 16（**→ 2026-10-23 起这一位恒为 17**：方言引用规则让 `FROM[internal_audit]` 在正则层就看得见，CI 里 `== 16` 的断言已改成 17 并注明"再出现 16 就是那条规则退化了"）、`failures` 全 0）。**仍未做**：① 数字来自本机模拟，GitHub Runner 上这两个 job 的真实绿/红要在首次推送后确认；② "阈值不达标禁止合并"当时只有 `v1_baseline` 40/40 这一条硬阈值，EX 相对上一版本不回退 2pp 的门禁没有实现（下一条已落地）；③ 灰度与 prompt/schema/experience 三者版本号联动回滚仍是纸面设计（只有 `AGENT_PROMPT_VERSION` 与 schema hash 真实存在）。


> 状态（2026-10-23，**"跑冻结集 + 对抗集"这一步现在才真的有语料可跑，但 CI 仍不跑它**）：`--adversarial`（24 条）与 `--paraphrase`（32 条）都已可作为独立跑分入口，`eval --adversarial` 退出码 1（有失败用例）、带上门禁则退出码 2（`语料规模不同`，三种组合各钉了一条测试）。CI 的两道门禁不变：`--only-tag v1_baseline` 按通过数拦、全量 123 条按指标差分拦。把两份测量语料并进门禁需要**先人工逐条审核**（§0：AI/本会话生成的用例不得自动并入冻结集），再刷新 `benchmarks/ex_baseline.json` 的 `total/answerable`——这是有意的顺序：测量语料可以立刻产生数字，但不能立刻获得拦合并的权力。

> 状态（2026-10-23，**P2-6 三条验收全部落地，但"真实流量上的灰度结论"仍未测得**）：① 灰度：`gray.py` 用 `sha256(salt:key) mod 10000` 做确定性分桶，Web 按会话、CLI `chat` 按会话、`ask`/库调用按问题文本；`0/100` 精确，候选臂 `agent-v5-fewshot-early` 只把 few-shot 移到 JSON 契约之前，基线 `agent-v5` 逐字节未动。`metrics` 新增 `by_prompt_version`，日志里出现两个以上版本时多印一行 `灰度对比`（ok / 澄清 / 升级），EX 仍只能用 `eval --prompt-version <arm>` 分臂跑。实测 2000 个合成 key 在 10% 下命中 212（10.6%）；两条命令各跑一遍 123 条，两臂都是 61/123、EX 0.4602、825 行事件、坏行 0——但 `mock` 不读 system prompt，**这不是 prompt 质量结论**。② 版本化：`release snapshot` 把 prompt 版本/指纹、schema 文件与经验库（SQLite 在线备份）写进 `<home>/releases/<id>/` 并逐项记 sha256；`release rollback --yes` 覆盖前另存 `var/release_backups/`，恢复 schema 与经验库（实测 1→2→1 条），prompt **只比对不恢复**：快照没有正文，需 checkout 代码并重启，文件已回滚但 prompt 不匹配时明确返回 **rc 3**。③ 测试与证据：+23 条（`test_gray` 14 / `test_release` 9），全量 **737 passed**，四种安装形态 737 / 706+5 / 717+20 / 686+25；P8 扫描 TABLE / TABLE COMMENT 0。**仍未做**：真实 provider 的灰度 EX/升级率、自动回滚触发器、远端制品库与签名、GitHub Runner 上的真实 CI 绿/红（本机未推送）；对抗集仍未进门禁，原因同上一条。

---

## 5. 里程碑建议

| 阶段 | 时间 | 交付 | 退出标准 |
|---|---|---|---|
| M1 可信基线 | 2 周 | AST guard、100+ 评测集、在线模型基线 | 对抗集 0 漏放；EX 基线可复现 |
| M2 准确率 | 3 周 | 混合 schema linking、值对齐、多轮 | 同集 EX 相对提升 ≥ 8pp |
| M3 记忆闭环 | 2 周 | 经验 embedding、反例、A/B 归因 | few-shot 有显著增益；无错误经验召回 |
| M4 产品化 | 3 周 | 权限、可观测性、反馈队列、SQL Server 适配 | 通过安全评审；P95 延迟达标 |

> 时间是小团队估算，按实际人力调整；每阶段结束必须更新本文件与评测报告。

---

## 6. 明确不做 / 反模式

| 反模式 | 为什么不做 |
|---|---|
| 让 Agent 自动把成功结果标为 verified | 会形成自我强化闭环；错误答案被反复召回 |
| 用 SQL 字符串相似度代替结果集评测 | 等价写法多，会误杀；无法发现口径错误 |
| 护栏失败时让模型“绕过”或重试到通过 | 把权限问题当成生成问题，最终越权 |
| 执行报错就盲目加表/加字段“修” | 引入新幻觉；修复必须可回退、不扩权 |
| 为了单条难例堆 prompt 规则 | 过拟合，伤泛化；应按失败类别系统性修 |
| 只看平均准确率，不看拒答与人工升级率 | 鼓励模型瞎答；业务上不可用 |
| 把 mock 的 100% 当作在线模型效果 | 误导面试/汇报；必须标注 provider 与数据集 |
| 在提示词中塞全量 schema | token 爆炸、注意力稀释；应做 schema linking |

---

## 7. 如何贡献一项改进

1. 在本文档认领条目，写清假设与验收指标。
2. 在 `tests/` 增加至少一个失败用例（先红后绿）。
3. 评测集新增对应难例，跑全量报告。
4. 更新 README/ARCHITECTURE 中受影响的行为描述。
5. PR 描述附：变更前后指标、失败类别变化、成本变化、回滚方式。
