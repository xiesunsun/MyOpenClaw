"""SessionTrajectory → 自包含单文件 HTML 观测平台(设计 §5)。

零外部资源:数据以 JSON 岛内嵌,样式与脚本全部 inline。
`</` 转义为 `<\\/` 防止正文中的 </script> 截断数据岛。
调色板为 dataviz 参考实例前三槽(明暗两模式均通过验证器)。
"""

from __future__ import annotations

import json

from pickel.observe.model import SessionTrajectory, trajectory_to_dict


def render_html(trajectories: list[SessionTrajectory], *, generated_at: str) -> str:
    if not trajectories:
        raise ValueError("没有可导出的会话轨迹")
    payload = json.dumps(
        [trajectory_to_dict(trajectory) for trajectory in trajectories],
        ensure_ascii=False,
    ).replace("</", "<\\/")
    return _TEMPLATE.replace("__GENERATED_AT__", generated_at).replace(
        "__DATA__", payload
    )


_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pickel 观测平台</title>
<style>
:root {
  color-scheme: light;
  --surface: #fcfcfb; --surface-2: #f2f1ee; --border: #dedcd6;
  --text-1: #0b0b0b; --text-2: #52514e; --text-3: #8a8880;
  --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a;
  --error: #b3261e; --error-bg: #fbeae9; --accent: #2a78d6;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --surface: #1a1a19; --surface-2: #242422; --border: #3a3936;
    --text-1: #ffffff; --text-2: #c3c2b7; --text-3: #8a8880;
    --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70;
    --error: #e66767; --error-bg: #3a2422; --accent: #3987e5;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--surface); color: var(--text-1);
  font: 14px/1.6 -apple-system, "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
  display: flex; height: 100vh; overflow: hidden;
}
#sidebar {
  width: 300px; min-width: 300px; border-right: 1px solid var(--border);
  overflow-y: auto; background: var(--surface-2); padding: 12px;
}
#sidebar h1 { font-size: 16px; margin: 4px 8px 12px; }
#sidebar .meta { color: var(--text-3); font-size: 12px; margin: 0 8px 12px; }
.session-item {
  padding: 10px 12px; border-radius: 8px; cursor: pointer;
  border: 1px solid transparent; margin-bottom: 6px;
}
.session-item:hover { background: var(--surface); }
.session-item.active { background: var(--surface); border-color: var(--accent); }
.session-item .sid { font-family: ui-monospace, monospace; font-size: 12px; color: var(--text-2); }
.session-item .line2 { font-size: 12px; color: var(--text-3); }
#main { flex: 1; overflow-y: auto; padding: 20px 28px; }
h2 { font-size: 18px; margin: 0 0 4px; }
.sub { color: var(--text-3); font-size: 12px; margin-bottom: 16px; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; margin-bottom: 20px; }
.card { background: var(--surface-2); border: 1px solid var(--border); border-radius: 10px; padding: 10px 14px; }
.card .label { font-size: 12px; color: var(--text-2); }
.card .value { font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; }
.card .hint { font-size: 11px; color: var(--text-3); }
section h3 { font-size: 15px; margin: 22px 0 10px; }
.legend { display: flex; gap: 16px; font-size: 12px; color: var(--text-2); margin-bottom: 6px; }
.legend .chip::before {
  content: ""; display: inline-block; width: 10px; height: 10px;
  border-radius: 3px; margin-right: 5px; background: var(--chip); vertical-align: -1px;
}
#chart-wrap { position: relative; overflow-x: auto; background: var(--surface-2); border: 1px solid var(--border); border-radius: 10px; padding: 10px; }
#tooltip {
  position: fixed; display: none; z-index: 10; pointer-events: none;
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 8px 10px; font-size: 12px; box-shadow: 0 4px 14px rgba(0,0,0,.18);
  font-variant-numeric: tabular-nums;
}
.turn { border: 1px solid var(--border); border-radius: 10px; margin-bottom: 14px; overflow: hidden; }
.turn-head { background: var(--surface-2); padding: 10px 14px; }
.turn-head .q { font-weight: 600; white-space: pre-wrap; }
.turn-head .badges { margin-top: 4px; }
.badge {
  display: inline-block; font-size: 11px; color: var(--text-2);
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 999px; padding: 1px 8px; margin-right: 6px;
}
.badge.err { color: var(--error); border-color: var(--error); background: var(--error-bg); }
.badge.trace { border-style: dashed; }
.step { padding: 10px 14px; border-top: 1px solid var(--border); }
.step .text, .final { white-space: pre-wrap; word-break: break-word; }
.tool { background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px; padding: 8px 12px; margin: 8px 0; }
.tool.error { border-color: var(--error); background: var(--error-bg); }
.tool summary { cursor: pointer; font-family: ui-monospace, monospace; font-size: 13px; }
.tool pre {
  margin: 6px 0 0; padding: 8px; background: var(--surface); border-radius: 6px;
  font-size: 12px; overflow-x: auto; white-space: pre-wrap; word-break: break-all;
}
.final-wrap { padding: 10px 14px; border-top: 1px solid var(--border); }
.fail-banner { padding: 8px 14px; background: var(--error-bg); color: var(--error); font-size: 13px; }
.muted { color: var(--text-3); font-size: 12px; }
svg text { fill: var(--text-2); font-size: 11px; font-variant-numeric: tabular-nums; }
svg .grid { stroke: var(--border); stroke-width: 1; }
svg .compaction { stroke: var(--text-3); stroke-width: 1.5; stroke-dasharray: 4 3; }
</style>
</head>
<body>
<div id="sidebar">
  <h1>Pickel 观测平台</h1>
  <div class="meta">生成于 <span id="generated-at">__GENERATED_AT__</span></div>
  <div id="session-list"></div>
