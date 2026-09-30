"use strict";

// 判定台：把 state + questions 打到 /v1/systemone，把 answers 摊平成可读卡片。
// 只做展示，不含任何业务判定逻辑——阈值之类的决定留在调用方。

const SAMPLE_STATE = {
  prompt: "帮我查一下 k8s 里 pod 为什么一直 pending",
  tools: [
    "bash: Execute bash commands in the current working directory",
    "grep: Search file contents for patterns, respects .gitignore",
    "kubectl_describe: Show details of a Kubernetes resource",
    "ocr_image: Extract text from an image using the NAS OCR service"
  ]
};

const SAMPLE_QUESTIONS = {
  route: {
    type: "choice",
    instructions: "Which tool should handle this request?",
    criteria: {
      bash: "shell commands",
      grep: "search file contents",
      kubectl_describe: "kubernetes resource inspection",
      ocr_image: "read text off an image",
      none: "no listed tool fits"
    }
  },
  needed: {
    type: "noul",
    instructions: "Is any listed tool actually needed for this request?"
  },
  complexity: {
    type: "score",
    instructions: "How complex is this request?",
    criteria: ["trivial", "moderate", "complex"]
  }
};

const $ = (id) => document.getElementById(id);

function authHeaders() {
  const token = $("token").value.trim();
  return token ? { Authorization: "Bearer " + token, "X-Auth-Token": token } : {};
}

function reset() {
  $("state").value = JSON.stringify(SAMPLE_STATE, null, 2);
  $("questions").value = JSON.stringify(SAMPLE_QUESTIONS, null, 2);
  $("answers").textContent = "";
  $("usage").hidden = true;
  showError(null);
}

function showError(message) {
  const node = $("error");
  if (!message) {
    node.hidden = true;
    node.textContent = "";
    return;
  }
  node.hidden = false;
  node.textContent = message;
}

async function refreshStatus() {
  try {
    const resp = await fetch("/readyz");
    const body = await resp.json().catch(() => ({}));
    setStatus("s-readyz", body.status || resp.status, resp.ok);
  } catch (err) {
    setStatus("s-readyz", "unreachable", false);
  }
  try {
    const resp = await fetch("/version");
    const body = await resp.json();
    $("s-tier").textContent = body.tier || "?";
  } catch (err) {
    $("s-tier").textContent = "?";
  }
  try {
    const resp = await fetch("/models");
    const body = await resp.json();
    const state = (body[0] && body[0].state) || {};
    const seconds = state.load_seconds;
    $("s-load").textContent =
      typeof seconds === "number" ? seconds.toFixed(2) + " s" : "未加载";
  } catch (err) {
    $("s-load").textContent = "?";
  }
  setStatus("s-livez", "ok", true);
}

function setStatus(id, text, ok) {
  const node = $(id);
  node.textContent = text;
  node.classList.toggle("ok", !!ok);
  node.classList.toggle("bad", ok === false);
}

function answerCard(qid, answer) {
  const wrap = document.createElement("div");
  wrap.className = "answer";

  const label = document.createElement("div");
  label.className = "qid";
  label.textContent = qid;
  wrap.appendChild(label);

  const value = document.createElement("div");
  value.className = "value";

  let confidence = answer.confidence;
  if (typeof answer.choice === "string") {
    value.textContent = answer.choice;
  } else if (typeof answer.noul === "number") {
    value.textContent = "P(true) = " + answer.noul.toFixed(3);
    } else if (typeof answer.score === "number") {
    value.textContent = answer.score.toFixed(2);
  } else {
    value.textContent = JSON.stringify(answer);
  }
  wrap.appendChild(value);

  // choice 的 distribution 是各选项概率，画出来比只看冠军更有信息量
  const distribution = answer.distribution;
  if (distribution && typeof distribution === "object") {
    Object.keys(distribution).forEach((name) => {
      const probability = Number(distribution[name]);
      const row = document.createElement("div");
      const bar = document.createElement("div");
      bar.className = "bar";
      const fill = document.createElement("span");
      const width = Math.max(0, Math.min(1, probability)) * 100;
      fill.style.width = width.toFixed(1) + "%";
      bar.appendChild(fill);
      const text = document.createElement("div");
      text.className = "conf";
      text.textContent = name + "  " + probability.toFixed(3);
      row.appendChild(text);
      row.appendChild(bar);
      wrap.appendChild(row);
    });
  } else if (typeof confidence === "number") {
    const text = document.createElement("div");
    text.className = "conf";
    text.textContent = "confidence " + confidence.toFixed(3);
    wrap.appendChild(text);
  }

  return wrap;
}

async function run() {
  showError(null);
  $("answers").textContent = "";
  $("usage").hidden = true;

  let state;
  let questions;
  try {
    state = JSON.parse($("state").value);
  } catch (err) {
    showError("state 不是合法 JSON：" + err.message);
    return;
  }
  try {
    questions = JSON.parse($("questions").value);
  } catch (err) {
    showError("questions 不是合法 JSON：" + err.message);
    return;
  }

  $("run").disabled = true;
  $("timing").textContent = "判定中…";
  const started = performance.now();
  try {
    const resp = await fetch("/v1/systemone", {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, authHeaders()),
      body: JSON.stringify({ state: state, questions: questions })
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      showError("HTTP " + resp.status + "：" + (body.error || "未知错误"));
      return;
    }
    const answers = body.answers || {};
    Object.keys(answers).forEach((qid) => {
      $("answers").appendChild(answerCard(qid, answers[qid]));
    });
    $("usage").hidden = false;
    $("usage").textContent = JSON.stringify(body.usage || {}, null, 2);
    $("timing").textContent =
      "服务端 " + (body.time_ms || 0).toFixed(1) + " ms，往返 " +
      (performance.now() - started).toFixed(0) + " ms";
  } catch (err) {
    showError("请求失败：" + err.message);
  } finally {
    $("run").disabled = false;
  }
}

$("run").addEventListener("click", run);
$("reset").addEventListener("click", reset);
reset();
refreshStatus();
setInterval(refreshStatus, 15000);
