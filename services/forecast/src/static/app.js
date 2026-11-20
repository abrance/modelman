"use strict";

const form = document.getElementById("form");
const statusEl = document.getElementById("status");
const resultEl = document.getElementById("result");
const errorEl = document.getElementById("error");
const errorBody = document.getElementById("error-body");
const metaEl = document.getElementById("meta");
const tableEl = document.getElementById("table");
const canvas = document.getElementById("chart");
const submit = document.getElementById("submit");

let history = [];
let payload = null;

// 逗号、空格、换行都当分隔符：从表格里粘出来的一列数字三种都常见。
function parseValues(text) {
  const parts = text.split(/[\s,;]+/).filter((part) => part.length > 0);
  const values = parts.map(Number);
  if (values.length === 0) throw new Error("没有解析到数字");
  const bad = values.findIndex((value) => !Number.isFinite(value));
  if (bad >= 0) throw new Error(`第 ${bad + 1} 个值不是数字：${parts[bad]}`);
  return values;
}

function headers() {
  const token = document.getElementById("token").value.trim();
  return token ? { "x-auth-token": token } : {};
}

async function probe() {
  try {
    const response = await fetch("/readyz");
    const body = await response.json();
    statusEl.className = "status " + (response.ok ? "ok" : "bad");
    statusEl.textContent = response.ok
      ? `就绪 · ${body.tier}`
      : `未就绪 · ${body.error || body.status}`;
  } catch (exc) {
    statusEl.className = "status bad";
    statusEl.textContent = "读不到服务状态：" + exc.message;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  resultEl.hidden = true;
  errorEl.hidden = true;
  submit.disabled = true;
  submit.textContent = "预测中…";

  try {
    history = parseValues(document.getElementById("values").value);
    const horizon = Number(document.getElementById("horizon").value);
    const levels = Array.from(document.querySelectorAll("fieldset input:checked")).map(
      (input) => Number(input.value)
    );

    const request = { series: [{ values: history }], horizon };
    if (levels.length > 0) request.quantiles = levels;

    const response = await fetch("/v1/forecast", {
      method: "POST",
      headers: { "content-type": "application/json", ...headers() },
      body: JSON.stringify(request),
    });
    const body = await response.json();

    if (!response.ok || body.success !== true) {
      throw new Error(body.error || body.detail || `HTTP ${response.status}`);
    }

    payload = body;
    render(body);
    resultEl.hidden = false;
  } catch (exc) {
    errorBody.textContent = exc.message;
    errorEl.hidden = false;
  } finally {
    submit.disabled = false;
    submit.textContent = "预测";
  }
});

function render(body) {
  const item = body.forecasts[0];
  const levels = body.quantiles;
  metaEl.textContent =
    `${body.model} · 上下文 ${body.context_lengths[0]} 点 → 预测 ${body.horizon} 步 · ` +
    `分位 ${levels.join(", ")} · 耗时 ${body.time_ms} ms` +
    (body.truncated ? " · 输入被 MAX_CONTEXT 截断" : "");

  draw(history, item, levels);
  renderTable(history.length, item);
}

function renderTable(contextLength, item) {
  const levels = Object.keys(item.quantiles);
  const head = ["#", "点预测", ...levels];
  const rows = [];
  for (let step = 0; step < item.point.length; step += 1) {
    const cells = [String(contextLength + step + 1), fmt(item.point[step])];
    for (const level of levels) cells.push(fmt(item.quantiles[level][step]));
    rows.push(cells);
  }
  tableEl.innerHTML =
    "<table><thead><tr>" +
    head.map((cell) => `<th>${cell}</th>`).join("") +
    "</tr></thead><tbody>" +
    rows
      .map((cells) => "<tr>" + cells.map((cell) => `<td>${cell}</td>`).join("") + "</tr>")
      .join("") +
    "</tbody></table>";
}

function fmt(value) {
  return Number(value).toFixed(2);
}

function draw(hist, item, levels) {
  const ctx = canvas.getContext("2d");
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 880;
  const height = 300;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const pad = { top: 14, right: 12, bottom: 22, left: 52 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  const horizon = item.point.length;
  const total = hist.length + horizon;
  const all = hist.slice();
  for (const level of levels) all.push(...item.quantiles[level]);
  all.push(...item.point);

  let min = Math.min(...all);
  let max = Math.max(...all);
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  min -= span * 0.06;
  max += span * 0.06;

  const x = (index) => pad.left + (index / Math.max(1, total - 1)) * plotW;
  const y = (value) => pad.top + plotH - ((value - min) / (max - min)) * plotH;

  // 网格与坐标
  ctx.strokeStyle = "#eceef1";
  ctx.fillStyle = "#8a939d";
  ctx.font = "11px ui-monospace, monospace";
  ctx.lineWidth = 1;
  for (let tick = 0; tick <= 4; tick += 1) {
    const value = min + ((max - min) * tick) / 4;
    const yy = y(value);
    ctx.beginPath();
    ctx.moveTo(pad.left, yy);
    ctx.lineTo(pad.left + plotW, yy);
    ctx.stroke();
    ctx.fillText(value.toFixed(1), 6, yy + 4);
  }

  // 分位带：由外向内画，最外的两条构成 0.1–0.9 区间
  const sorted = levels.map(Number).sort((a, b) => a - b);
  const half = Math.floor(sorted.length / 2);
  for (let index = 0; index < half; index += 1) {
    const low = item.quantiles[String(sorted[index])];
    const high = item.quantiles[String(sorted[sorted.length - 1 - index])];
    if (!low || !high) continue;
    const alpha = 0.1 + 0.1 * index;
    ctx.fillStyle = `rgba(47, 111, 94, ${alpha})`;
    ctx.beginPath();
    low.forEach((value, step) => {
      const xx = x(hist.length + step);
      step === 0 ? ctx.moveTo(xx, y(value)) : ctx.lineTo(xx, y(value));
    });
    for (let step = high.length - 1; step >= 0; step -= 1) {
      ctx.lineTo(x(hist.length + step), y(high[step]));
    }
    ctx.closePath();
    ctx.fill();
  }

  // 历史
  ctx.strokeStyle = "#5b656f";
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  hist.forEach((value, index) => {
    index === 0 ? ctx.moveTo(x(index), y(value)) : ctx.lineTo(x(index), y(value));
  });
  ctx.stroke();

  // 点预测
  ctx.strokeStyle = "#2f6f5e";
  ctx.lineWidth = 2;
  ctx.beginPath();
  const last = hist[hist.length - 1];
  ctx.moveTo(x(hist.length - 1), y(last));
  item.point.forEach((value, step) => ctx.lineTo(x(hist.length + step), y(value)));
  ctx.stroke();

  // 分界
  ctx.strokeStyle = "#c8ccd2";
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(x(hist.length - 1), pad.top);
  ctx.lineTo(x(hist.length - 1), pad.top + plotH);
  ctx.stroke();
  ctx.setLineDash([]);
}

probe();