</div>
<div id="main"></div>
<div id="tooltip"></div>
<script type="application/json" id="observe-data">__DATA__</script>
<script>
"use strict";
const DATA = JSON.parse(document.getElementById("observe-data").textContent);
const fmt = n => (n ?? 0).toLocaleString("zh-CN");
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const ms = v => v == null ? "—" : (v >= 1000 ? (v / 1000).toFixed(1) + " s" : v + " ms");

function sessionStats(t) {
  let steps = 0, tools = 0, errors = 0;
  for (const turn of t.turns) for (const s of turn.steps) {
    steps++;
    tools += s.tool_executions.length;
    errors += s.tool_executions.filter(e => e.is_error).length;
  }
  return { steps, tools, errors };
}

function renderSidebar() {
  const list = document.getElementById("session-list");
  list.innerHTML = DATA.map((t, i) => {
    const st = sessionStats(t);
    return `<div class="session-item" data-i="${i}" id="item-${i}">
      <div>${esc(t.title || t.turns[0]?.query.slice(0, 40) || "(无标题)")}</div>
      <div class="sid">${esc(t.session_id.slice(0, 8))} · ${esc(t.agent_id)}</div>
      <div class="line2">${t.turns.length} turn · ${st.steps} step · ${fmt(t.session_usage.actual_input)} tok</div>
    </div>`;
  }).join("");
  list.querySelectorAll(".session-item").forEach(el =>
    el.addEventListener("click", () => select(+el.dataset.i)));
}

function select(i) {
  document.querySelectorAll(".session-item").forEach(el => el.classList.remove("active"));
  document.getElementById("item-" + i).classList.add("active");
  renderMain(DATA[i]);
}

function overviewCards(t, st) {
  const u = t.session_usage;
  const hitRate = u.actual_input > 0 ? (100 * u.cache_read / u.actual_input).toFixed(1) + "%" : "—";
  const elapsed = t.turns.reduce((a, turn) => a + turn.elapsed_ms, 0);
  const cards = [
    ["Turn 数", t.turns.length, ""],
    ["Step 数", st.steps, ""],
    ["工具调用", st.tools, st.errors ? st.errors + " 次错误" : "无错误"],
    ["实际输入 tokens", fmt(u.actual_input), "input+cache_read+cache_write"],
    ["输出 tokens", fmt(u.output), ""],
    ["缓存命中率", hitRate, "cache_read / 实际输入"],
    ["模型耗时", ms(elapsed), "全部 step 合计"],
    ["压缩次数", t.compaction_steps.length, "compaction"],
  ];
  return `<div class="cards">` + cards.map(([label, value, hint]) =>
    `<div class="card"><div class="label">${label}</div><div class="value">${value}</div>` +
    (hint ? `<div class="hint">${hint}</div>` : "") + `</div>`).join("") + `</div>`;
}

function runtimeMetricCards(t) {
  const m = t.metrics || {};
  if (!m.provider?.count && !m.tool?.count && !m.turn?.count) return "";
  const rate = x => x == null ? "—" : (x * 100).toFixed(1) + "%";
  const pct = (group, key) => ms(group?.[key]);
  const cards = [
    ["Turn 成功率", rate(m.turn?.success_rate), `${m.turn?.count || 0} 次`],
    ["Provider 成功率", rate(m.provider?.success_rate), `${m.provider?.count || 0} 次`],
    ["模型 P50", pct(m.provider?.duration_ms, "p50"), "完整响应"],
    ["模型 P95", pct(m.provider?.duration_ms, "p95"), "完整响应"],
    ["TTFT P50", pct(m.provider?.ttft_ms, "p50"), "首个流式块"],
    ["TTFT P95", pct(m.provider?.ttft_ms, "p95"), "首个流式块"],
    ["工具成功率", rate(m.tool?.success_rate), `${m.tool?.count || 0} 次`],
    ["工具 P95", pct(m.tool?.duration_ms, "p95"), "执行副作用"],
    ["Hook 失败", fmt(m.hook?.failure_count || 0), `${m.hook?.count || 0} 次调用`],
    ["缓存读取", fmt(m.provider?.tokens?.cache_read_tokens || 0), "tokens"],
  ];
  return `<section><h3>运行指标（Trace）</h3><div class="cards">` +
    cards.map(([label, value, hint]) =>
      `<div class="card"><div class="label">${label}</div><div class="value">${value}</div>` +
      `<div class="hint">${hint}</div></div>`).join("") + `</div></section>`;
}

