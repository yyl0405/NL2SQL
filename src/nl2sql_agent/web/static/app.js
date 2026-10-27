"use strict";

const api = {
  health: () => request("/api/health"),
  createSession: () => request("/api/sessions", { method: "POST", json: {} }),
  resetSession: (id) => request(`/api/sessions/${id}`, { method: "DELETE" }),
  ask: (id, question) =>
    request(`/api/sessions/${id}/ask`, { method: "POST", json: { question } }),
  askStream: (id, question, onStep) => streamAsk(id, question, onStep),
  schema: () => request("/api/schema"),
  searchSchema: (q, topK = 5) => request(`/api/schema/search?q=${encodeURIComponent(q)}&top_k=${topK}`),
  experience: (status, limit = 30) =>
    request(`/api/experience?limit=${limit}${status ? `&status=${status}` : ""}`),
  failures: (status = "open", limit = 30) =>
    request(`/api/failures?status=${status}&limit=${limit}`),
  feedback: (status = "open", limit = 30) =>
    request(`/api/feedback?status=${status}&limit=${limit}`),
  submitFeedback: (id, payload) =>
    request(`/api/sessions/${id}/feedback`, { method: "POST", json: payload }),
  reviewFeedback: (id, payload) =>
    request(`/api/feedback/${id}/review`, { method: "POST", json: payload }),
  evalCases: () => request("/api/eval/cases"),
  runEval: (body) => request("/api/eval/run", { method: "POST", json: body }),
  reports: () => request("/api/eval/reports"),
  report: (name) => request(`/api/eval/reports/${encodeURIComponent(name)}`),
  cost: () => request("/api/cost"),
};

async function request(path, { method = "GET", json } = {}) {
  const response = await fetch(path, {
    method,
    headers: json ? { "content-type": "application/json" } : undefined,
    body: json ? JSON.stringify(json) : undefined,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      if (payload && payload.detail) detail = payload.detail;
    } catch (error) {
      /* keep the status line */
    }
    throw new Error(detail);
  }
  return response.json();
}

// SSE frames arrive as arbitrary byte slices, so a frame is only parsed once its
// terminating blank line has been buffered.
function drainSse(buffer, handlers) {
  let text = buffer;
  for (;;) {
    const split = text.indexOf("\n\n");
    if (split < 0) break;
    const frame = text.slice(0, split);
    text = text.slice(split + 2);
    let event = "message";
    let data = "";
    for (const line of frame.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (!data) continue;
    let payload = {};
    try {
      payload = JSON.parse(data);
    } catch (error) {
      continue;
    }
    handlers(event, payload);
  }
  return text;
}

async function streamAsk(sessionId, question, onStep) {
  const response = await fetch(`/api/sessions/${sessionId}/ask/stream`, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "text/event-stream" },
    body: JSON.stringify({ question }),
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      if (payload && payload.detail) detail = payload.detail;
    } catch (error) {
      /* keep the status line */
    }
    const error = new Error(detail);
    // 404/405 means the running server predates the stream route (a stale process
    // still serves the new static JS from disk); a proxy that refuses SSE looks the
    // same. Neither is a reason to lose the answer - fall back to the plain turn.
    error.fallbackToPlain = response.status === 404 || response.status === 405;
    throw error;
  }
  if (!response.body || typeof response.body.getReader !== "function") {
    const error = new Error("streaming unsupported");
    error.fallbackToPlain = true;
    throw error;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let outcome = null;
  let failure = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer = drainSse(buffer + decoder.decode(value, { stream: true }), (event, data) => {
      if (event === "step" && onStep) onStep(data);
      else if (event === "result") outcome = data;
      else if (event === "error") failure = new Error(data.message || "本轮执行失败");
    });
  }
  if (failure) throw failure;
  if (!outcome) {
    const error = new Error("流式响应没有返回结果");
    error.fallbackToPlain = true;
    throw error;
  }
  return outcome;
}

// A blocked stream must not cost the user the answer: fall back to the plain
// endpoint, which runs the identical turn without the step feed.
async function streamFallback(id, question, onStep) {
  if (onStep) onStep({ tool: "ask", ok: true, summary: "流式通道不可用（服务需重启加载新路由），已改用整体请求", elapsed_ms: 0, step: 0 });
  return api.ask(id, question);
}

const state = {
  health: null,
  route: "chat",
  sessionId: null,
  busy: false,
  chat: { messages: [], slots: null, selected: -1, error: "", pending: null },
  eval: { report: null, running: false, error: "", filter: "all", multiTurn: true, judge: false, reports: [], page: 1, pageSize: 30 },
  schema: { data: null, selected: null, query: "", hits: [], error: "", page: 1, pageSize: 30, relPage: 1, relPageSize: 30 },
  cost: { report: null, meta: null, by: "category", error: "" },
  memory: {
    tab: "experience",
    expStatus: "verified",
    exp: { items: [], stats: null },
    failStatus: "open",
    fail: { items: [], stats: null },
    fbStatus: "open",
    fb: { items: [], stats: null },
    error: "",
  },
};

const VIEW_META = {
  chat: ["多轮问数"],
  eval: ["评测验证"],
  schema: ["表结构浏览"],
  cost: ["成本看板"],
  memory: ["经验与失败"],
};

const EXAMPLES = [
  "2026年6月华东地区销售订单总金额是多少？",
  "库存低于安全库存的物料有哪些？",
  "各区域销售额排名前3",
  "ORD-1002 的物料齐套情况怎么样？",
  "6月采购到货数量按供应商统计",
];

const SLOT_LABELS = {
  region: "区域", customer: "客户", material: "物料", order_id: "订单",
  month: "月份", date: "日期", limit: "条数", dimension: "维度",
  metric: "指标", direction: "排序",
};

const TOOL_ZH = {
  search_tables: "检索表",
  get_table_schema: "看字段",
  search_values: "取值对齐",
  search_experience: "召回经验",
  validate_sql: "护栏校验",
  execute_sql: "只读执行",
  repair_sql: "修复 SQL",
  save_experience: "存候选经验",
  generate_sql: "生成 SQL",
  phrase_answer: "结果措辞",
  pipeline: "管道升级",
  ask: "整体请求",
};

const STATUS_LABELS = {
  ok: "已回答", needs_clarification: "需澄清", refused: "拒答",
  needs_human: "转人工", failed: "执行失败", max_steps: "步数耗尽",
};

const REWRITE_LABELS = { none: "未改写", rule: "规则", llm: "模型", clarify_reply: "澄清回问" };
const EXPERIENCE_STATUS_LABELS = { verified: "已验证", candidate: "候选" };
const FAILURE_STATUS_LABELS = { open: "待处理", resolved: "已结案" };
const FEEDBACK_STATUS_LABELS = { open: "待审核", approved: "已通过", rejected: "已拒绝" };
const KIND_LABELS = { single: "单轮", multi: "多轮" };
const JUDGE_STATUS_LABELS = { not_run: "未运行", ok: "已运行", skipped: "已跳过", error: "失败" };
const SOURCE_LABELS = { agent: "智能体", human: "人工", feedback: "反馈", manual: "人工", seed: "预置" };
const DIFFICULTY_LABELS = { easy: "简单", medium: "中等", hard: "困难" };
const REWRITER_LABELS = { auto: "自动", rule: "规则", llm: "模型", off: "关" };
const CATEGORY_LABELS = {
  metric: "指标", bom: "物料清单", kitting: "齐套", inventory: "库存", purchase: "采购",
  production: "生产", ranking: "排名", detail: "明细", group_by: "分组", time_filter: "时间筛选",
  scope: "范围", clarification: "澄清", sales: "销售", customer: "客户", supplier: "供应商",
  material: "物料", order: "订单", unknown: "未知",
};

function rewriteLabel(source) {
  return REWRITE_LABELS[source] || source || "";
}

function categoryLabel(category) {
  return CATEGORY_LABELS[category] || category || "未知";
}

function relationParts(relation) {
  const [fromTable, ...fromRest] = String(relation.left || relation.from_table || "").split(".");
  const [toTable, ...toRest] = String(relation.right || relation.to_table || "").split(".");
  return {
    fromTable,
    fromColumn: fromRest.join("."),
    toTable,
    toColumn: toRest.join("."),
    kind: relation.type || relation.kind || "",
  };
}

const METRIC_LABELS = {
  overall_accuracy: ["总体准确率", "pct", "通过用例 / 总用例（含拒答与澄清）"],
  execution_accuracy: ["执行准确率", "pct", "结果集与 gold SQL 完全一致，主指标"],
  valid_sql_rate: ["合法 SQL 率", "pct", "生成的 SQL 通过只读护栏"],
  execution_success_rate: ["执行成功率", "pct", "护栏通过后数据库真的能跑"],
  refusal_accuracy: ["拒答准确率", "pct", "超范围问题被正确拒绝"],
  clarification_accuracy: ["澄清准确率", "pct", "信息不足时正确发起澄清"],
  multi_turn_accuracy: ["多轮准确率", "pct", "会话用例最后一轮通过比例"],
  table_recall_at_5: ["表召回@5", "pct", "BM25 schema linking 命中期望表"],
  avg_steps: ["平均步数", "num", "每题工具调用轮数"],
  avg_latency_ms: ["平均耗时", "ms", "离线 mock 链路，不含模型网络时间"],
  avg_tokens: ["平均 token", "num", "真实 provider 才有意义"],
};

/* ---------- helpers ---------- */

function h(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
}

