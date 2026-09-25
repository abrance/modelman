/* 日志聚类页面的逻辑。
 *
 * 无构建、无框架、无外部依赖：一个文件、原生 DOM。页面里的每一段文本都来自
 * 服务端或用户粘贴的日志，也就是**不可信输入**，所以渲染一律走 textContent，
 * 全文件不出现 innerHTML（tests/test_http.py 里有断言兜着）。
 *
 * 三个动作对应三个接口，其中只有「聚类」会改服务端状态（模板树），
 * 界面上必须让人一眼看出来，见 docs/logcluster-ui.md。
 */

"use strict";

const TOKEN_KEY = "logcluster.token";
const STATUS_POLL_MS = 10000;

const $ = (id) => document.getElementById(id);

const els = {
  state: $("stat-state"),
  counts: $("stat-counts"),
  params: $("stat-params"),
  drop: $("drop"),
  file: $("file"),
  lines: $("lines"),
  budget: $("budget"),
  notice: $("notice"),
  cluster: $("cluster"),
  match: $("match"),
  clear: $("clear"),
  summary: $("summary"),
  results: $("results"),
  templates: $("templates"),
  templateSummary: $("template-summary"),
  refresh: $("refresh"),
  tokenBox: $("token-box"),
  tokenInput: $("token-input"),
};

/* 服务端报的上限。页面不硬编码任何参数：全部读自 /version，
 * 免得调了 SIM_TH / MAX_LINES 之后界面还在说旧数。 */
const limits = { max_lines: 0, max_line_chars: 0, max_bytes: 0 };

/* ── 与后端交互 ─────────────────────────────────────────────────── */

function token() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

function headers(extra) {
  const out = Object.assign({}, extra || {});
  const value = token();
  if (value) out["X-Auth-Token"] = value;
  return out;
}

/** 统一发请求：把服务端的 error 字段原样带出来，不自己编文案。 */
async function api(path, options) {
  const opts = Object.assign({}, options || {});
  opts.headers = headers(opts.headers);
  let response;
  try {
    response = await fetch(path, opts);
  } catch (err) {
    return { ok: false, status: 0, error: "网络请求失败：" + err.message };
  }
  let payload = null;
  const text = await response.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch (err) {
      payload = null;
    }
  }
  if (!response.ok) {
    const message =
      (payload && (payload.error || payload.detail)) ||
      text ||
      "HTTP " + response.status;
    return { ok: false, status: response.status, error: String(message) };
  }
  return { ok: true, status: response.status, body: payload };
}

function onUnauthorized(result) {
  if (result.status !== 401) return false;
  els.tokenBox.open = true;
  notice("接口要 token，填在下面那个折叠区里。", true);
  return true;
}

/* ── 状态条 ─────────────────────────────────────────────────────── */

async function loadStatus() {
  const version = await api("/version");
  if (version.ok) {
    const v = version.body;
    limits.max_lines = v.max_lines;
    limits.max_line_chars = v.max_line_chars;
    limits.max_bytes = v.max_bytes;
    els.params.textContent =
      v.profile_id +
      " · sim_th " + v.sim_th +
      " · depth " + v.depth +
      " · 掩码 " + (v.mask_rules.length || 0) + " 条";
    if (v.auth_required) els.tokenBox.open = els.tokenBox.open || !token();
    setState(v.state_loaded ? "ok" : "warn",
      v.state_loaded ? "模板树已就绪" : "状态尚未加载");
    refreshBudget();
  } else {
    setState("bad", "读不到 /version");
  }

  const health = await api("/healthz");
  if (health.ok) {
    const h = health.body;
    els.counts.textContent =
      "模板 " + h.cluster_count + " 个 · 覆盖 " + h.total_size + " 行 · 已跑 " +
      Math.round(h.uptime_secs / 60) + " 分钟";
  }
}

function setState(kind, text) {
  els.state.className = "chip " + kind;
  els.state.textContent = text;
}

function notice(text, bad) {
  els.notice.textContent = text || "";
  els.notice.className = bad ? "notice bad" : "notice";
}

/* ── 输入与上限 ─────────────────────────────────────────────────── */

/** 按行拆，去掉空行（空行会各变成一条模板，白白污染模板树）。 */
function parseInput() {
  const raw = els.lines.value.split(/\r?\n/);
  const lines = [];
  let skipped = 0;
  const tooLong = [];
  raw.forEach((line) => {
    const value = line.replace(/\r$/, "");
    if (value.trim() === "") {
      skipped += 1;
      return;
    }
    if (limits.max_line_chars && value.length > limits.max_line_chars) {
      tooLong.push({ number: lines.length + 1, length: value.length });
    }
    lines.push(value);
  });
  return { lines: lines, skipped: skipped, tooLong: tooLong };
}

/** 与体积上限逐字对齐：算的是 JSON 请求体的**字节数**，
 * 因为服务端卡的正是 content-length。 */
function bodyBytes(lines) {
  return new TextEncoder().encode(JSON.stringify({ lines: lines })).length;
}