const SERIES = [
  ["input", "var(--series-1)", "input"],
  ["cache_read", "var(--series-2)", "cache_read"],
  ["cache_write", "var(--series-3)", "cache_write"],
];

function contextChart(t) {
  const steps = t.turns.flatMap(turn => turn.steps);
  if (!steps.length) return `<p class="muted">无 step 数据</p>`;
  const W = Math.max(560, steps.length * 26 + 70), H = 220;
  const padL = 56, padB = 24, padT = 12;
  const plotH = H - padB - padT;
  const max = Math.max(1, ...steps.map(s => s.usage.actual_input));
  const barW = Math.min(18, Math.max(6, Math.floor((W - padL - 10) / steps.length) - 2));
  let bars = "", ticks = "";
  steps.forEach((s, i) => {
    const x = padL + i * (barW + 2);
    let y = H - padB;
    let segs = "";
    for (const [key, color] of SERIES) {
      const v = s.usage[key];
      if (v <= 0) continue;
      const h = Math.max(1, plotH * v / max);
      y -= h;
      segs += `<rect x="${x}" y="${y}" width="${barW}" height="${Math.max(0, h - 2)}" rx="2" fill="${color}"/>`;
    }
    bars += `<g class="bar" data-i="${i}">` +
      `<rect x="${x - 1}" y="${padT}" width="${barW + 2}" height="${plotH}" fill="transparent"/>` + segs + `</g>`;
    if (steps.length <= 30 || i % Math.ceil(steps.length / 20) === 0)
      ticks += `<text x="${x + barW / 2}" y="${H - 8}" text-anchor="middle">${i + 1}</text>`;
  });
  let grid = "";
  for (let g = 0; g <= 4; g++) {
    const gy = padT + plotH * g / 4;
    grid += `<line class="grid" x1="${padL - 4}" y1="${gy}" x2="${W - 6}" y2="${gy}"/>` +
      `<text x="${padL - 8}" y="${gy + 4}" text-anchor="end">${fmt(Math.round(max * (4 - g) / 4))}</text>`;
  }
  let compaction = "";
  for (const c of t.compaction_steps) {
    const cx = padL + c * (barW + 2) - 1.5;
    compaction += `<line class="compaction" x1="${cx}" y1="${padT}" x2="${cx}" y2="${H - padB}"/>` +
      `<text x="${cx + 3}" y="${padT + 10}">压缩</text>`;
  }
  const legend = `<div class="legend">` + SERIES.map(([, color, name]) =>
    `<span class="chip" style="--chip:${color}">${name}</span>`).join("") +
    (t.compaction_steps.length ? `<span class="muted">┆ 压缩点</span>` : "") + `</div>`;
  return legend +
    `<div id="chart-wrap"><svg id="ctx-chart" width="${W}" height="${H}" role="img" aria-label="每 step 实际输入 token 堆叠图">` +
    grid + bars + compaction + `</svg></div>`;
}

function bindChartTooltip(t) {
  const svg = document.getElementById("ctx-chart");
  if (!svg) return;
  const tip = document.getElementById("tooltip");
  const steps = t.turns.flatMap(turn => turn.steps);
  svg.querySelectorAll("g.bar").forEach(g => {
    g.addEventListener("mousemove", event => {
      const s = steps[+g.dataset.i];
      tip.style.display = "block";
      tip.style.left = (event.clientX + 14) + "px";
      tip.style.top = (event.clientY + 14) + "px";
      tip.innerHTML = `<b>step ${+g.dataset.i + 1}</b> · ${esc(s.model_label)}<br>` +
        `实际输入 ${fmt(s.usage.actual_input)}<br>` +
        `input ${fmt(s.usage.input)} · cache_read ${fmt(s.usage.cache_read)} · cache_write ${fmt(s.usage.cache_write)}<br>` +
        `output ${fmt(s.usage.output)} · 耗时 ${ms(s.elapsed_ms)}`;
    });
    g.addEventListener("mouseleave", () => { tip.style.display = "none"; });
  });
}