function pct(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function num(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toFixed(digits);
}

function runAt(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function metricFormat(key, value, kind) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  if (kind === "pct") return pct(value);
  if (kind === "ms") return `${num(value)} ms`;
  if (kind === "num") return key === "avg_tokens" ? Math.round(value).toLocaleString() : num(value);
  return String(value);
}

function chip(text, tone) {
  return `<span class="chip${tone ? ` chip--${tone}` : ""}">${h(text)}</span>`;
}

function statusChip(status) {
  const tone = status === "ok" ? "ok"
    : status === "needs_clarification" ? "warn"
      : status === "refused" ? "info" : "bad";
  return chip(STATUS_LABELS[status] || status || "—", tone);
}

const SQL_KEYWORDS =
  /\b(with|select|from|where|group by|order by|limit|join|left join|inner join|on|as|and|or|not in|in|case|when|then|else|end|desc|asc|having|union|distinct)\b/gi;

function highlightSql(sql) {
  return h(sql || "")
    .replace(/(--[^\n]*)/g, '<span class="cm">$1</span>')
    .replace(/&#39;([^&]*?)&#39;/g, '<span class="str">&#39;$1&#39;</span>')
    .replace(SQL_KEYWORDS, (match) => `<span class="kw">${match}</span>`);
}

function sqlBlock(sql, label = "SQL") {
  if (!sql) return "";
  return `<div class="sql-block">
    <div class="sql-block__head">
      <span class="small mono muted">${h(label)}</span>
      <button class="btn btn--ghost btn--sm" data-copy="${encodeURIComponent(sql)}">复制</button>
    </div>
    <pre class="sql">${highlightSql(sql)}</pre>
  </div>`;
}

function toast(message) {
  const host = document.getElementById("toasts");
  const node = document.createElement("div");
  node.className = "toast";
  node.textContent = message;
  host.appendChild(node);
  setTimeout(() => node.remove(), 2200);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制到剪贴板");
  } catch (error) {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
    toast("已复制到剪贴板");
  }
}

function el(root, selector) {
  return root.querySelector(selector);
}

function on(root, event, handler) {
  root.addEventListener(event, handler);
}

function skeleton(rows = 3) {
  return `<div class="card__body grid">${'<div class="skel"></div>'.repeat(rows)}</div>`;
}

function empty(text) {
  return `<div class="empty">${h(text)}</div>`;
}

function pagerMarkup(kind, page, pageSize, total) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const current = Math.min(Math.max(1, page), pages);
  const numbers = [];
  for (let index = 1; index <= pages; index += 1) {
    if (index === 1 || index === pages || Math.abs(index - current) <= 2) numbers.push(index);
    else if (numbers[numbers.length - 1] !== "...") numbers.push("...");
  }
  return `<div class="pager" data-pager="${h(kind)}">
    <button class="pager__btn" data-page="${current - 1}" type="button" ${current <= 1 ? "disabled" : ""} aria-label="上一页">‹</button>
    ${numbers.map((number) => number === "..."
    ? '<span class="pager__dots">…</span>'
    : `<button class="pager__btn${number === current ? " is-active" : ""}" data-page="${number}" type="button">${number}</button>`).join("")}
    <button class="pager__btn" data-page="${current + 1}" type="button" ${current >= pages ? "disabled" : ""} aria-label="下一页">›</button>
    <label class="pager__jump">到第 <input class="field num pager__input" type="number" min="1" max="${pages}" value="${current}" aria-label="页码"> 页</label>
    <button class="btn btn--sm" data-jump="1" type="button">确定</button>
    <span class="pager__total">共 ${total} 条</span>
    <select class="field pager__size" aria-label="每页条数">${[30, 50, 100].map((size) => `<option value="${size}"${size === pageSize ? " selected" : ""}>${size}条/页</option>`).join("")}</select>
  </div>`;
}

function bindPager(root, onChange) {
  if (!root) return;
  on(root, "click", (event) => {
    const pager = event.target.closest("[data-pager]");
    if (!pager) return;
    const pageButton = event.target.closest("[data-page]");
    if (pageButton && !pageButton.disabled) {
      onChange({ page: Number(pageButton.dataset.page) });
      return;
    }
    if (event.target.closest("[data-jump]")) {
      const value = Number(pager.querySelector(".pager__input")?.value);
      if (Number.isFinite(value)) onChange({ page: Math.max(1, Math.floor(value)) });
    }
  });
  on(root, "change", (event) => {
    const size = event.target.closest(".pager__size");
    if (size) onChange({ pageSize: Number(size.value) });
  });
}

function addCmd(item) {
  return `nl2sql-agent experience add --question ${JSON.stringify(item.question)} --sql ${JSON.stringify(item.sql)} --category ${item.category || "unknown"} --verified`;
}

function resolveCmd(item) {
  return `nl2sql-agent experience resolve ${item.id} --sql "SELECT ..." --note "口径说明"`;
}

function reviewCmd(item) {
  return `nl2sql-agent experience review ${item.id} --approve --sql "SELECT ..." --note "口径说明"`;
}

function slotsChips(slots) {
  if (!slots) return "";
  const parts = Object.entries(SLOT_LABELS)
    .filter(([key]) => slots[key] !== "" && slots[key] !== 0 && slots[key] != null)
    .map(([key, label]) => chip(`${label} ${slots[key]}`, "accent"));
  return parts.length ? `<div class="slots">${parts.join("")}</div>` : `<span class="muted">—</span>`;
}

function resultTable(columns, rows, limit = 50) {
  if (!columns || !columns.length) return "";
  const shown = rows.slice(0, limit);
  const numeric = columns.map((_, index) => rows.some((row) => typeof row[index] === "number")
    && rows.every((row) => row[index] === null || row[index] === undefined || typeof row[index] === "number"));
  const head = columns.map((name, index) => `<th${numeric[index] ? ' class="num"' : ""}>${h(name)}</th>`).join("");
  const body = shown
    .map((row) => `<tr>${row
      .map((cell) => {
        const numeric = typeof cell === "number";
        const text = cell === null || cell === undefined ? "" : String(cell);
        return `<td class="${numeric ? "num" : ""}"${numeric ? ' data-sort-value="' + h(text) + '"' : ""}>${h(text)}</td>`;
      })
      .join("")}</tr>`)
    .join("");
  const more = rows.length > limit
    ? `<div class="small muted" style="padding:6px 12px">仅显示前 ${limit} 行，共 ${rows.length} 行</div>`
    : "";
  return `<div class="table-wrap"><table class="data"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>${more}`;
}

/* ---------- shell ---------- */

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("nl2sql-theme", theme);
}

function setStatus(text, bad) {
  document.getElementById("status-text").textContent = text;
  document.getElementById("status-dot").className = `banner__dot${bad ? " banner__dot--bad" : ""}`;
}

function renderRailMeta() {
  const health = state.health;
  const node = document.getElementById("rail-meta");
  if (!health) {
    node.innerHTML = `<div class="meta__row"><span class="meta__k">运行时未就绪</span></div>`;
    return;
  }
  const guard = health.guard || {};
  const rows = [
    ["提供方", health.provider === "mock" ? "模拟（离线）" : `openai / ${health.model}`],
    ["提示词", health.prompt_version],
    ["表结构", health.schema_hash],
    ["角色", guard.principal ? guard.principal : "不限"],
    ["语法树护栏", guard.ast_guard === "on" ? "开（读不懂即拒）" : "关"],
    ["方言", health.dialect],
    ["基准日", health.as_of_date],
    ["单轮用例", `${health.single_turn_cases} 条`],
    ["多轮会话", health.multi_turn_enabled ? `开 · ${REWRITER_LABELS[health.rewriter] || health.rewriter}` : "关"],
    ["少样本", health.few_shot_enabled === false ? "关" : "开"],
    ["智能评审", health.judge_enabled ? "已配置" : "未启用"],
    ["在线会话", `${health.sessions}`],
  ];
  node.innerHTML = rows
    .map(([k, v]) => `<div class="meta__row"><span class="meta__k">${h(k)}</span><span class="meta__v" title="${h(v)}">${h(v)}</span></div>`)
    .join("");
  document.getElementById("brand-prompt").textContent = health.prompt_version;
}

async function loadHealth() {
  try {
    state.health = await api.health();
    renderRailMeta();
    setStatus(state.health.provider === "mock" ? "离线模拟" : `已连接 ${state.health.model}`, false);
    document.getElementById("status-banner").title = state.health.provider === "mock"
      ? "内置规则模型：不联网、不调用大模型，仅用于本地演示。要真实问数请切换 openai provider。"
      : `已连接模型 ${state.health.model}`;
  } catch (error) {
    setStatus("后端不可用", true);
    toast(`健康检查失败：${error.message}`);
  }
}