function refreshBudget() {
  const parsed = parseInput();
  const bytes = bodyBytes(parsed.lines);
  els.budget.textContent =
    parsed.lines.length + " 行 · " + bytes + " B" +
    (limits.max_bytes ? "（上限 " + limits.max_lines + " 行 / " + limits.max_bytes + " B）" : "");

  let reason = "";
  if (!parsed.lines.length) {
    reason = "";
  } else if (limits.max_lines && parsed.lines.length > limits.max_lines) {
    reason = "行数 " + parsed.lines.length + " 超过上限 " + limits.max_lines;
  } else if (parsed.tooLong.length) {
    const first = parsed.tooLong[0];
    reason =
      "第 " + first.number + " 行长度 " + first.length +
      " 超过上限 " + limits.max_line_chars +
      (parsed.tooLong.length > 1 ? "（共 " + parsed.tooLong.length + " 行超长）" : "");
  } else if (limits.max_bytes && bytes > limits.max_bytes) {
    reason = "请求体 " + bytes + " B 超过上限 " + limits.max_bytes + " B";
  }

  els.cluster.disabled = Boolean(reason) || !parsed.lines.length;
  els.match.disabled = Boolean(reason) || !parsed.lines.length;
  notice(reason, Boolean(reason));
  return parsed;
}

function appendText(text, sourceName) {
  const value = String(text || "").replace(/\r\n?/g, "\n");
  if (!value.trim()) return 0;
  const current = els.lines.value;
  els.lines.value = current && !current.endsWith("\n") ? current + "\n" + value : current + value;
  return value.split("\n").filter((line) => line.trim() !== "").length;
}

async function addFiles(files) {
  const list = Array.from(files || []);
  if (!list.length) return;
  let added = 0;
  let skipped = 0;
  for (const file of list) {
    if (file.type && !file.type.startsWith("text/") && !/\.(log|txt|out|json)$/i.test(file.name)) {
      skipped += 1;
      continue;
    }
    added += appendText(await file.text(), file.name);
  }
  refreshBudget();
  if (skipped) notice("忽略了 " + skipped + " 个非文本文件");
  else if (added) notice("从文件读入 " + added + " 行");
}

/* ── 三个动作 ───────────────────────────────────────────────────── */

async function runCluster() {
  const parsed = refreshBudget();
  if (!parsed.lines.length || els.cluster.disabled) return;
  await post("cluster", parsed);
}

async function runMatch() {
  const parsed = refreshBudget();
  if (!parsed.lines.length || els.match.disabled) return;
  await post("match", parsed);
}

async function post(kind, parsed) {
  const path = kind === "cluster" ? "/cluster" : "/match";
  els.cluster.disabled = true;
  els.match.disabled = true;
  els.summary.textContent = kind === "cluster" ? "正在聚类（会写入模板树）…" : "正在匹配（只读）…";
  els.results.replaceChildren();

  const result = await api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lines: parsed.lines }),
  });

  refreshBudget();
  if (!result.ok) {
    onUnauthorized(result);
    els.summary.textContent = "";
    notice("请求失败：" + result.error, true);
    return;
  }

  // 先刷新状态与模板树，最后再渲染结果：
  // 那两步的 refreshBudget() 会把 #notice 清掉，结果里的提示（例如忽略了 N 个空行）
  // 必须放在它们后面写，否则写完就被抹掉。
  if (kind === "cluster") {
    await loadTemplates();
    await loadStatus();
  }
  render(kind, result.body, parsed);
}

/* ── 渲染 ───────────────────────────────────────────────────────── */

const CHANGE_LABEL = {
  cluster_created: "新建模板",
  cluster_template_changed: "模板被改写",
  none: "命中既有模板",
};

function render(kind, payload, parsed) {
  const results = payload.results || [];
  els.summary.className = "summary";
  els.summary.replaceChildren();

  if (kind === "cluster") {
    const created = results.filter((r) => r.change_type === "cluster_created").length;
    const changed = results.filter((r) => r.change_type === "cluster_template_changed").length;
    els.summary.appendChild(
      document.createTextNode(
        "处理 " + results.length + " 行 · " + Math.round(payload.time_ms) + " ms" +
        " · 模板树现有 " + payload.cluster_count + " 个模板 · "
      )
    );
    if (created || changed) {
      const strong = document.createElement("strong");
      strong.className = "write";
      strong.textContent = "本次新建 " + created + " 个、改写 " + changed + " 个模板";
      els.summary.appendChild(strong);
    } else {
      els.summary.appendChild(document.createTextNode("本次没有改动模板树"));
    }
  } else {
    const hit = results.filter((r) => r.matched).length;
    els.summary.appendChild(
      document.createTextNode(
        "只匹配（未写入）· 处理 " + results.length + " 行 · 命中 " + hit +
        " 行 · 未命中 " + (results.length - hit) + " 行 · " +
        Math.round(payload.time_ms) + " ms"
      )
    );
  }

  const list = document.createDocumentFragment();
  results.forEach((item) => list.appendChild(resultRow(kind, item)));
  els.results.replaceChildren(list);

  if (parsed.skipped) {
    notice("忽略了 " + parsed.skipped + " 个空行", false);
  }
}

