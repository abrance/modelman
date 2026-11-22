"""自带页面：粘贴历史 JSON → 调 /v1/price-forecast → 表格展示。

免鉴权是刻意的（与 OCR/日志聚类同口径，见 design.md D15-D17）：页面只暴露
结构，不含任何数据；真正的业务端点仍走 token。
"""

from __future__ import annotations

from fastapi.responses import Response

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>modelman price-lear</title>
<style>
body{font:14px/1.6 system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem}
textarea{width:100%;height:180px;font:12px/1.4 monospace}
button{padding:.4rem 1rem}
pre{background:#f4f4f4;padding:.6rem;overflow:auto}
table{border-collapse:collapse;margin-top:1rem}
td,th{border:1px solid #ccc;padding:.2rem .6rem;text-align:right}
</style>
</head>
<body>
<h1>广东现货日前小时电价 · LEAR 预测</h1>
<p>输入完整历史日（升序，最后一天为锚点）与目标日，POST /v1/price-forecast。</p>
<p><input id="token" placeholder="AUTH_TOKEN（启用时填写）" style="width:14rem"></p>
<p><label>目标日 <input id="target" placeholder="YYYY-MM-DD"></label></p>
<textarea id="days" placeholder='[{"date":"2026-11-09","values":[...24 个价...],"power":[...24 个电量...]}, ...]'></textarea>
<p><button onclick="run()">预测</button></p>
<pre id="out"></pre>
<table id="tbl" hidden></table>
<script>
async function run() {
  const out = document.getElementById('out'), tbl = document.getElementById('tbl');
  out.textContent = '请求中…'; tbl.hidden = true;
  const headers = {'Content-Type': 'application/json'};
  const token = document.getElementById('token').value.trim();
  if (token) headers['X-Auth-Token'] = token;
  try {
    const res = await fetch('/v1/price-forecast', {
      method: 'POST', headers,
      body: JSON.stringify({days: JSON.parse(document.getElementById('days').value),
                            target: document.getElementById('target').value.trim()})
    });
    const body = await res.json();
    if (!res.ok) { out.textContent = JSON.stringify(body, null, 2); return; }
    out.textContent = `model=${body.model} target=${body.target} horizon=${body.horizon_days}天 耗时${body.time_ms.toFixed(1)}ms`;
    let html = '<tr><th>小时</th><th>10%</th><th>中位</th><th>90%</th></tr>';
    for (let h = 0; h < 24; h++) html += `<tr><td>${h}:00</td><td>${body.quantiles['0.1'][h].toFixed(2)}</td><td>${body.point[h].toFixed(2)}</td><td>${body.quantiles['0.9'][h].toFixed(2)}</td></tr>`;
    tbl.innerHTML = html; tbl.hidden = false;
  } catch (err) { out.textContent = String(err); }
}
</script>
</body>
</html>"""


def asset() -> Response:
    return Response(
        content=INDEX_HTML,
        media_type="text/html; charset=utf-8",
        headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"},
    )