function routeOf(hash) {
  const name = String(hash || "").replace(/^#\/?/, "").split("?")[0];
  return VIEW_META[name] ? name : "chat";
}

async function navigate({ force = false } = {}) {
  const route = routeOf(location.hash);
  if (!force && route === state.route && viewHasData(route)) return;
  state.route = route;
  document.querySelectorAll(".nav__item").forEach((button) => {
    button.setAttribute("aria-current", String(button.dataset.route === route));
  });
  const [title] = VIEW_META[route];
  document.getElementById("view-title").textContent = title;
  const view = document.getElementById("view");
  view.className = `view${route === "chat" ? " view--flush" : ""}`;
  view.innerHTML = `<div class="wrap">${skeleton(4)}</div>`;
  try {
    if (route === "chat") await renderChat(view);
    else if (route === "eval") await renderEval(view);
    else if (route === "schema") await renderSchema(view);
    else if (route === "cost") await renderCost(view);
    else await renderMemory(view);
  } catch (error) {
    view.className = "view";
    view.innerHTML = `<div class="wrap"><div class="notice notice--bad">页面加载失败：${h(error.message)}</div></div>`;
  }
}

function viewHasData(route) {
  if (route === "chat") return state.chat.messages.length > 0;
  if (route === "eval") return Boolean(state.eval.report);
  if (route === "schema") return Boolean(state.schema.data);
  if (route === "cost") return Boolean(state.cost.report);
  return Boolean(state.memory.exp.stats || state.memory.fail.stats || state.memory.fb.stats);
}

/* ---------- chat ---------- */

async function ensureSession() {
  if (state.sessionId) return state.sessionId;
  const created = await api.createSession();
  state.sessionId = created.session_id;
  await refreshHealthSessions();
  return state.sessionId;
}

async function refreshHealthSessions() {
  if (!state.health) return;
  try {
    const health = await api.health();
    state.health = { ...state.health, sessions: health.sessions };
    renderRailMeta();
  } catch (error) {
    /* non-fatal */
  }
}

async function renderChat(view) {
  await ensureSession();
  view.innerHTML = `
    <div class="chat">
      <div class="chat__scroll" id="chat-scroll"><div class="chat__inner" id="chat-inner"></div></div>
      <div class="composer">
        <div class="composer__row">
          <textarea id="chat-input" rows="1" placeholder="问点什么…" aria-label="提问"></textarea>
          <button class="btn btn--primary" id="chat-send" type="button">发送</button>
          <button class="btn" id="chat-reset" type="button" title="清空会话">新会话</button>
        </div>
        <div class="examples" id="chat-examples">
          ${EXAMPLES.map((q) => `<button class="chip btn-chip" type="button" data-example="${h(q)}">${h(q)}</button>`).join("")}
        </div>
      </div>
    </div>
    <aside class="detail" id="chat-detail"></aside>`;

  paintChat();

  const input = el(view, "#chat-input");
  const send = el(view, "#chat-send");
  on(send, "click", () => submit(input.value));
  on(input, "keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit(input.value);
    }
  });
  on(input, "input", () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 140)}px`;
  });
  on(el(view, "#chat-reset"), "click", resetChat);
  on(el(view, "#chat-examples"), "click", (event) => {
    const button = event.target.closest("[data-example]");
    if (!button) return;
    input.value = button.dataset.example;
    input.focus();
  });
  const inner = el(view, "#chat-inner");
  const selectTurn = (event) => {
    const feedback = event.target.closest("[data-feedback]");
    if (feedback) {
      submitFeedback(Number(feedback.dataset.turn), feedback.dataset.feedback);
      return;
    }
    const option = event.target.closest("[data-option]");
    if (option) {
      submit(option.dataset.option);
      return;
    }
    const pick = event.target.closest("[data-turn]");
    if (pick) {
      state.chat.selected = Number(pick.dataset.turn);
      paintChat();
    }
  };
  on(inner, "click", selectTurn);
  on(inner, "keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    if (!event.target.closest("[data-turn]")) return;
    event.preventDefault();
    selectTurn(event);
  });
  input.focus();
}

async function submit(question) {
  const text = (question || "").trim();
  if (!text || state.busy) return;
  state.busy = true;
  const send = document.getElementById("chat-send");
  send.disabled = true;
  send.innerHTML = `<span class="spinner" aria-hidden="true"></span>运行中`;
  const input = document.getElementById("chat-input");
  if (input) {
    input.value = "";
    input.style.height = "auto";
  }
  // The question goes up before the first byte of the answer: the user should
  // never stare at a spinner that hides what they just asked.
  state.chat.pending = { question: text, steps: [], elapsedMs: 0 };
  state.chat.error = "";
  paintChat();
  let payload = null;
  try {
    const id = await ensureSession();
    const onStep = (step) => {
      if (!state.chat.pending) return;
      state.chat.pending.steps.push(step);
      state.chat.pending.elapsedMs += Number(step.elapsed_ms || 0);
      paintChat();
    };
    try {
      payload = await streamAsk(id, text, onStep);
    } catch (error) {
      if (!error.fallbackToPlain) throw error;
      payload = await streamFallback(id, text, onStep);
    }
    state.chat.messages.push(payload.result);
    state.chat.slots = payload.conversation.slots;
    state.chat.selected = state.chat.messages.length - 1;
    state.chat.error = "";
  } catch (error) {
    state.chat.error = error.message;
  } finally {
    state.busy = false;
    state.chat.pending = null;
  }
  paintChat();
  const button = document.getElementById("chat-send");
  if (button) {
    button.disabled = false;
    button.textContent = "发送";
  }
  const scroll = document.getElementById("chat-scroll");
  if (scroll) scroll.scrollTop = scroll.scrollHeight;
  await refreshHealthSessions();
}

async function submitFeedback(turnIndex, reason) {
  try {
    const id = await ensureSession();
    const payload = await api.submitFeedback(id, { turn_index: turnIndex, reason });
    toast(payload.created ? "已提交反馈，进入审核队列" : "该轮反馈已在审核队列中");
  } catch (error) {
    toast(`反馈失败：${error.message}`);
  }
}

async function resetChat() {
  if (!state.sessionId) return;
  try {
    await api.resetSession(state.sessionId);
    state.chat = { messages: [], slots: null, selected: -1, error: "", pending: null };
    paintChat();
    toast("会话已清空");
    await refreshHealthSessions();
  } catch (error) {
    toast(`重置失败：${error.message}`);
  }
}

function paintChat() {
  const inner = document.getElementById("chat-inner");
  const detail = document.getElementById("chat-detail");
  if (!inner) return;
  const messages = state.chat.messages;
  const parts = [];
  if (!messages.length && !state.chat.error) {
    const health = state.health || {};
    const stats = [
      health.single_turn_cases ? `<span><b>${h(health.single_turn_cases)}</b> 条用例</span>` : "",
      health.dialect ? `<span><b>${h(health.dialect)}</b> 方言</span>` : "",
      health.prompt_version ? `<span><b>${h(health.prompt_version)}</b> 提示词</span>` : "",
    ].filter(Boolean).join("");
    parts.push(`<div class="hero">
      <div class="hero__orb" aria-hidden="true"><i></i><i></i><i></i><svg class="hero__logo" viewBox="0 0 40 40"><use href="#logo-glyph"/></svg></div>
      <h1 class="hero__title">问一句，<span>看见数据。</span></h1>
      ${stats ? `<div class="hero__stats">${stats}</div>` : ""}
    </div>`);
  }
  if (state.chat.error) {
    parts.push(`<div class="notice notice--bad">请求失败：${h(state.chat.error)}</div>`);
  }
  messages.forEach((message, index) => parts.push(messageMarkup(message, index)));
  if (state.chat.pending) parts.push(pendingMarkup(state.chat.pending));
  inner.innerHTML = parts.join("");

  if (!detail) return;
  const picked = messages[state.chat.selected] || messages[messages.length - 1];
  if (!picked) {
    detail.innerHTML = `<div class="detail__h">验证详情</div><div class="detail__empty" aria-hidden="true"><i></i><i></i><i></i></div>`;
    return;
  }
  detail.innerHTML = `
    <div class="detail__h">验证详情 · 第 ${state.chat.selected + 1} 轮</div>
    <div class="grid" style="gap:8px">
      <div class="small muted">原始问题</div>
      <div>${h(picked.question)}</div>
      <div class="small muted">改写后的独立问题</div>
      <div class="strong">${h(picked.effective_question)}</div>
      <div class="pill-row">
        ${statusChip(picked.status)}
        ${chip(picked.rewrite_source === "none" ? "未改写" : `改写 ${rewriteLabel(picked.rewrite_source)}`, picked.rewrite_source === "none" ? "" : "info")}
        ${(picked.inherited_slots || []).length ? chip(`继承 ${picked.inherited_slots.join(", ")}`, "accent") : ""}
        ${chip(`置信度 ${num(picked.confidence, 2)}`, "")}
        ${chip(`${picked.steps} 步`, "")}
        ${chip(`${num(picked.elapsed_ms)} ms`, "")}
        ${picked.tokens ? chip(`${picked.tokens} tokens`, "") : ""}
      </div>
    </div>
    <div><div class="small muted" style="margin-bottom:6px">本轮槽位</div>${slotsChips(picked.slots)}</div>
    ${(picked.assumptions || []).length
    ? `<div><div class="small muted" style="margin-bottom:6px">口径假设</div><ul style="margin:0;padding-left:18px">${picked.assumptions.map((item) => `<li class="small">${h(item)}</li>`).join("")}</ul></div>`
    : ""}
    ${picked.error ? `<div class="notice notice--bad">${h(picked.error)}</div>` : ""}
    ${picked.clarification ? clarificationMarkup(picked.clarification, false) : ""}
    ${(picked.alternatives || []).length
    ? `<div><div class="small muted" style="margin-bottom:6px">更窄的问法</div><ul style="margin:0;padding-left:18px">${picked.alternatives.map((item) => `<li class="small">${h(item)}</li>`).join("")}</ul></div>`
    : ""}
    ${sqlBlock(picked.sql)}
    ${picked.columns && picked.columns.length
      ? `<div><div class="small muted" style="margin-bottom:6px">结果集 · ${picked.row_count} 行</div>${resultTable(picked.columns, picked.rows)}</div>`
      : ""}
    <div><div class="small muted" style="margin-bottom:6px">工具调用轨迹</div>${timelineMarkup(picked.tool_trace)}</div>`;
}

function pendingMarkup(pending) {
  const steps = pending.steps || [];
  const last = steps.length ? steps[steps.length - 1] : null;
  const phase = last
    ? `${TOOL_ZH[last.tool] || last.tool}${last.summary ? ` · ${last.summary}` : ""}`
    : "已提交问题，等待模型选择工具";
  return `
  <div class="msg msg--user"><div class="bubble">${h(pending.question)}</div></div>
  <div class="msg msg--bot">
    <div class="bubble bubble--pending" id="chat-pending" aria-live="polite">
      <span class="spinner" aria-hidden="true"></span><span class="thinking">${h(phase)}</span>
    </div>
    <div class="msg__meta">
      ${chip(`已执行 ${steps.length} 步`, "")}
      <span class="mono small muted">${num(pending.elapsedMs)} ms</span>
    </div>
    ${steps.length
      ? `<div class="pill-row">${steps
        .map((step) => `<span class="chip${step.ok ? "" : " chip--bad"}">${h(TOOL_ZH[step.tool] || step.tool)}</span>`)
        .join("")}</div>`
      : ""}
  </div>`;
}

function messageMarkup(message, index) {
  const selected = index === state.chat.selected;
  const clarification = message.clarification;
  const options = (clarification && clarification.options) || [];
  const answer = options.length
    ? `${clarification.reason ? `${clarification.reason}\n` : ""}请明确要按哪个口径继续：\n${options
      .map((option, position) => `${position + 1}. ${option}`)
      .join("\n")}`
    : message.answer || message.error || "(无回答文本)";
  const rewrote = message.effective_question && message.effective_question !== message.question;
  return `
  <div class="msg msg--user"><div class="bubble">${h(message.question)}</div></div>
  <div class="msg msg--bot">
    <div class="bubble${selected ? " bubble--selected" : ""}" data-turn="${index}" role="button" tabindex="0" aria-label="查看第 ${index + 1} 轮验证详情">${h(answer)}</div>
    <div class="msg__meta">
      ${statusChip(message.status)}
      ${message.rewrite_source === "none" ? "" : chip(`改写 ${rewriteLabel(message.rewrite_source)}`, "info")}
      ${rewrote ? chip(`→ ${message.effective_question}`, "accent") : ""}
      <span class="mono small muted">第 ${index + 1} 轮 · ${num(message.elapsed_ms)} ms · ${message.steps} 步${message.trace_id ? ` · 追踪 ${message.trace_id}` : ""}</span>
    </div>
    ${options.length
      ? `<div class="pill-row">${options
        .map((option) => `<button class="chip btn-chip" type="button" data-option="${h(option)}">${h(option)}</button>`)
        .join("")}</div>`
      : ""}
    ${(message.alternatives || []).length
      ? `<div class="pill-row"><span class="small muted">更窄的问法</span>${message.alternatives
        .map((option) => `<button class="chip btn-chip" type="button" data-option="${h(option)}">${h(option)}</button>`)
        .join("")}</div>`
      : ""}
    ${message.sql
      ? `<details class="msg__panel">
          <summary>SQL 与结果集${message.row_count ? ` · ${message.row_count} 行` : ""}</summary>
          <div class="msg__panel__body">
            <pre class="sql">${highlightSql(message.sql)}</pre>
            ${message.columns && message.columns.length ? resultTable(message.columns, message.rows || []) : ""}
          </div>
        </details>`
      : ""}
    ${message.status === "ok" && message.sql
      ? `<div class="pill-row msg__feedback">
          ${[["result_wrong", "结果不对"], ["metric_wrong", "口径不对"], ["field_wrong", "字段不对"]]
            .map(([reason, label]) => `<button class="chip btn-chip" type="button" data-feedback="${reason}" data-turn="${index}">${label}</button>`)
            .join("")}
        </div>`
      : ""}
  </div>`;
}

function timelineMarkup(trace) {
  if (!trace || !trace.length) return `<div class="tl-empty">—</div>`;
  return `<div class="timeline">${trace
    .map((step, index) => `<div class="tl">
        <div class="tl__gut"><i class="tl__dot${step.ok ? "" : " tl__dot--bad"}"></i>${index === trace.length - 1 ? "" : '<i class="tl__line"></i>'}</div>
        <div class="tl__body">
          <div class="tl__name">${h(TOOL_ZH[step.tool] || step.tool || "?")} <span class="muted small">#${h(step.step)}</span></div>
          ${step.arguments ? `<div class="tl__args">${h(JSON.stringify(step.arguments).slice(0, 260))}</div>` : ""}
          ${step.summary ? `<div class="tl__obs${step.ok ? "" : " bad"}">${h(String(step.summary).slice(0, 360))}</div>` : ""}
        </div>
      </div>`).join("")}</div>`;
}

function clarificationMarkup(clarification, withOptions) {
  const missing = clarification.missing_slots || [];
  return `<div class="notice">
    <div class="small strong">需要澄清${missing.length ? `（缺 ${missing.map((s) => SLOT_LABELS[s] || s).join("、")}）` : ""}</div>
    <div class="small muted">${h(clarification.reason || "")}</div>
    ${withOptions && (clarification.options || []).length
      ? `<div class="pill-row" style="margin-top:8px">${clarification.options
        .map((option) => `<button class="chip btn-chip" type="button" data-option="${h(option)}">${h(option)}</button>`)
        .join("")}</div>`
      : ""}
  </div>`;
}

/* ---------- eval ---------- */

async function renderEval(view) {
  view.innerHTML = `
  <div class="wrap">
    <div class="card">
      <div class="card__head"><span class="card__title">运行评测</span></div>
      <div class="card__body">
        <div class="grid grid--4" style="align-items:end">
          <label class="switch"><input type="checkbox" id="eval-multi" ${state.eval.multiTurn ? "checked" : ""}>包含 10 条多轮会话用例</label>
          <label class="switch"><input type="checkbox" id="eval-judge" ${state.eval.judge ? "checked" : ""}>调用智能评审</label>
          <div><div class="small muted" style="margin-bottom:4px">Judge 抽样数</div><input class="field" id="eval-sample" type="number" min="1" max="50" placeholder="如 10" style="width:110px"></div>
          <div style="display:flex;gap:8px"><button class="btn btn--primary" id="eval-run" type="button">开始跑分</button><button class="btn" id="eval-cases" type="button">用例清单</button></div>
        </div>
        <div id="eval-status" style="margin-top:12px"></div>
      </div>
    </div>
    <div id="eval-body"></div>
  </div>`;

  on(el(view, "#eval-run"), "click", runEval);
  on(el(view, "#eval-multi"), "change", (event) => (state.eval.multiTurn = event.target.checked));
  on(el(view, "#eval-judge"), "change", (event) => (state.eval.judge = event.target.checked));
  on(el(view, "#eval-cases"), "click", showCaseInventory);
  paintEval();
}

async function showCaseInventory() {
  const body = document.getElementById("eval-body");
  try {
    const cases = await api.evalCases();
    const row = (item) => `<tr>
      <td class="mono small">${h(item.id)}</td>
      <td class="case__q">${item.turns && item.turns.length
      ? item.turns.map((turn, index) => `<div>${h(turn)}</div>`).join("")
      : h(item.question)}</div>
      <td class="small">${h(KIND_LABELS[item.kind] || item.kind)}</td>
      <td class="small">${h(categoryLabel(item.category))}</td>
      <td class="small">${h(DIFFICULTY_LABELS[item.difficulty] || item.difficulty || "—")}</td>
      <td class="small">${item.expect_refusal ? chip("期望拒答", "info") : ""}${item.expect_clarification ? chip("期望澄清", "warn") : ""}</td>
    </tr>`;
    body.innerHTML = `<div class="card">
      <div class="card__head"><span class="card__title">冻结用例清单</span><span class="card__hint">${cases.single.length} + ${cases.multi.length}</span></div>
      <div class="card__body card__body--tight"><div class="table-wrap" style="border:0;max-height:520px">
        <table class="data"><thead><tr><th>编号</th><th>问题</th><th>类型</th><th>类别</th><th>难度</th><th>期望</th></tr></thead>
        <tbody>${cases.single.map(row).join("")}${cases.multi.map(row).join("")}</tbody></table>
      </div></div></div>`;
  } catch (error) {
    body.innerHTML = `<div class="notice notice--bad">用例清单加载失败：${h(error.message)}</div>`;
  }
}

async function runEval() {
  const status = document.getElementById("eval-status");
  const run = document.getElementById("eval-run");
  state.eval.running = true;
  state.eval.error = "";
  run.disabled = true;
  run.innerHTML = `<span class="spinner" aria-hidden="true"></span>跑分中`;
  status.innerHTML = `<div class="notice">正在执行冻结集…</div>`;
  try {
    const sample = Number(document.getElementById("eval-sample").value);
    const payload = await api.runEval({
      multi_turn: state.eval.multiTurn,
      judge: state.eval.judge,
      judge_sample: Number.isFinite(sample) && sample > 0 ? sample : null,
    });
    state.eval.report = payload.report;
    state.eval.reportDir = payload.report_dir;
    state.eval.reports = [];
    state.eval.page = 1;
    paintEval();
    status.innerHTML = `<div class="notice notice--ok">完成：${payload.report.passed} / ${payload.report.total} 通过</div>`;
  } catch (error) {
    state.eval.error = error.message;
    status.innerHTML = `<div class="notice notice--bad">${h(error.message)}</div>`;
  } finally {
    state.eval.running = false;
    run.disabled = false;
    run.textContent = "开始跑分";
  }
}

function paintEval() {
  const body = document.getElementById("eval-body");
  if (!body) return;
  const report = state.eval.report;
  if (!report) {
    body.innerHTML = `<div class="card"><div class="card__head"><span class="card__title">结果</span></div>${empty("暂无结果")}</div>
      <div class="card" id="eval-reports"></div>`;
    loadReports(body);
    return;
  }
  const m = report.metrics || {};
  const cards = Object.keys(METRIC_LABELS)
    .filter((key) => key in m)
    .map((key) => {
      const [label, kind] = METRIC_LABELS[key];
      const tone = kind === "pct" && m[key] >= 1 ? " ok" : kind === "pct" && m[key] < 0.8 ? " bad" : "";
      return `<div class="card kpi${tone}">
        <div class="kpi__k">${h(label)}</div>
        <div class="kpi__v">${h(metricFormat(key, m[key], kind))}</div>
      </div>`;
    }).join("");

  const accuracy = report.total ? report.passed / report.total : 0;
  const meta = report.metadata || {};
  const head = `<div class="card"><div class="card__body" style="display:flex;gap:var(--sp-5);align-items:center;flex-wrap:wrap">
    <div>
      <div class="kpi__k">通过</div>
      <div class="kpi__v">${report.passed} <span class="muted" style="font-size:var(--fs-lg)">/ ${report.total}</span></div>
      <div class="bar${accuracy >= 1 ? " bar--ok" : ""}" style="width:220px;margin-top:8px"><i style="width:${(accuracy * 100).toFixed(1)}%"></i></div>
    </div>
    <div class="grid" style="gap:4px;min-width:260px">
      <div class="small muted">模型</div><div class="mono">${h(meta.provider || report.provider)} · ${h(meta.model || "RuleBasedLLM")}</div>
      <div class="small muted">版本</div><div class="mono">${h(meta.prompt_version || "")} · ${h(meta.git_sha || "local")}</div>
      <div class="small muted">时间</div><div class="mono">${h(runAt(meta.run_at || report.generated_at))}</div>
    </div>
    <div class="pill-row" style="margin-left:auto">
      ${chip(`可回答 ${report.answerable}`, "")}
      ${chip(`拒答 ${report.refusals}`, "info")}
      ${chip(`澄清 ${report.clarifications}`, "warn")}
      ${chip(`跳过 ${report.skipped}`, "")}
      ${chip(`token ${report.total_tokens}`, "")}
    </div>
  </div></div>`;

  const byKind = Object.entries(report.by_kind || {}).map(([kind, item]) => `<tr>
      <td>${kind === "single" ? "单轮" : "多轮会话"}</td>
      <td class="num">${item.cases}</td><td class="num">${item.answerable}</td><td class="num">${item.passed}</td>
      <td class="num">${pct(item.accuracy)}</td><td class="num">${pct(item.execution_accuracy)}</td>
      <td class="num">${num(item.avg_turns)}</td><td class="num">${num(item.avg_steps)}</td>
      <td class="num">${num(item.avg_latency_ms)}</td>
    </tr>`).join("");

  const byCategory = Object.entries(report.by_category || {}).map(([category, item]) => `<tr>
      <td class="mono">${h(categoryLabel(category))}</td>
      <td class="num">${item.cases}</td><td class="num">${item.passed}</td>
      <td class="num">${pct(item.accuracy)}</td>
      <td style="width:120px"><div class="bar${item.accuracy >= 1 ? " bar--ok" : item.accuracy < 0.8 ? " bar--bad" : ""}"><i style="width:${(item.accuracy * 100).toFixed(1)}%"></i></div></td>
      <td class="num">${num(item.avg_latency_ms)}</td>
    </tr>`).join("");

  body.innerHTML = `
    <div class="grid grid--4">${cards}</div>
    ${head}
    <div class="grid grid--2">
      <div class="card"><div class="card__head"><span class="card__title">按会话深度</span></div>
        <div class="card__body card__body--tight"><div class="table-wrap" style="border:0"><table class="data">
          <thead><tr><th>类型</th><th class="num">用例</th><th class="num">可答</th><th class="num">通过</th><th class="num">准确率</th><th class="num">执行准确</th><th class="num">平均轮</th><th class="num">平均步</th><th class="num">耗时ms</th></tr></thead>
          <tbody>${byKind}</tbody></table></div></div></div>
      <div class="card"><div class="card__head"><span class="card__title">按类别</span></div>
        <div class="card__body card__body--tight"><div class="table-wrap" style="border:0"><table class="data">
          <thead><tr><th>类别</th><th class="num">用例</th><th class="num">通过</th><th class="num">准确率</th><th></th><th class="num">耗时ms</th></tr></thead>
          <tbody>${byCategory}</tbody></table></div></div></div>
    </div>
    ${judgeBlock(report.judge || {})}
    <div class="card">
      <div class="card__head">
        <span class="card__title">逐条用例</span>
        <div class="tabs" style="margin-left:12px;border:0">
          ${[["all", "全部"], ["failed", "仅失败"], ["passed", "仅通过"]]
        .map(([key, label]) => `<button data-filter="${key}" aria-selected="${state.eval.filter === key}" type="button">${label}</button>`)
        .join("")}
        </div>
      </div>
      <div class="card__body card__body--tight" id="eval-cases-body">${caseTable(report)}</div>
    </div>
    <div class="card" id="eval-reports"></div>`;

  on(body, "click", (event) => {
    const tab = event.target.closest("[data-filter]");
    if (!tab) return;
    state.eval.filter = tab.dataset.filter;
    state.eval.page = 1;
    body.querySelectorAll("[data-filter]").forEach((node) =>
      node.setAttribute("aria-selected", String(node.dataset.filter === state.eval.filter)));
    document.getElementById("eval-cases-body").innerHTML = caseTable(report);
  });
  bindPager(el(body, "#eval-cases-body"), ({ page, pageSize }) => {
    if (pageSize) {
      state.eval.pageSize = pageSize;
      state.eval.page = 1;
    } else {
      state.eval.page = page;
    }
    document.getElementById("eval-cases-body").innerHTML = caseTable(report);
  });
  on(el(body, "#eval-cases-body"), "click", (event) => {
    const row = event.target.closest("[data-case]");
    if (!row) return;
    const detail = document.getElementById(`case-${row.dataset.case}`);
    if (detail) detail.hidden = !detail.hidden;
  });
  loadReports(body);
}

function caseTable(report) {
  const filtered = (report.cases || [])
    .map((item, index) => ({ item, index }))
    .filter(({ item }) => state.eval.filter === "all" ? true : state.eval.filter === "passed" ? item.passed : !item.passed);
  if (!filtered.length) return empty("—");
  const pages = Math.max(1, Math.ceil(filtered.length / state.eval.pageSize));
  state.eval.page = Math.min(Math.max(1, state.eval.page), pages);
  const start = (state.eval.page - 1) * state.eval.pageSize;
  const rows = filtered.slice(start, start + state.eval.pageSize).map(({ item, index }) => `
    <tr class="case__row" data-case="${index}" style="cursor:pointer">
      <td>${item.passed ? '<span class="pass">通过</span>' : '<span class="bad">失败</span>'}</td>
      <td class="mono small">${h(item.id)}</td>
      <td class="case__q">${h(item.question)}</td>
      <td class="small">${h(categoryLabel(item.category))}</td>
      <td class="small">${h(KIND_LABELS[item.kind] || item.kind)}</td>
      <td class="small">${h(STATUS_LABELS[item.status] || item.status)}</td>
      <td class="num small">${item.row_count ?? "—"}/${item.gold_rows ?? "—"}</td>
      <td class="num small">${num(item.elapsed_ms)}</td>
      <td class="small">${item.error ? chip(item.error.slice(0, 28), "bad") : ""}</td>
    </tr>
    <tr class="case__detail" id="case-${index}" hidden>
      <td colspan="9">
        ${item.effective_question && item.effective_question !== item.question
        ? `<div class="small"><span class="muted">改写后：</span>${h(item.effective_question)}（${h(rewriteLabel(item.rewrite_source))}）</div>` : ""}
        ${item.expect_clarification ? `<div class="small muted">期望澄清，实际给出 ${item.clarification_options} 个选项</div>` : ""}
        ${sqlBlock(item.sql || "", "预测 SQL")}
        ${item.expected_tables && item.expected_tables.length
        ? `<div class="pill-row"><span class="small muted">期望表</span>${item.expected_tables.map((t) => chip(t, "accent")).join("")}${item.table_recall_at_5 === null || item.table_recall_at_5 === undefined ? "" : chip(`recall@5 ${pct(item.table_recall_at_5)}`, item.table_recall_at_5 >= 1 ? "ok" : "warn")}</div>` : ""}
        ${item.error ? `<div class="small bad">${h(item.error)}</div>` : ""}
        <div class="small muted">${item.steps} 步 · ${num(item.elapsed_ms)} ms · ${item.tokens} token</div>
      </td>
    </tr>`).join("");
  return `<div class="table-wrap" style="border:0;max-height:520px"><table class="data">
    <thead><tr><th>结果</th><th>编号</th><th>问题</th><th>类别</th><th>类型</th><th>状态</th><th class="num">行</th><th class="num">耗时ms</th><th>备注</th></tr></thead>
    <tbody>${rows}</tbody></table></div>${pagerMarkup("eval", state.eval.page, state.eval.pageSize, filtered.length)}`;
}

function judgeBlock(judge) {
  if (!judge.enabled) {
    const status = JUDGE_STATUS_LABELS[judge.status] || judge.status || "未运行";
    return `<div class="card"><div class="card__head"><span class="card__title">智能评审</span><span class="card__hint">${chip(status, "")}</span></div>
      <div class="card__body"><div class="pill-row">${chip("未启用", "")}${judge.prompt_version ? chip(`提示词 ${judge.prompt_version}`, "") : ""}</div></div></div>`;
  }
  const metrics = Object.entries(judge.metrics || {})
    .map(([key, value]) => chip(`${key} ${pct(value)}`, ""))
    .join("");
  const status = JUDGE_STATUS_LABELS[judge.status] || judge.status || "";
  return `<div class="card"><div class="card__head"><span class="card__title">智能评审</span><span class="card__hint">${chip(`${judge.model || "—"} · ${status}`, "")}</span></div>
    <div class="card__body">
      <div class="pill-row">${chip(`判定 ${judge.judged_cases}/${judge.sample_size}`, "info")}${chip("缓存命中 " + (judge.cache_hits || 0), "")}${chip("未知 " + (judge.unknown_cases || 0), judge.unknown_cases ? "warn" : "")}${metrics}</div>
    </div></div>`;
}

async function loadReports(body) {
  const host = document.getElementById("eval-reports") || body.querySelector("#eval-reports");
  if (!host) return;
  try {
    const reports = await api.reports();
    host.innerHTML = `<div class="card__head"><span class="card__title">历史报告</span><span class="card__hint" title="${h(reports.dir)}">${reports.items.length}</span></div>
      <div class="card__body${reports.items.length ? " card__body--tight" : ""}">${reports.items.length
        ? `<div class="table-wrap" style="border:0"><table class="data"><thead><tr><th>文件</th><th class="num">大小</th><th>时间</th><th></th></tr></thead><tbody>${reports.items
          .map((item) => `<tr><td class="mono small">${h(item.name)}</td><td class="num small">${(item.size / 1024).toFixed(1)} KB</td><td class="small muted">${h(new Date(item.modified_at * 1000).toLocaleString())}</td><td><button class="btn btn--sm" data-report="${h(item.name)}" type="button">载入</button></td></tr>`)
          .join("")}</tbody></table></div>`
        : empty("—")}</div>`;
    on(host, "click", async (event) => {
      const button = event.target.closest("[data-report]");
      if (!button) return;
      try {
        const report = await api.report(button.dataset.report);
        state.eval.report = report;
        state.eval.page = 1;
        paintEval();
        toast(`已载入 ${button.dataset.report}`);
      } catch (error) {
        toast(`载入失败：${error.message}`);
      }
    });
  } catch (error) {
    host.innerHTML = `<div class="card__body"><div class="small muted">历史报告不可用：${h(error.message)}</div></div>`;
  }
}

/* ---------- schema ---------- */

function selectSchemaTable(name) {
  state.schema.selected = name;
  const index = (state.schema.data?.tables || []).findIndex((table) => table.name === name);
  if (index >= 0) state.schema.page = Math.floor(index / state.schema.pageSize) + 1;
  paintSchema();
}

async function renderSchema(view) {
  view.innerHTML = `
  <div class="wrap">
    <div class="card">
      <div class="card__head"><span class="card__title">表结构检索</span></div>
      <div class="card__body">
        <div style="display:flex;gap:8px;align-items:center">
          <input class="field" id="schema-q" placeholder="搜索业务问法" style="flex:1;max-width:460px" aria-label="Schema 检索">
          <button class="btn btn--primary" id="schema-go" type="button">检索</button>
          <span class="small muted">召回</span>
          <input class="field num" id="schema-k" type="number" min="1" max="10" value="5" style="width:72px" aria-label="召回数量" title="召回数量">
        </div>
        <div id="schema-hits" style="margin-top:12px"></div>
      </div>
    </div>
    <div class="split">
      <div class="card"><div class="card__head"><span class="card__title">表</span><span class="card__hint" id="schema-count"></span></div>
        <div class="list" id="schema-list"></div><div id="schema-list-pager"></div></div>
      <div class="card" id="schema-detail">${skeleton(6)}</div>
    </div>
    <div class="grid grid--2">
      <div class="card"><div class="card__head"><span class="card__title">表关系</span><span class="card__hint" id="schema-rel-count"></span></div><div class="card__body card__body--tight" id="schema-rel"></div></div>
      <div class="card"><div class="card__head"><span class="card__title">业务口径</span><span class="card__hint" id="schema-rule-count"></span></div><div class="card__body" id="schema-rules"></div></div>
    </div>
  </div>`;

  on(el(view, "#schema-go"), "click", () => runSchemaSearch(el(view, "#schema-q").value));
  on(el(view, "#schema-q"), "keydown", (event) => {
    if (event.key === "Enter") runSchemaSearch(event.target.value);
  });
  on(el(view, "#schema-list"), "click", (event) => {
    const item = event.target.closest("[data-table]");
    if (!item) return;
    selectSchemaTable(item.dataset.table);
  });
  bindPager(el(view, "#schema-list-pager"), ({ page, pageSize }) => {
    if (pageSize) {
      state.schema.pageSize = pageSize;
      state.schema.page = 1;
    } else {
      state.schema.page = page;
    }
    paintSchema();
  });
  bindPager(el(view, "#schema-rel"), ({ page, pageSize }) => {
    if (pageSize) {
      state.schema.relPageSize = pageSize;
      state.schema.relPage = 1;
    } else {
      state.schema.relPage = page;
    }
    paintSchema();
  });

  if (!state.schema.data) state.schema.data = await api.schema();
  if (!state.schema.selected) {
    const first = (state.schema.data.tables || []).find((table) => !table.disabled);
    state.schema.selected = first ? first.name : null;
  }
  paintSchema();
}

async function runSchemaSearch(query) {
  const text = (query || "").trim();
  const host = document.getElementById("schema-hits");
  if (!text) {
    host.innerHTML = "";
    return;
  }
  host.innerHTML = skeleton(2);
  try {
    const topK = Number(document.getElementById("schema-k").value) || 5;
    const result = await api.searchSchema(text, Math.min(10, Math.max(1, topK)));
    state.schema.query = text;
    state.schema.hits = result.tables;
    const meta = `检索 ${result.mode}${result.embedding && result.embedding !== "none" ? " · " + result.embedding : ""}${result.degraded ? " · 已降级 BM25" : ""}${result.reranked ? " · 已重排" : ""}`;
    host.innerHTML = result.tables.length
      ? `<div class="small muted" style="margin-bottom:6px">${h(meta)}</div><div class="table-wrap"><table class="data"><thead><tr><th class="num">序号</th><th>表</th><th class="num">分数</th><th>来源</th><th>说明</th><th></th></tr></thead><tbody>${result.tables
        .map((hit, index) => `<tr><td class="num">${index + 1}</td><td class="mono">${h(hit.name)}</td><td class="num">${num(hit.score, 4)}</td><td>${(hit.sources || []).map((source) => chip(source === "vector" ? "向量" : "BM25", source === "vector" ? "accent" : "")).join("")}</td><td class="small muted">${h(hit.description || "—")}</td><td><button class="btn btn--sm" data-table="${h(hit.name)}" type="button">查看</button></td></tr>`)
        .join("")}</tbody></table></div>`
      : `<div class="small muted">未命中</div>`;
    on(host, "click", (event) => {
      const button = event.target.closest("[data-table]");
      if (!button) return;
      selectSchemaTable(button.dataset.table);
      document.getElementById("schema-detail").scrollIntoView({ block: "nearest" });
    });
  } catch (error) {
    host.innerHTML = `<div class="notice notice--bad">检索失败：${h(error.message)}</div>`;
  }
}

function paintSchema() {
  const data = state.schema.data;
  if (!data) return;
  const list = document.getElementById("schema-list");
  const detail = document.getElementById("schema-detail");
  const tables = data.tables || [];
  const listPages = Math.max(1, Math.ceil(tables.length / state.schema.pageSize));
  state.schema.page = Math.min(Math.max(1, state.schema.page), listPages);
  const listStart = (state.schema.page - 1) * state.schema.pageSize;
  const pageTables = tables.slice(listStart, listStart + state.schema.pageSize);
  document.getElementById("schema-count").textContent = `${tables.filter((t) => !t.disabled).length} 可用 / ${tables.length} 声明 · ${data.schema_hash}`;
  list.innerHTML = pageTables
    .map((table) => `<button class="list__item${table.disabled ? " list__item--off" : ""}" data-table="${h(table.name)}" aria-current="${state.schema.selected === table.name}" type="button">
      <span class="list__n">${h(table.name)}</span><span class="list__d truncate" style="max-width:220px">${h(table.description || "—")}</span></button>`)
    .join("");
  const listPager = document.getElementById("schema-list-pager");
  if (listPager) listPager.innerHTML = pagerMarkup("schema", state.schema.page, state.schema.pageSize, tables.length);

  const table = data.tables.find((item) => item.name === state.schema.selected);
  if (!table) {
    detail.innerHTML = empty("—");
    return;
  }
  const relations = (data.relationships || []).map(relationParts).filter(
    (rel) => rel.fromTable === table.name || rel.toTable === table.name);
  detail.innerHTML = `
    <div class="card__head">
      <span class="card__title mono">${h(table.name)}</span>
      ${table.disabled ? chip("已禁用", "bad") : chip("可用", "ok")}
      ${(table.primary_key || []).length ? chip("主键 " + table.primary_key.join(","), "") : ""}
      ${(table.columns || []).length ? chip(`注释 ${table.columns.filter((column) => column.description).length}/${table.columns.length}`, "") : ""}
    </div>
    <div class="card__body">
      <div>${h(table.description || "—")}</div>
      ${(table.synonyms || []).length ? `<div class="pill-row" style="margin-top:8px">${table.synonyms.map((s) => chip(s, "accent")).join("")}</div>` : ""}
      ${(table.required_filters || []).length ? `<div class="notice" style="margin-top:12px"><div class="small strong">必填过滤（护栏强制）</div>${table.required_filters.map((filter) => `<div class="mono small">${h(JSON.stringify(filter))}</div>`).join("")}</div>` : ""}
    </div>
    <div class="card__body card__body--tight">
      <div class="table-wrap" style="border:0"><table class="data">
        <thead><tr><th>列</th><th>类型</th><th>说明</th><th>同义词</th><th>标记</th></tr></thead>
        <tbody>${(table.columns || []).map((column) => `<tr>
          <td class="mono small">${h(column.name)}</td>
          <td class="mono small muted">${h(column.type)}</td>
          <td class="small">${h(column.description || "—")}</td>
          <td class="small muted">${h((column.synonyms || []).join(" / ") || "—")}</td>
          <td>${column.sensitive ? chip("敏感", "bad") : ""}${column.disabled ? chip("禁用", "warn") : ""}${(table.primary_key || []).includes(column.name) ? chip("主键", "info") : ""}</td>
        </tr>`).join("")}</tbody>
      </table></div>
    </div>
    ${relations.length ? `<div class="card__body"><div class="small strong" style="margin-bottom:6px">关联</div>${relations
      .map((rel) => `<div class="mono small">${h(`${rel.fromTable}.${rel.fromColumn} → ${rel.toTable}.${rel.toColumn} (${rel.kind || ""})`)}</div>`)
      .join("")}</div>` : ""}`;

  const relHost = document.getElementById("schema-rel");
  if (relHost) {
    const allRelations = (data.relationships || []).map(relationParts);
    const relPages = Math.max(1, Math.ceil(allRelations.length / state.schema.relPageSize));
    state.schema.relPage = Math.min(Math.max(1, state.schema.relPage), relPages);
    const relStart = (state.schema.relPage - 1) * state.schema.relPageSize;
    const shownRelations = allRelations.slice(relStart, relStart + state.schema.relPageSize);
    const relCount = document.getElementById("schema-rel-count");
    if (relCount) relCount.textContent = `${allRelations.length}`;
    relHost.innerHTML = allRelations.length
      ? `<div class="table-wrap" style="border:0;max-height:360px"><table class="data"><thead><tr><th>起点</th><th>终点</th><th>类型</th></tr></thead><tbody>${shownRelations
        .map((rel) => `<tr><td class="mono small">${h(rel.fromTable)}.${h(rel.fromColumn)}</td><td class="mono small">${h(rel.toTable)}.${h(rel.toColumn)}</td><td class="small">${h(rel.kind || "—")}</td></tr>`)
        .join("")}</tbody></table></div>${pagerMarkup("schema-rel", state.schema.relPage, state.schema.relPageSize, allRelations.length)}`
      : empty("无声明关系");
  }
  const rules = document.getElementById("schema-rules");
  const ruleCount = document.getElementById("schema-rule-count");
  if (ruleCount) ruleCount.textContent = `${(data.business_rules || []).length}`;
  if (rules) {
    rules.innerHTML = (data.business_rules || []).length
      ? `<ul style="margin:0;padding-left:18px;display:flex;flex-direction:column;gap:6px">${data.business_rules
        .map((rule) => `<li class="small">${h(rule)}</li>`).join("")}</ul>`
      : empty("未配置业务口径");
  }
}

/* ---------- memory ---------- */

async function renderMemory(view) {
  view.innerHTML = `
  <div class="wrap">
    <div class="card"><div class="card__body" style="display:flex;gap:var(--sp-4);align-items:center;flex-wrap:wrap">
      <div class="tabs" style="border:0">
        <button data-mtab="experience" aria-selected="${state.memory.tab === "experience"}" type="button">经验库</button>
        <button data-mtab="failures" aria-selected="${state.memory.tab === "failures"}" type="button">失败队列</button>
        <button data-mtab="feedback" aria-selected="${state.memory.tab === "feedback"}" type="button">反馈审核</button>
      </div>
      <div id="memory-filter" style="margin-left:auto;display:flex;gap:8px;align-items:center"></div>
    </div></div>
    <div id="memory-kpi" class="grid grid--4"></div>
    <div id="memory-body" class="card">${skeleton(4)}</div>
  </div>`;

  on(el(view, '[data-mtab="experience"]').parentElement, "click", (event) => {
    const tab = event.target.closest("[data-mtab]");
    if (!tab) return;
    state.memory.tab = tab.dataset.mtab;
    view.querySelectorAll("[data-mtab]").forEach((node) =>
      node.setAttribute("aria-selected", String(node.dataset.mtab === state.memory.tab)));
    paintMemory();
  });
  on(document.getElementById("memory-filter"), "click", (event) => {
    const status = event.target.closest("[data-mstatus]");
    if (status) {
      if (state.memory.tab === "experience") state.memory.expStatus = status.dataset.mstatus;
      else if (state.memory.tab === "failures") state.memory.failStatus = status.dataset.mstatus;
      else state.memory.fbStatus = status.dataset.mstatus;
      paintMemory();
      return;
    }
    if (event.target.closest("[data-mlimit]")) {
      state.memory.limit = 100;
      paintMemory();
    }
  });
  on(document.getElementById("memory-body"), "click", async (event) => {
    const button = event.target.closest("[data-cmd]");
    if (button) {
      copyText(button.dataset.cmd);
      return;
    }
    const action = event.target.closest("[data-fb-action]");
    if (!action) return;
    const body = document.getElementById("memory-body");
    const id = action.dataset.fbId;
    const kind = action.dataset.fbAction;
    const sqlInput = body.querySelector(`[data-fb-sql="${id}"]`);
    const noteInput = body.querySelector(`[data-fb-note="${id}"]`);
    const sql = sqlInput ? sqlInput.value.trim() : "";
    const note = noteInput ? noteInput.value.trim() : "";
    if (kind === "approve" && !sql) {
      toast("通过反馈需要填写修正 SQL");
      return;
    }
    if (kind === "reject" && !note) {
      toast("拒绝反馈需要填写原因");
      return;
    }
    action.disabled = true;
    try {
      await api.reviewFeedback(id, { action: kind, sql, note });
      toast(kind === "approve" ? "已通过并写入 verified 经验" : "已拒绝该反馈");
      await paintMemory();
    } catch (error) {
      toast(`审核失败：${error.message}`);
      action.disabled = false;
    }
  });
  await paintMemory();
}

function paintMemoryFilter() {
  const host = document.getElementById("memory-filter");
  const options = state.memory.tab === "experience"
    ? [["verified", "已验证"], ["candidate", "候选"], ["all", "全部"]]
    : state.memory.tab === "failures"
      ? [["open", "待处理"], ["resolved", "已结案"], ["all", "全部"]]
      : [["open", "待审核"], ["approved", "已通过"], ["rejected", "已拒绝"], ["all", "全部"]];
  const current = state.memory.tab === "experience"
    ? state.memory.expStatus
    : state.memory.tab === "failures" ? state.memory.failStatus : state.memory.fbStatus;
  host.innerHTML = `${options
    .map(([value, label]) => `<button class="chip btn-chip" type="button" data-mstatus="${value}"${current === value ? ' style="border-style:solid;border-color:var(--accent);color:var(--accent)"' : ""}>${h(label)}</button>`)
    .join("")}
    <button class="btn btn--sm" data-mlimit="100" type="button">加载更多</button>`;
}

async function paintMemory() {
  paintMemoryFilter();
  const body = document.getElementById("memory-body");
  const kpi = document.getElementById("memory-kpi");
  body.innerHTML = skeleton(4);
  try {
    const limit = state.memory.limit || 30;
    if (state.memory.tab === "experience") {
      const status = state.memory.expStatus === "all" ? null : state.memory.expStatus;
      const payload = await api.experience(status, limit);
      state.memory.exp = payload;
      const stats = payload.stats || {};
      kpi.innerHTML = [
        ["已验证经验", stats.verified ?? 0],
        ["候选经验", stats.candidates ?? 0],
        ["复用命中", stats.reuse_hits ?? 0],
        ["待处理失败", stats.failures_open ?? 0],
      ].map(([label, value]) => `<div class="card kpi"><div class="kpi__k">${h(label)}</div><div class="kpi__v">${h(value)}</div></div>`).join("");
      const hash = state.health ? state.health.schema_hash : "";
      body.innerHTML = `<div class="card__head"><span class="card__title">经验条目</span><span class="card__hint">${payload.items.length}</span></div>` +
        (payload.items.length
          ? payload.items.map((item) => `<div class="exp">
              <div class="exp__top">
                <span class="exp__q">${h(item.question)}</span>
                ${chip(EXPERIENCE_STATUS_LABELS[item.status] || item.status, item.status === "verified" ? "ok" : "warn")}
                ${item.category ? chip(categoryLabel(item.category), "accent") : ""}
                ${chip(SOURCE_LABELS[item.source] || item.source || "智能体", "")}
                ${chip(`命中 ${item.hit_count || 0}`, "")}
                ${hash && item.schema_hash && item.schema_hash !== hash ? chip("表结构已变更", "bad") : ""}
              </div>
              ${sqlBlock(item.sql)}
              <div class="small muted mono">编号 ${item.id} · ${h(item.created_at || "")}</div>
              <button class="btn btn--sm" data-cmd="${h(addCmd(item))}" type="button">复制命令</button>
            </div>`).join("")
          : empty("—"));
    } else if (state.memory.tab === "failures") {
      const status = state.memory.failStatus;
      const payload = await api.failures(status, limit);
      state.memory.fail = payload;
      const stats = payload.stats || {};
      const byCategory = Object.entries(stats.failures_by_category || {});
      kpi.innerHTML = [
        ["队列总数", stats.failures_total ?? 0],
        ["待处理", stats.failures_open ?? 0],
        ["已结案", stats.failures_resolved ?? 0],
        ["累计发生", stats.failure_occurrences ?? 0],
      ].map(([label, value]) => `<div class="card kpi"><div class="kpi__k">${h(label)}</div><div class="kpi__v">${h(value)}</div></div>`).join("");
      body.innerHTML = `<div class="card__head"><span class="card__title">失败队列</span><span class="card__hint">${byCategory.map(([key, value]) => `${h(key)} ${h(value)}`).join(" · ")}</span></div>` +
        (payload.items.length
          ? payload.items.map((item) => `<div class="exp">
              <div class="exp__top">
                <span class="exp__q">${h(item.question)}</span>
                ${chip(FAILURE_STATUS_LABELS[item.status] || item.status, item.status === "open" ? "bad" : "ok")}
                ${item.category ? chip(categoryLabel(item.category), "accent") : ""}
                ${chip(`发生 ${item.occur_count} 次`, item.occur_count > 1 ? "warn" : "")}
                ${chip(STATUS_LABELS[item.reason] || item.reason || "—", "")}
              </div>
              ${item.last_sql ? sqlBlock(item.last_sql, "最后一次 SQL") : ""}
              ${item.detail ? `<details class="msg__panel"><summary>Agent 终态细节</summary><div class="msg__panel__body"><pre class="sql">${h(item.detail)}</pre></div></details>` : ""}
              <div class="small muted mono">编号 ${item.id} · 首次 ${h(item.created_at || "")} · 最近 ${h(item.last_seen_at || "")}${item.resolution ? ` · 结案说明：${h(item.resolution)}` : ""}</div>
              <button class="btn btn--sm" data-cmd="${h(resolveCmd(item))}" type="button">复制结案命令</button>
            </div>`).join("")
          : empty("—"));
    } else {
      const status = state.memory.fbStatus;
      const payload = await api.feedback(status, limit);
      state.memory.fb = payload;
      const stats = payload.stats || {};
      const labels = payload.reasons || {};
      kpi.innerHTML = [
        ["待审核", stats.feedback_open ?? 0],
        ["已通过", stats.feedback_approved ?? 0],
        ["已拒绝", stats.feedback_rejected ?? 0],
        ["累计反馈", stats.feedback_occurrences ?? 0],
      ].map(([label, value]) => `<div class="card kpi"><div class="kpi__k">${h(label)}</div><div class="kpi__v">${h(value)}</div></div>`).join("");
      body.innerHTML = `<div class="card__head"><span class="card__title">用户反馈审核</span><span class="card__hint">${payload.items.length}</span></div>` +
        (payload.items.length
          ? payload.items.map((item) => `<div class="exp">
              <div class="exp__top">
                <span class="exp__q">${h(item.question)}</span>
                ${chip(FEEDBACK_STATUS_LABELS[item.status] || item.status, item.status === "open" ? "warn" : item.status === "approved" ? "ok" : "bad")}
                ${chip(labels[item.reason] || item.reason, "accent")}
                ${item.category ? chip(categoryLabel(item.category), "") : ""}
                ${chip(`×${item.occur_count}`, item.occur_count > 1 ? "warn" : "")}
                ${item.trace_id ? chip(`追踪 ${item.trace_id}`, "") : ""}
              </div>
              ${sqlBlock(item.sql, "被反馈的 SQL")}
              ${item.suggested_sql ? sqlBlock(item.suggested_sql, "反馈附带的建议 SQL（候选，未生效）") : ""}
              ${item.detail ? `<div class="small muted">${h(item.detail)}</div>` : ""}
              <div class="small muted mono">编号 ${item.id} · 会话 ${h(item.session_id || "-")} · 轮次 ${h(item.turn_index)} · ${h(item.created_at || "")}</div>
              ${item.status === "open"
                ? `<div class="grid" style="gap:8px;margin-top:8px">
                    <textarea class="field" data-fb-sql="${item.id}" rows="2" placeholder="修正 SQL"></textarea>
                    <input class="field" data-fb-note="${item.id}" placeholder="审核说明">
                    <div class="pill-row">
                      <button class="btn btn--sm btn--primary" data-fb-action="approve" data-fb-id="${item.id}" type="button">通过并转 verified</button>
                      <button class="btn btn--sm" data-fb-action="reject" data-fb-id="${item.id}" type="button">拒绝</button>
                      <button class="btn btn--sm" data-cmd="${h(reviewCmd(item))}" type="button">复制命令</button>
                    </div>
                  </div>`
                : `<div class="small muted">审核：${h(item.reviewer || "-")} · ${h(item.review_note || "—")}${item.reviewed_at ? ` · ${h(item.reviewed_at)}` : ""}</div>`}
            </div>`).join("")
          : empty("—"));
    }
    state.memory.error = "";
  } catch (error) {
    kpi.innerHTML = "";
    body.innerHTML = `<div class="card__body"><div class="notice notice--bad">加载失败：${h(error.message)}</div></div>`;
  }
}

/* ---------- cost ---------- */

function costBuckets(report, by) {
  const buckets = (by === "principal" ? report.by_principal : report.by_category) || {};
  const currency = (report.pricing || {}).currency || "";
  const rows = Object.entries(buckets).map(([name, item]) => {
    const cost = item.estimated_cost;
    const label = by === "principal" ? (name === "anonymous" ? "匿名" : name) : categoryLabel(name);
    return `<tr>
      <td class="mono">${h(label)}</td>
      <td class="num">${item.turns}</td>
      <td class="num">${item.total_tokens}</td>
      <td class="num">${item.prompt_tokens}</td>
      <td class="num">${item.completion_tokens}</td>
      <td class="num">${item.cached_tokens}</td>
      <td class="num">${item.unclassified_tokens}</td>
      <td class="num">${cost === null || cost === undefined ? "—" : `${Number(cost).toFixed(6)} ${h(currency)}`}</td>
    </tr>`;
  }).join("");
  if (!rows) return empty(`还没有 ${by === "principal" ? "用户" : "业务域"} 维度的 turn 事件。`);
  return `<div class="table-wrap"><table class="data"><thead><tr>
    <th>${by === "principal" ? "用户" : "业务域"}</th><th class="num">轮数</th><th class="num">总 token</th><th class="num">输入</th>
    <th class="num">输出</th><th class="num">缓存</th><th class="num">未分类</th><th class="num">估算费用</th>
  </tr></thead><tbody>${rows}</tbody></table></div>`;
}

async function renderCost(view) {
  let payload;
  try {
    payload = await api.cost();
  } catch (error) {
    view.innerHTML = `<div class="wrap"><div class="notice notice--bad">成本数据加载失败：${h(error.message)}</div></div>`;
    return;
  }
  state.cost.meta = payload;
  if (!payload.enabled || !payload.report) {
    state.cost.report = null;
    view.innerHTML = `<div class="wrap"><div class="notice notice--warn">${h(payload.reason || "成本数据不可用")}</div></div>`;
    return;
  }
  const report = payload.report;
  state.cost.report = report;
  const tokens = report.tokens || {};
  const cache = report.cache || {};
  const pricing = report.pricing || {};
  const priced = Boolean(pricing.configured);
  const cost = priced && report.estimated_cost !== null && report.estimated_cost !== undefined
    ? `${Number(report.estimated_cost).toFixed(6)} ${pricing.currency || ""}`
    : "未配置";
  const cacheRate = cache.hit_rate === null || cache.hit_rate === undefined ? "—" : pct(cache.hit_rate);
  view.innerHTML = `
    <div class="wrap">
      <div class="grid grid--4">
        <div class="card kpi"><div class="kpi__k">问答轮数</div><div class="kpi__v">${report.turns}</div></div>
        <div class="card kpi"><div class="kpi__k">总 token</div><div class="kpi__v">${tokens.total ?? 0}</div><div class="kpi__d">${tokens.prompt ?? 0} / ${tokens.completion ?? 0}</div></div>
        <div class="card kpi"><div class="kpi__k">结果缓存</div><div class="kpi__v">${cacheRate}</div><div class="kpi__d">${cache.hits ?? 0} 命中 · ${cache.saved_tokens ?? 0} token</div></div>
        <div class="card kpi ${priced ? "" : "kpi--bad"}"><div class="kpi__k">估算费用</div><div class="kpi__v">${h(cost)}</div></div>
      </div>
      <div class="card">
        <div class="card__head">
          <span class="card__title">按${state.cost.by === "principal" ? "用户" : "业务域"}拆分</span>
          <span class="card__hint" title="${h(payload.path)}">${payload.lines}${payload.malformed_lines ? ` · 坏行 ${payload.malformed_lines}` : ""}</span>
        </div>
        <div class="card__body">
          <div class="tabs" style="border:0;margin-bottom:10px">
            <button data-cost-by="category" aria-selected="${state.cost.by === "category"}" type="button">业务域</button>
            <button data-cost-by="principal" aria-selected="${state.cost.by === "principal"}" type="button">用户</button>
          </div>
          <div id="cost-table">${costBuckets(report, state.cost.by)}</div>
        </div>
      </div>
    </div>`;
  on(el(view, "[data-cost-by]").parentElement, "click", (event) => {
    const button = event.target.closest("[data-cost-by]");
    if (!button) return;
    state.cost.by = button.dataset.costBy;
    view.querySelectorAll("[data-cost-by]").forEach((node) =>
      node.setAttribute("aria-selected", String(node.dataset.costBy === state.cost.by)));
    document.getElementById("cost-table").innerHTML = costBuckets(report, state.cost.by);
  });
}

/* ---------- global ---------- */

function bindGlobal() {
  document.querySelectorAll(".nav__item").forEach((button) => {
    button.addEventListener("click", () => {
      location.hash = `#/${button.dataset.route}`;
    });
  });
  on(window, "hashchange", () => navigate());
  document.getElementById("btn-theme").addEventListener("click", () => {
    setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  });
  document.getElementById("btn-refresh").addEventListener("click", async () => {
    await loadHealth();
    await navigate({ force: true });
    toast("已重新载入");
  });
  on(document, "click", (event) => {
    const button = event.target.closest("[data-copy]");
    if (button) copyText(decodeURIComponent(button.dataset.copy));
  });
  on(document, "keydown", (event) => {
    if (event.altKey && ["1", "2", "3", "4", "5"].includes(event.key)) {
      location.hash = `#/${["chat", "eval", "schema", "memory", "cost"][Number(event.key) - 1]}`;
    }
  });
}

async function boot() {
  const saved = localStorage.getItem("nl2sql-theme");
  setTheme(saved || "dark");
  bindGlobal();
  await loadHealth();
  if (!state.health) {
    document.getElementById("view").innerHTML =
      `<div class="wrap"><div class="notice notice--bad">后端不可用。请确认已用 <code class="inline">nl2sql-agent serve</code> 启动，并安装可选依赖：<code class="inline">pip install -e ".[web]"</code></div></div>`;
    return;
  }
  await navigate();
}

boot();