function toolCard(e) {
  const badges =
    (e.is_error ? `<span class="badge err">错误</span>` : "") +
    (e.orphan ? `<span class="badge">孤儿结果</span>` : "") +
    (e.duration_ms != null ? `<span class="badge trace">${ms(e.duration_ms)} · trace</span>` : "");
  return `<details class="tool${e.is_error ? " error" : ""}">
    <summary>${esc(e.name)} ${badges}</summary>
    <pre>${esc(JSON.stringify(e.arguments, null, 2))}</pre>
    ${e.result_preview ? `<pre>${esc(e.result_preview)}</pre>` : `<p class="muted">（无结果）</p>`}
  </details>`;
}

function digestBlock(s) {
  const d = s.request_digest;
  if (!d) return "";
  const sections = d.system_sections
    .map(x => `${esc(x.name)} ${fmt(x.chars)} 字符`).join(" · ") || "（无 system）";
  const tools = d.tool_names.length
    ? d.tool_names.map(esc).join(", ") : "（无工具）";
  return `<details class="tool">
    <summary>请求摘要 <span class="badge trace">trace · 非真源</span></summary>
    <pre>system: ${sections}
tools (${d.tool_names.length}): ${tools}
messages: ${d.message_count} 条 · 全文 ${fmt(d.request_chars)} 字符` +
    (d.hook_injected_chars ? `\nhook 注入: ${fmt(d.hook_injected_chars)} 字符` : "") +
    `</pre></details>`;
}

function stepBlock(s) {
  const badges =
    `<span class="badge">step ${s.index + 1}</span>` +
    `<span class="badge">${esc(s.model_label)}</span>` +
    (s.finish_reason ? `<span class="badge">${esc(s.finish_reason)}</span>` : "") +
    `<span class="badge">实际输入 ${fmt(s.usage.actual_input)}</span>` +
    `<span class="badge">输出 ${fmt(s.usage.output)}</span>` +
    `<span class="badge">${ms(s.elapsed_ms)}</span>` +
    (s.thinking_chars ? `<span class="badge">思考 ${fmt(s.thinking_chars)} 字符</span>` : "") +
    (s.hook_injected_chars ? `<span class="badge">hook 注入 ${fmt(s.hook_injected_chars)} 字符</span>` : "");
  return `<div class="step"><div class="badges">${badges}</div>` +
    digestBlock(s) +
    s.tool_executions.map(toolCard).join("") +
    (s.text ? `<div class="text">${esc(s.text)}</div>` : "") + `</div>`;
}

function turnBlock(turn) {
  const head =
    `<div class="turn-head"><div class="q">▸ ${esc(turn.query || "（无 query）")}</div>` +
    `<div class="badges">` +
    `<span class="badge">turn ${turn.index + 1}</span>` +
    `<span class="badge">${turn.steps.length} step</span>` +
    `<span class="badge">实际输入 ${fmt(turn.usage_totals.actual_input)}</span>` +
    `<span class="badge">输出 ${fmt(turn.usage_totals.output)}</span>` +
    `<span class="badge">${ms(turn.elapsed_ms)}</span>` +
    (turn.started_at ? `<span class="badge trace">${esc(turn.started_at)} · trace</span>` : "") +
    (turn.interrupted ? `<span class="badge err">已中断</span>` : "") +
    `</div></div>`;
  const fail = turn.failed
    ? `<div class="fail-banner">turn 失败：${esc(turn.failed.error_type)} — ${esc(turn.failed.message)}</div>`
    : "";
  return `<div class="turn">${head}${fail}` +
    turn.steps.map(stepBlock).join("") +
    (turn.final_text
      ? `<div class="final-wrap"><div class="muted">最终回复</div><div class="final">${esc(turn.final_text)}</div></div>`
      : "") + `</div>`;
}

function renderMain(t) {
  const st = sessionStats(t);
  document.getElementById("main").innerHTML =
    `<h2>${esc(t.title || t.session_id)}</h2>` +
    `<div class="sub">${esc(t.session_id)} · ${esc(t.agent_id)} · ${esc(t.cwd)} · ` +
    `${esc(t.created_at)} ~ ${esc(t.updated_at)}` +
    (t.trace_available ? " · <span title='时间戳与终态来自 trace,非真源'>trace 增强</span>" : "") + `</div>` +
    overviewCards(t, st) +
    runtimeMetricCards(t) +
    `<section><h3>上下文占用（每 step 实际输入）</h3>${contextChart(t)}</section>` +
    `<section><h3>执行轨迹</h3>${t.turns.map(turnBlock).join("")}</section>`;
  bindChartTooltip(t);
}

renderSidebar();
if (DATA.length) select(0);
</script>
</body>
</html>
"""