function resultRow(kind, item) {
  const li = document.createElement("li");
  if (kind === "cluster" && item.change_type && item.change_type !== "none") li.className = "changed";

  const line = document.createElement("p");
  line.className = "line";
  line.textContent = item.line;
  li.appendChild(line);

  const template = document.createElement("p");
  template.className = "tpl";
  template.textContent = item.template || "（没有对应模板）";
  li.appendChild(template);

  const meta = document.createElement("div");
  meta.className = "meta";

  if (kind === "cluster") {
    meta.appendChild(tag("#" + item.cluster_id, ""));
    meta.appendChild(
      tag(CHANGE_LABEL[item.change_type] || item.change_type, item.change_type === "none" ? "" : "new")
    );
    (item.parameters || []).forEach((param) => meta.appendChild(tag(param, "param")));
  } else {
    meta.appendChild(
      tag(item.matched ? "命中 #" + item.cluster_id : "未命中", item.matched ? "match" : "miss")
    );
  }

  if (item.template) {
    const copy = document.createElement("button");
    copy.className = "ghost copy";
    copy.textContent = "复制模板";
    copy.addEventListener("click", () => copyText(item.template, copy));
    meta.appendChild(copy);
  }

  li.appendChild(meta);
  return li;
}

function tag(text, className) {
  const span = document.createElement("span");
  span.className = "tag" + (className ? " " + className : "");
  span.textContent = text;
  return span;
}

async function copyText(text, button) {
  const original = button.textContent;
  let done = false;
  try {
    await navigator.clipboard.writeText(text);
    done = true;
  } catch (err) {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try {
      done = document.execCommand("copy");
    } catch (fallbackErr) {
      done = false;
    }
    document.body.removeChild(area);
  }
  button.textContent = done ? "已复制" : "复制失败";
  window.setTimeout(() => {
    button.textContent = original;
  }, 1500);
}

async function loadTemplates() {
  const result = await api("/clusters");
  if (!result.ok) {
    onUnauthorized(result);
    els.templateSummary.textContent = "读不到模板树：" + result.error;
    els.templates.replaceChildren();
    return;
  }
  const list = (result.body.clusters || []).slice().sort((a, b) => b.size - a.size);
  els.templateSummary.textContent =
    list.length + " 个模板 · 累计 " + result.body.total_size + " 行";

  const fragment = document.createDocumentFragment();
  list.forEach((cluster) => {
    const li = document.createElement("li");

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.appendChild(tag("#" + cluster.cluster_id, ""));
    meta.appendChild(tag(cluster.size + " 行", ""));

    const copy = document.createElement("button");
    copy.className = "ghost copy";
    copy.textContent = "复制";
    copy.addEventListener("click", () => copyText(cluster.template, copy));
    meta.appendChild(copy);
    li.appendChild(meta);

    const template = document.createElement("p");
    template.className = "tpl";
    template.textContent = cluster.template;
    li.appendChild(template);
    fragment.appendChild(li);
  });
  els.templates.replaceChildren(fragment);
}

/* ── 事件绑定 ───────────────────────────────────────────────────── */

els.lines.addEventListener("input", refreshBudget);
els.cluster.addEventListener("click", runCluster);
els.match.addEventListener("click", runMatch);
els.refresh.addEventListener("click", loadTemplates);

els.clear.addEventListener("click", () => {
  els.lines.value = "";
  els.results.replaceChildren();
  els.summary.textContent = "还没有结果。贴几行日志，点「聚类」或「只匹配」。";
  els.summary.className = "summary muted";
  notice("");
  refreshBudget();
});

els.drop.addEventListener("click", () => els.file.click());
els.drop.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    els.file.click();
  }
});
els.file.addEventListener("change", () => {
  addFiles(els.file.files);
  els.file.value = "";
});

["dragenter", "dragover"].forEach((name) => {
  els.drop.addEventListener(name, (event) => {
    event.preventDefault();
    els.drop.classList.add("hot");
  });
});
["dragleave", "drop"].forEach((name) => {
  els.drop.addEventListener(name, (event) => {
    event.preventDefault();
    els.drop.classList.remove("hot");
  });
});
els.drop.addEventListener("drop", (event) => {
  if (event.dataTransfer) addFiles(event.dataTransfer.files);
});

els.tokenInput.value = token();
els.tokenInput.addEventListener("change", () => {
  const value = els.tokenInput.value.trim();
  if (value) localStorage.setItem(TOKEN_KEY, value);
  else localStorage.removeItem(TOKEN_KEY);
  loadTemplates();
  loadStatus();
});

/* ── 启动 ───────────────────────────────────────────────────────── */

loadStatus().then(loadTemplates);
window.setInterval(loadStatus, STATUS_POLL_MS);
refreshBudget();
