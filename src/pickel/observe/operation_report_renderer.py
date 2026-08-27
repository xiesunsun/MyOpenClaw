"""将 OperationObservationDocument 渲染为自包含诊断工作台 HTML。"""

from __future__ import annotations

import html
import json
from typing import Any

from pickel.observe.operation_projector import OperationObservationDocument


class OperationReportRenderer:
    """自包含 HTML 报告渲染器，不直接访问数据库或 ContentStore。"""

    def render(self, doc: OperationObservationDocument | dict[str, Any]) -> str:
        data = doc.to_dict() if isinstance(doc, OperationObservationDocument) else doc
        # 版本由 Projector/HTTP 合同提供；Renderer 不篡改调用方传入的快照。
        # 这样旧快照仍可原样渲染，且客户端能准确看到生产侧声明的版本。
        # application/json script 的内容不是 HTML 文本节点：html.escape 会把
        # 引号变成实体，浏览器读取 textContent 后 JSON.parse 将直接失败。
        # 只转义可能结束 script 或触发 HTML 解析的字符，保留合法 JSON 字节。
        json_str = json.dumps(data, ensure_ascii=False, default=str)
        escaped_json = _script_safe_json(json_str)

        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
  <title>Pickel Diagnostics · Operation Trace Explorer</title>
  <style>
    :root {{
      color-scheme: light dark;
      --pk-bg: light-dark(#f4f5f7, #0f1115);
      --pk-surface: light-dark(#ffffff, #171a20);
      --pk-surface-raised: light-dark(#f8f9fb, #1d2128);
      --pk-surface-active: light-dark(#edf3ff, #17253d);
      --pk-text: light-dark(#172033, #edf1f7);
      --pk-muted: light-dark(#687286, #9ca7b9);
      --pk-border: light-dark(#dde1e8, #303642);
      --pk-border-strong: light-dark(#c7cdd8, #454d5c);
      --pk-model: light-dark(#3867d6, #7aa2ff);
      --pk-model-soft: light-dark(#dfe9ff, #20365f);
      --pk-tool: light-dark(#8b5e11, #e7b75c);
      --pk-tool-soft: light-dark(#fff0cf, #493919);
      --pk-agent: light-dark(#7052b8, #b69aea);
      --pk-agent-soft: light-dark(#eee7ff, #362755);
      --pk-storage: light-dark(#16745a, #66caae);
      --pk-storage-soft: light-dark(#dff5ed, #183d34);
      --pk-error: light-dark(#b73838, #ff8585);
      --pk-error-soft: light-dark(#fde7e7, #472323);
      --pk-success: light-dark(#147250, #68d0ad);
      --pk-shadow: light-dark(0 12px 40px rgba(25, 34, 52, .10), 0 16px 48px rgba(0, 0, 0, .32));
    }}

    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 20px;
      background: var(--pk-bg);
      color: var(--pk-text);
      font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 14px;
      line-height: 1.45;
    }}

    #pickel-trace-explorer {{
      max-width: 1480px;
      margin: 0 auto;
      width: 100%;
    }}

    #pickel-trace-explorer strong {{ font-weight: 500; }}
    #pickel-trace-explorer button,
    #pickel-trace-explorer select {{ font: inherit; color: inherit; }}
    #pickel-trace-explorer button {{ font-weight: 400; }}

    #pickel-trace-explorer .pk-window {{
      background: var(--pk-surface);
      border: 1px solid var(--pk-border);
      border-radius: 14px;
      box-shadow: var(--pk-shadow);
      overflow: hidden;
      width: 100%;
    }}

    #pickel-trace-explorer .pk-appbar {{
      align-items: center;
      background: var(--pk-surface);
      border-bottom: 1px solid var(--pk-border);
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      justify-content: space-between;
      padding: 13px 16px;
    }}

    #pickel-trace-explorer .pk-brand {{ align-items: center; display: flex; gap: 10px; min-width: 190px; }}
    #pickel-trace-explorer .pk-mark {{
      align-items: center;
      background: var(--pk-text);
      border-radius: 8px;
      color: var(--pk-surface);
      display: inline-flex;
      height: 29px;
      justify-content: center;
      width: 29px;
      font-weight: 700;
      font-size: 13px;
    }}
    #pickel-trace-explorer .pk-brand-title {{ font-weight: 500; }}
    #pickel-trace-explorer .pk-brand-subtitle {{ color: var(--pk-muted); font-size: .84em; }}
    #pickel-trace-explorer .pk-app-actions {{ align-items: center; display: flex; flex-wrap: wrap; gap: 8px; }}
    #pickel-trace-explorer .pk-field {{
      align-items: center;
      background: var(--pk-surface-raised);
      border: 1px solid var(--pk-border);
      border-radius: 7px;
      display: flex;
      gap: 6px;
      padding: 0 8px;
    }}
    #pickel-trace-explorer .pk-field span {{ color: var(--pk-muted); font-size: .8em; }}
    #pickel-trace-explorer .pk-field select {{ background: transparent; border: 0; min-height: 32px; }}
    #pickel-trace-explorer .pk-button {{
      align-items: center;
      background: var(--pk-surface-raised);
      border: 1px solid var(--pk-border);
      border-radius: 7px;
      cursor: pointer;
      display: inline-flex;
      gap: 6px;
      min-height: 34px;
      padding: 6px 10px;
    }}

    #pickel-trace-explorer .pk-summary {{
      align-items: center;
      background: var(--pk-surface-raised);
      border-bottom: 1px solid var(--pk-border);
      display: flex;
      flex-wrap: wrap;
      gap: 8px 22px;
      padding: 9px 16px;
    }}
    #pickel-trace-explorer .pk-summary-item {{ align-items: baseline; display: flex; gap: 6px; white-space: nowrap; }}
    #pickel-trace-explorer .pk-summary-label {{ color: var(--pk-muted); font-size: .8em; }}
    #pickel-trace-explorer .pk-summary-value {{ font-variant-numeric: tabular-nums; font-weight: 500; }}
    #pickel-trace-explorer .pk-status {{ color: var(--pk-success); text-transform: capitalize; }}
    #pickel-trace-explorer .pk-status.pk-failed {{ color: var(--pk-error); }}
    #pickel-trace-explorer .pk-integrity {{
      align-items: center;
      display: flex;
      gap: 7px;
      margin-left: auto;
    }}
    #pickel-trace-explorer .pk-dot {{ background: currentColor; border-radius: 50%; height: 7px; width: 7px; }}

    #pickel-trace-explorer .pk-analytics {{ background: var(--pk-surface); border-bottom: 1px solid var(--pk-border); padding: 13px 16px 15px; }}
    #pickel-trace-explorer .pk-analytics-head {{ align-items: center; display: flex; flex-wrap: wrap; gap: 8px 18px; justify-content: space-between; margin-bottom: 10px; }}
    #pickel-trace-explorer .pk-analytics-title {{ font-weight: 500; }}
    #pickel-trace-explorer .pk-analytics-note {{ color: var(--pk-muted); font-size: .78em; }}
    #pickel-trace-explorer .pk-selected-call {{ align-items: baseline; display: flex; flex-wrap: wrap; gap: 6px 13px; }}
    #pickel-trace-explorer .pk-selected-call-name {{ color: var(--pk-model); font-weight: 500; }}
    #pickel-trace-explorer .pk-selected-metric {{ color: var(--pk-muted); font-size: .8em; }}
    #pickel-trace-explorer .pk-selected-metric strong {{ color: var(--pk-text); }}
    #pickel-trace-explorer .pk-chart-grid {{ display: grid; gap: 16px; grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    #pickel-trace-explorer .pk-chart-panel {{ min-width: 0; }}
    #pickel-trace-explorer .pk-chart-head {{ align-items: baseline; display: flex; gap: 7px; justify-content: space-between; margin-bottom: 4px; }}
    #pickel-trace-explorer .pk-chart-title {{ font-size: .82em; font-weight: 500; }}
    #pickel-trace-explorer .pk-chart-unit {{ color: var(--pk-muted); font-size: .72em; }}
    #pickel-trace-explorer .pk-chart-note {{ color: var(--pk-muted); font-size: .72em; line-height: 1.35; margin-top: 5px; min-height: 2.7em; }}
    #pickel-trace-explorer .pk-chart {{ min-height: 142px; width: 100%; }}
    #pickel-trace-explorer .pk-chart svg {{ display: block; overflow: visible; width: 100%; }}
    #pickel-trace-explorer .pk-chart .pk-grid-line {{ stroke: var(--pk-border); stroke-width: 1; }}
    #pickel-trace-explorer .pk-chart .pk-axis-line {{ stroke: var(--pk-border-strong); stroke-width: 1; }}
    #pickel-trace-explorer .pk-chart .pk-axis-text {{ fill: var(--pk-muted); font-size: 11px; font-weight: 400; }}
    #pickel-trace-explorer .pk-chart .pk-selected-label {{ fill: var(--pk-text); font-weight: 500; }}
    #pickel-trace-explorer .pk-chart .pk-series-line {{ fill: none; stroke: var(--pk-model); stroke-width: 2; }}
    #pickel-trace-explorer .pk-chart .pk-cache-line {{ fill: none; stroke: var(--pk-storage); stroke-width: 2; }}
    #pickel-trace-explorer .pk-chart .pk-point {{ fill: var(--pk-surface); stroke: var(--pk-model); stroke-width: 2; cursor: pointer; }}
    #pickel-trace-explorer .pk-chart .pk-cache-point {{ fill: var(--pk-surface); stroke: var(--pk-storage); stroke-width: 2; cursor: pointer; }}
    #pickel-trace-explorer .pk-chart .pk-selected-point {{ fill: var(--pk-text) !important; stroke: var(--pk-text) !important; r: 5 !important; }}
    #pickel-trace-explorer .pk-chart .pk-value-text {{ fill: var(--pk-text); font-size: 11px; font-weight: 500; }}
    #pickel-trace-explorer .pk-chart .pk-bar-cached {{ fill: var(--pk-storage); }}
    #pickel-trace-explorer .pk-chart .pk-bar-uncached {{ fill: var(--pk-model); }}
    #pickel-trace-explorer .pk-chart .pk-bar-output {{ fill: var(--pk-tool); }}
    #pickel-trace-explorer .pk-chart .pk-failed-mark {{ fill: var(--pk-error); }}
    #pickel-trace-explorer .pk-chart-legend {{ display: flex; flex-wrap: wrap; gap: 5px 12px; margin-top: 2px; }}
    #pickel-trace-explorer .pk-chart-legend span {{ align-items: center; color: var(--pk-muted); display: flex; font-size: .72em; gap: 5px; }}
    #pickel-trace-explorer .pk-swatch {{ border-radius: 2px; height: 7px; width: 11px; display: inline-block; }}
    #pickel-trace-explorer .pk-swatch-cache {{ background: var(--pk-storage); }}
    #pickel-trace-explorer .pk-swatch-input {{ background: var(--pk-model); }}

    #pickel-trace-explorer .pk-workbench {{
      display: grid;
      grid-template-columns: minmax(210px, 240px) minmax(380px, 1fr);
      min-height: 520px;
    }}
    #pickel-trace-explorer .pk-pane {{ min-width: 0; padding: 14px; }}
    #pickel-trace-explorer .pk-pane + .pk-pane {{ border-left: 1px solid var(--pk-border); }}
    #pickel-trace-explorer .pk-pane-heading {{
      align-items: center;
      display: flex;
      gap: 8px;
      justify-content: space-between;
      margin-bottom: 12px;
    }}
    #pickel-trace-explorer .pk-pane-title {{ font-size: .86em; font-weight: 500; letter-spacing: .02em; }}
    #pickel-trace-explorer .pk-pane-note {{ color: var(--pk-muted); font-size: .78em; }}

    #pickel-trace-explorer .pk-tree {{ display: grid; gap: 3px; max-height: 480px; overflow-y: auto; }}
    #pickel-trace-explorer .pk-tree-row {{
      align-items: center;
      background: transparent;
      border: 0;
      border-radius: 7px;
      cursor: pointer;
      display: grid;
      grid-template-columns: 14px minmax(0, 1fr) auto;
      gap: 7px;
      min-height: 33px;
      padding: 4px 7px;
      text-align: left;
      width: 100%;
    }}
    #pickel-trace-explorer .pk-tree-row:hover {{ background: var(--pk-surface-raised); }}
    #pickel-trace-explorer .pk-tree-row[aria-pressed="true"] {{ background: var(--pk-surface-active); }}
    #pickel-trace-explorer .pk-tree-row[data-depth="1"] {{ padding-left: 16px; }}
    #pickel-trace-explorer .pk-tree-row[data-depth="2"] {{ padding-left: 28px; }}
    #pickel-trace-explorer .pk-tree-row[data-depth="3"] {{ padding-left: 40px; }}
    #pickel-trace-explorer .pk-tree-icon {{ display: inline-flex; font-size: 11px; width: 14px; justify-content: center; }}
    #pickel-trace-explorer .pk-tree-label {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    #pickel-trace-explorer .pk-tree-meta {{ color: var(--pk-muted); font-size: .76em; }}
    #pickel-trace-explorer .pk-model-color {{ color: var(--pk-model); }}
    #pickel-trace-explorer .pk-tool-color {{ color: var(--pk-tool); }}
    #pickel-trace-explorer .pk-agent-color {{ color: var(--pk-agent); }}
    #pickel-trace-explorer .pk-error-color {{ color: var(--pk-error); }}
    #pickel-trace-explorer .pk-storage-color {{ color: var(--pk-storage); }}

    #pickel-trace-explorer .pk-legend {{
      border-top: 1px solid var(--pk-border);
      display: grid;
      gap: 7px;
      margin-top: 14px;
      padding-top: 12px;
    }}
    #pickel-trace-explorer .pk-legend-row {{ align-items: center; color: var(--pk-muted); display: flex; font-size: .78em; gap: 7px; }}
    #pickel-trace-explorer .pk-symbol-solid {{ background: var(--pk-success); border-radius: 2px; height: 7px; width: 14px; }}
    #pickel-trace-explorer .pk-symbol-dashed {{ border-top: 2px dashed var(--pk-muted); height: 2px; width: 14px; }}

    #pickel-trace-explorer .pk-timeline-controls {{ align-items: center; display: flex; flex-wrap: wrap; gap: 10px; }}
    #pickel-trace-explorer .pk-check {{ align-items: center; color: var(--pk-muted); cursor: pointer; display: inline-flex; font-size: .82em; gap: 6px; }}
    #pickel-trace-explorer .pk-timeline {{ padding-top: 4px; max-height: 480px; overflow-y: auto; }}
    #pickel-trace-explorer .pk-axis,
    #pickel-trace-explorer .pk-lane {{
      display: grid;
      grid-template-columns: 72px minmax(0, 1fr);
    }}
    #pickel-trace-explorer .pk-axis {{ color: var(--pk-muted); font-size: .74em; margin-bottom: 4px; }}
    #pickel-trace-explorer .pk-axis-track {{ display: flex; justify-content: space-between; padding: 0 4px; }}
    #pickel-trace-explorer .pk-lane-label {{ align-items: center; color: var(--pk-muted); display: flex; font-size: .78em; min-height: 46px; padding-right: 9px; }}
    #pickel-trace-explorer .pk-track {{
      background-image: linear-gradient(to right, var(--pk-border) 1px, transparent 1px);
      background-size: 20% 100%;
      border-left: 1px solid var(--pk-border);
      min-height: 46px;
      position: relative;
    }}
    #pickel-trace-explorer .pk-lane + .pk-lane .pk-track,
    #pickel-trace-explorer .pk-lane + .pk-lane .pk-lane-label {{ border-top: 1px solid var(--pk-border); }}
    #pickel-trace-explorer .pk-bar {{
      align-items: center;
      border: 0;
      border-radius: 5px;
      cursor: pointer;
      display: flex;
      gap: 5px;
      height: 25px;
      justify-content: flex-start;
      left: var(--left);
      min-width: 24px;
      overflow: hidden;
      padding: 0 7px;
      position: absolute;
      text-overflow: ellipsis;
      top: var(--top, 10px);
      white-space: nowrap;
      width: var(--width);
    }}
    #pickel-trace-explorer .pk-bar[data-kind="model"] {{ background: var(--pk-model-soft); color: var(--pk-model); }}
    #pickel-trace-explorer .pk-bar[data-kind="tool"] {{ background: var(--pk-tool-soft); color: var(--pk-tool); }}
    #pickel-trace-explorer .pk-bar[data-kind="agent"] {{ background: var(--pk-agent-soft); color: var(--pk-agent); }}
    #pickel-trace-explorer .pk-bar[data-kind="storage"] {{ background: var(--pk-storage-soft); color: var(--pk-storage); }}
    #pickel-trace-explorer .pk-bar[data-status="error"] {{ background: var(--pk-error-soft); color: var(--pk-error); }}
    #pickel-trace-explorer .pk-bar[aria-pressed="true"] {{ box-shadow: inset 0 0 0 2px currentColor; }}
    #pickel-trace-explorer .pk-bar.pk-trace {{ border: 1px dashed currentColor; opacity: .82; }}
    #pickel-trace-explorer .pk-bar.pk-dimmed {{ opacity: .14; }}
    #pickel-trace-explorer .pk-duration {{ color: var(--pk-muted); font-size: .76em; margin-left: auto; }}
    #pickel-trace-explorer .pk-critical {{
      align-items: center;
      color: var(--pk-error);
      display: flex;
      font-size: .78em;
      gap: 5px;
      margin: 11px 0 0 72px;
    }}

    #pickel-trace-explorer .pk-evidence {{ background: var(--pk-surface); border-top: 1px solid var(--pk-border); }}
    #pickel-trace-explorer .pk-selected-detail {{ border-bottom: 1px solid var(--pk-border); padding: 13px 16px 11px; }}
    #pickel-trace-explorer .pk-selected-detail-title {{ font-weight: 500; }}
    #pickel-trace-explorer .pk-selected-detail-subtitle {{ color: var(--pk-muted); font-size: .8em; margin-top: 2px; }}
    #pickel-trace-explorer .pk-selected-detail-grid {{ display: grid; gap: 7px 18px; grid-template-columns: repeat(4, minmax(0, 1fr)); margin: 10px 0 0; }}
    #pickel-trace-explorer .pk-selected-detail-grid .pk-kv-row {{ min-width: 0; }}
    #pickel-trace-explorer .pk-selected-detail-grid dt {{ color: var(--pk-muted); font-size: .76em; }}
    #pickel-trace-explorer .pk-selected-detail-grid dd {{ margin: 1px 0 0; overflow-wrap: anywhere; }}
    #pickel-trace-explorer .pk-evidence-head {{
      align-items: center;
      border-bottom: 1px solid var(--pk-border);
      display: flex;
      flex-wrap: wrap;
      gap: 10px 18px;
      justify-content: space-between;
      padding: 13px 16px;
    }}
    #pickel-trace-explorer .pk-selected-facts {{ align-items: center; background: var(--pk-surface-raised); border-bottom: 1px solid var(--pk-border); display: flex; flex-wrap: wrap; gap: 7px 18px; padding: 9px 16px; }}
    #pickel-trace-explorer .pk-selected-fact {{ align-items: baseline; display: flex; gap: 6px; }}
    #pickel-trace-explorer .pk-selected-fact-label {{ color: var(--pk-muted); font-size: .76em; }}
    #pickel-trace-explorer .pk-selected-fact-value {{ font-size: .82em; font-variant-numeric: tabular-nums; font-weight: 500; }}
    #pickel-trace-explorer .pk-evidence-title {{ font-weight: 500; }}
    #pickel-trace-explorer .pk-evidence-subtitle {{ color: var(--pk-muted); font-size: .78em; margin-top: 2px; }}
    #pickel-trace-explorer .pk-proof-chain {{ align-items: center; display: flex; flex-wrap: wrap; gap: 6px; }}
    #pickel-trace-explorer .pk-proof-item {{ color: var(--pk-muted); font-size: .75em; }}
    #pickel-trace-explorer .pk-proof-arrow {{ color: var(--pk-border-strong); }}
    #pickel-trace-explorer .pk-evidence-tabs {{ display: flex; flex-wrap: wrap; gap: 4px; padding: 9px 16px 0; }}
    #pickel-trace-explorer .pk-evidence-tab {{
      background: transparent;
      border: 1px solid transparent;
      border-radius: 6px;
      cursor: pointer;
      padding: 6px 9px;
    }}
    #pickel-trace-explorer .pk-evidence-tab[aria-selected="true"] {{ background: var(--pk-surface-active); border-color: var(--pk-border); font-weight: 500; }}
    #pickel-trace-explorer .pk-document {{
      display: grid;
      grid-template-columns: minmax(180px, 230px) minmax(0, 1fr);
      padding: 12px 16px 16px;
    }}
    #pickel-trace-explorer .pk-document-nav {{ border-right: 1px solid var(--pk-border); padding-right: 12px; }}
    #pickel-trace-explorer .pk-document-label {{ color: var(--pk-muted); font-size: .75em; margin-bottom: 7px; }}
    #pickel-trace-explorer .pk-document-item {{
      align-items: center;
      background: transparent;
      border: 0;
      border-radius: 6px;
      cursor: pointer;
      display: flex;
      gap: 7px;
      justify-content: space-between;
      min-height: 32px;
      padding: 5px 7px;
      text-align: left;
      width: 100%;
    }}
    #pickel-trace-explorer .pk-document-item[aria-pressed="true"] {{ background: var(--pk-surface-active); }}
    #pickel-trace-explorer .pk-document-count {{ color: var(--pk-muted); font-size: .76em; }}
    #pickel-trace-explorer .pk-document-body {{ min-width: 0; padding-left: 14px; }}
    #pickel-trace-explorer .pk-document-meta {{ align-items: center; display: flex; flex-wrap: wrap; gap: 6px 12px; justify-content: space-between; margin-bottom: 8px; }}
    #pickel-trace-explorer .pk-document-path {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .8em; }}
    #pickel-trace-explorer .pk-document-completeness {{ color: var(--pk-success); font-size: .76em; }}
    #pickel-trace-explorer .pk-document-code {{
      background: var(--pk-surface-raised);
      border: 1px solid var(--pk-border);
      border-radius: 7px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: .78em;
      margin: 0;
      min-height: 220px;
      max-height: 480px;
      overflow-wrap: anywhere;
      padding: 12px;
      white-space: pre-wrap;
      overflow-y: auto;
    }}

    @media (max-width: 980px) {{
      #pickel-trace-explorer .pk-chart-grid {{ grid-template-columns: 1fr; }}
      #pickel-trace-explorer .pk-workbench {{ grid-template-columns: 1fr; }}
      #pickel-trace-explorer .pk-pane + .pk-pane {{ border-left: 0; border-top: 1px solid var(--pk-border); }}
      #pickel-trace-explorer .pk-document {{ grid-template-columns: 1fr; }}
      #pickel-trace-explorer .pk-document-nav {{ border-right: 0; border-bottom: 1px solid var(--pk-border); padding-right: 0; padding-bottom: 10px; }}
      #pickel-trace-explorer .pk-document-body {{ padding-left: 0; padding-top: 10px; }}
      #pickel-trace-explorer .pk-selected-detail-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
  </style>
</head>
<body>
<div id="pickel-trace-explorer">
  <section class="pk-window" aria-label="Pickel Operation Trace Explorer">
    <header class="pk-appbar">
      <div class="pk-brand">
        <span class="pk-mark">PK</span>
        <span>
          <span class="pk-brand-title">Pickel Diagnostics</span><br>
          <span class="pk-brand-subtitle">Operation Trace Explorer</span>
        </span>
      </div>
      <div class="pk-app-actions">
        <label class="pk-field"><span>Session</span><select id="pk-session-select" aria-label="Session"><option>{html.escape(data.get('session', {}).get('session_id', '—'))}</option></select></label>
        <label class="pk-field"><span>Operation</span><select id="pk-op-select" aria-label="Operation"><option>{html.escape(data.get('operation', {}).get('operation_id', '—')[:12])}</option></select></label>
        <button class="pk-button" id="pk-export-raw-btn" type="button">导出证据 JSON</button>
      </div>
    </header>

    <div class="pk-summary" aria-label="Operation 摘要">
      <span class="pk-summary-item"><span class="pk-summary-label">结果</span><span class="pk-summary-value pk-status" id="pk-sum-status">{html.escape(data.get('summary', {}).get('status', '—'))}</span></span>
      <span class="pk-summary-item"><span class="pk-summary-label">耗时</span><span class="pk-summary-value" id="pk-sum-duration">{html.escape(data.get('summary', {}).get('duration_text', '—'))}</span></span>
      <span class="pk-summary-item"><span class="pk-summary-label">模型请求</span><span class="pk-summary-value" id="pk-sum-calls">{data.get('summary', {}).get('model_calls_count', 0)} · {data.get('summary', {}).get('model_retries_count', 0)} retry</span></span>
      <span class="pk-summary-item"><span class="pk-summary-label">工具调用</span><span class="pk-summary-value" id="pk-sum-tools">{data.get('summary', {}).get('tool_calls_count', 0)}</span></span>
      <span class="pk-summary-item"><span class="pk-summary-label">Child</span><span class="pk-summary-value" id="pk-sum-children">{data.get('summary', {}).get('children_count', 0)}</span></span>
      <span class="pk-integrity"><span class="pk-dot pk-storage-color"></span><span class="pk-summary-label" id="pk-sum-integrity">{html.escape(data.get('trace_integrity', '可靠事实完整'))}</span></span>
    </div>

    <!-- 第一层：ModelCall 趋势分析 -->
    <section class="pk-analytics" aria-label="ModelCall 请求趋势">
      <div class="pk-analytics-head">
        <div><span class="pk-analytics-title">ModelCall 请求趋势</span> <span class="pk-analytics-note">每个点对应一次真实 Provider 调用；重试独立计数</span></div>
        <div class="pk-selected-call" aria-live="polite">
          <span class="pk-selected-call-name" id="pk-usage-call">—</span>
          <span class="pk-selected-metric">Latency <strong id="pk-selected-latency">—</strong></span>
          <span class="pk-selected-metric">Cache hit <strong id="pk-cache-rate">—</strong></span>
          <span class="pk-selected-metric">Token <strong id="pk-total-tokens">—</strong></span>
        </div>
      </div>
      <div class="pk-chart-grid">
        <div class="pk-chart-panel">
          <div class="pk-chart-head"><span class="pk-chart-title">请求耗时</span><span class="pk-chart-unit">seconds · lower is better</span></div>
          <div class="pk-chart" id="pk-latency-chart"></div>
        </div>
        <div class="pk-chart-panel">
          <div class="pk-chart-head"><span class="pk-chart-title">Cache 命中率</span><span class="pk-chart-unit">provider-defined / unknown</span></div>
          <div class="pk-chart" id="pk-cache-chart"></div>
          <div class="pk-chart-note" id="pk-cache-semantics" aria-live="polite">选择 ModelCall 查看缓存口径</div>
        </div>
        <div class="pk-chart-panel">
          <div class="pk-chart-head"><span class="pk-chart-title">Token 构成</span><span class="pk-chart-unit">provider reported</span></div>
          <div class="pk-chart" id="pk-token-chart"></div>
          <div class="pk-chart-legend">
            <span><i class="pk-swatch pk-swatch-cache"></i>Cache read</span>
            <span><i class="pk-swatch pk-swatch-input"></i>Uncached input</span>
            <span><i class="pk-swatch" style="background:var(--pk-tool)"></i>Output</span>
          </div>
        </div>
      </div>
    </section>

    <!-- 第二层：执行结构与统一时间线 -->
    <div class="pk-workbench">
      <aside class="pk-pane pk-tree-pane" aria-label="执行结构">
        <div class="pk-pane-heading"><span class="pk-pane-title">执行结构</span><span class="pk-pane-note">树状层级</span></div>
        <div class="pk-tree" id="pk-tree"></div>
        <div class="pk-legend" aria-label="数据可靠性图例">
          <div class="pk-legend-row"><span class="pk-symbol-solid"></span>可靠事实：事务持久化</div>
          <div class="pk-legend-row"><span class="pk-symbol-dashed"></span>Trace：允许采样或丢失</div>
        </div>
      </aside>

      <main class="pk-pane pk-timeline-pane" aria-label="统一时间线">
        <div class="pk-pane-heading">
          <div><div class="pk-pane-title">统一时间线</div><div class="pk-pane-note" id="pk-timeline-range">Operation 时间跨度</div></div>
          <div class="pk-timeline-controls">
            <label class="pk-check"><input id="pk-errors-only" type="checkbox"> 聚焦异常路径</label>
          </div>
        </div>
        <div class="pk-timeline" id="pk-timeline" role="group" aria-label="按资源泳道排列的执行时间线"></div>
        <div class="pk-critical" id="pk-critical-path">关键路径</div>
      </main>

    </div>

    <!-- 第三层：事实详情与证据链 -->
    <section class="pk-evidence" aria-label="ModelCall 完整事实内容">
      <div class="pk-evidence-head">
        <div><div class="pk-evidence-title" id="pk-evidence-title">ModelCall · 完整内容</div><div class="pk-evidence-subtitle">RequestContent / ResponseContent 均来自不可变内容存储</div></div>
        <div class="pk-proof-chain" aria-label="内容证据链">
          <span class="pk-proof-item" id="pk-model-call-id">—</span><span class="pk-proof-arrow">→</span>
          <span class="pk-proof-item" id="pk-content-ref">—</span><span class="pk-proof-arrow">→</span>
          <span class="pk-proof-item" id="pk-content-schema">schema —</span><span class="pk-proof-arrow">→</span>
          <span class="pk-proof-item" id="pk-content-integrity">完整性待验证</span>
        </div>
      </div>
      <div class="pk-selected-detail" id="pk-selected-detail" aria-live="polite">
        <div class="pk-selected-detail-title" id="pk-selected-detail-title">—</div>
        <div class="pk-selected-detail-subtitle" id="pk-selected-detail-subtitle">点击执行树、时间线或图表查看事实</div>
        <dl class="pk-selected-detail-grid" id="pk-selected-detail-grid"></dl>
      </div>
      <div class="pk-selected-facts" aria-live="polite">
        <span class="pk-selected-fact"><span class="pk-selected-fact-label">Status</span><span class="pk-selected-fact-value" id="pk-fact-status">—</span></span>
        <span class="pk-selected-fact"><span class="pk-selected-fact-label">Attempt</span><span class="pk-selected-fact-value" id="pk-fact-attempt">—</span></span>
        <span class="pk-selected-fact"><span class="pk-selected-fact-label">Input</span><span class="pk-selected-fact-value" id="pk-input-total">—</span></span>
        <span class="pk-selected-fact"><span class="pk-selected-fact-label">Cache read</span><span class="pk-selected-fact-value" id="pk-cache-read">—</span></span>
        <span class="pk-selected-fact"><span class="pk-selected-fact-label">Output</span><span class="pk-selected-fact-value" id="pk-output-tokens">—</span></span>
        <span class="pk-selected-fact"><span class="pk-selected-fact-label">TTFT</span><span class="pk-selected-fact-value" id="pk-usage-ttft">—</span></span>
        <span class="pk-selected-fact"><span class="pk-selected-fact-label">Finish</span><span class="pk-selected-fact-value" id="pk-fact-finish">—</span></span>
      </div>
      <div class="pk-evidence-tabs" role="tablist" aria-label="ModelCall 内容层级">
        <button class="pk-evidence-tab" type="button" role="tab" data-evidence-tab="context" aria-selected="true">ModelContext</button>
        <button class="pk-evidence-tab" type="button" role="tab" data-evidence-tab="wire" aria-selected="false">Wire Request</button>
        <button class="pk-evidence-tab" type="button" role="tab" data-evidence-tab="provider" aria-selected="false">Provider Response</button>
        <button class="pk-evidence-tab" type="button" role="tab" data-evidence-tab="assistant" aria-selected="false">AssistantMessage</button>
      </div>
      <div class="pk-document">
        <nav class="pk-document-nav" aria-label="内容目录"></nav>
        <div class="pk-document-body">
          <div class="pk-document-meta"><span class="pk-document-path" id="pk-document-path">—</span><span class="pk-document-completeness" id="pk-document-completeness">完整内容 · 未截断</span></div>
          <pre class="pk-document-code" id="pk-document-code" aria-live="polite"><code></code></pre>
        </div>
      </div>
    </section>
  </section>

  <!-- 原始数据嵌入 -->
  <script id="pk-observation-data" type="application/json">
{escaped_json}
  </script>

  <script>
    (() => {{
      const root = document.getElementById('pickel-trace-explorer');
      const rawDataEl = document.getElementById('pk-observation-data');
      if (!rawDataEl) return;
      let DATA = JSON.parse(rawDataEl.textContent || '{{}}');
      let modelCalls = DATA.model_calls || [];
      let chartsData = DATA.charts || {{}};
      let executionNodes = DATA.execution_nodes || [];
      let timelineData = DATA.timeline || {{}};
      let docEvidence = DATA.document_evidence || {{}};
      const API_BASE = DATA.api_base || '';

      let selectedKey = modelCalls.length > 0 ? modelCalls[0].key : 'operation';
      let selectedModelCallKey = modelCalls.length > 0 ? modelCalls[0].key : null;
      let activeEvidenceTab = 'context';
      let activeDocSection = 'messages';
      let evidenceLoadingKey = null;
      const evidenceErrors = {{}};

      // DOM 引用
      const evidenceTabs = [...root.querySelectorAll('.pk-evidence-tab')];
      const evidenceTitle = root.querySelector('#pk-evidence-title');
      const modelCallIdEl = root.querySelector('#pk-model-call-id');
      const contentRefEl = root.querySelector('#pk-content-ref');
      const contentSchemaEl = root.querySelector('#pk-content-schema');
      const contentIntegrityEl = root.querySelector('#pk-content-integrity');
      const selectedDetailTitle = root.querySelector('#pk-selected-detail-title');
      const selectedDetailSubtitle = root.querySelector('#pk-selected-detail-subtitle');
      const selectedDetailGrid = root.querySelector('#pk-selected-detail-grid');
      const documentNav = root.querySelector('.pk-document-nav');
      const documentPath = root.querySelector('#pk-document-path');
      const documentCompleteness = root.querySelector('#pk-document-completeness');
      const documentCode = root.querySelector('#pk-document-code code');
      const treeContainer = root.querySelector('#pk-tree');
      const timelineContainer = root.querySelector('#pk-timeline');
      const criticalPathEl = root.querySelector('#pk-critical-path');
      const timelineRangeEl = root.querySelector('#pk-timeline-range');
      const cacheSemanticsEl = root.querySelector('#pk-cache-semantics');
      const sessionSelect = root.querySelector('#pk-session-select');
      const operationSelect = root.querySelector('#pk-op-select');
      const summaryStatusEl = root.querySelector('#pk-sum-status');
      const summaryDurationEl = root.querySelector('#pk-sum-duration');
      const summaryCallsEl = root.querySelector('#pk-sum-calls');
      const summaryToolsEl = root.querySelector('#pk-sum-tools');
      const summaryChildrenEl = root.querySelector('#pk-sum-children');
      const summaryIntegrityEl = root.querySelector('#pk-sum-integrity');

      function renderSummary() {{
        const summary = DATA.summary || {{}};
        summaryStatusEl.textContent = summary.status || '—';
        summaryDurationEl.textContent = summary.duration_text || '—';
        summaryCallsEl.textContent = `${{summary.model_calls_count ?? 0}} · ${{summary.model_retries_count ?? 0}} retry`;
        summaryToolsEl.textContent = summary.tool_calls_count ?? 0;
        summaryChildrenEl.textContent = summary.children_count ?? 0;
        summaryIntegrityEl.textContent = DATA.trace_integrity || summary.trace_integrity || '可靠事实完整';
      }}

      function applyData(next) {{
        DATA = next || {{}};
        modelCalls = DATA.model_calls || [];
        chartsData = DATA.charts || {{}};
        executionNodes = DATA.execution_nodes || [];
        timelineData = DATA.timeline || {{}};
        docEvidence = DATA.document_evidence || {{}};
        Object.keys(evidenceErrors).forEach(key => delete evidenceErrors[key]);
        selectedKey = modelCalls.length > 0 ? modelCalls[0].key : 'operation';
        selectedModelCallKey = modelCalls.length > 0 ? modelCalls[0].key : null;
        renderSummary();
        renderTree();
        renderTimeline();
        renderCharts();
        renderSelectedModelCallHeader();
        renderSelectedDetail();
        renderEvidence();
      }}

      function fetchJson(path) {{
        return fetch(`${{API_BASE}}${{path}}`, {{ headers: {{ 'Accept': 'application/json' }} }})
          .then(response => response.ok ? response.json() : response.json().then(body => Promise.reject(new Error(body.error?.message || `HTTP ${{response.status}}`))));
      }}

      function renderOperationOptions(operations) {{
        if (!operationSelect || !Array.isArray(operations)) return;
        operationSelect.innerHTML = operations.map(item => `<option value="${{escapeHtml(item.operation_id)}}">${{escapeHtml(item.operation_id.slice(0, 12))}} · ${{escapeHtml(item.status || 'unknown')}}</option>`).join('');
        if (DATA.operation?.operation_id) operationSelect.value = DATA.operation.operation_id;
      }}

      async function loadOperation(operationId) {{
        if (!API_BASE || !operationId) return;
        try {{
          const next = await fetchJson(`/api/v1/operations/${{encodeURIComponent(operationId)}}`);
          next.available_operations = DATA.available_operations;
          next.api_base = API_BASE;
          applyData(next);
          if (operationSelect) operationSelect.value = operationId;
        }} catch (error) {{
          selectedDetailTitle.textContent = '无法加载 Operation';
          selectedDetailSubtitle.textContent = error.message || String(error);
        }}
      }}

      if (operationSelect) operationSelect.addEventListener('change', () => loadOperation(operationSelect.value));
      if (sessionSelect && API_BASE && DATA.session?.session_id) sessionSelect.addEventListener('change', () => {{
        fetchJson(`/api/v1/sessions/${{encodeURIComponent(sessionSelect.value)}}`).then(index => {{
          DATA.available_operations = index.operations || [];
          renderOperationOptions(DATA.available_operations);
          const active = index.session?.active_operation_id || DATA.available_operations.at(-1)?.operation_id;
          if (active) loadOperation(active);
        }}).catch(error => {{ selectedDetailSubtitle.textContent = error.message || String(error); }});
      }});

      // 导出原始 JSON 按钮
      const exportBtn = root.querySelector('#pk-export-raw-btn');
      if (exportBtn) {{
        exportBtn.addEventListener('click', () => {{
          const blob = new Blob([JSON.stringify(DATA, null, 2)], {{ type: 'application/json' }});
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          a.download = `observation_${{DATA.operation?.operation_id || 'evidence'}}.json`;
          a.click();
          URL.revokeObjectURL(url);
        }});
      }}

      function escapeHtml(value) {{
        return String(value == null ? '' : value).replace(/[&<>"']/g, char => ({{
          '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
        }}[char]));
      }}

      function formatNumber(value) {{
        return value == null ? '—' : Number(value).toLocaleString('en-US');
      }}

      function renderTree() {{
        treeContainer.innerHTML = executionNodes.map(node => {{
          const isPressed = node.key === selectedKey;
          const colorClass = node.kind === 'model' ? (node.status === 'error' ? 'pk-error-color' : 'pk-model-color') : (node.kind === 'tool' ? 'pk-tool-color' : (node.kind === 'agent' || node.kind === 'child' ? 'pk-agent-color' : ''));
          const icon = node.kind === 'operation' ? '⚙' : (node.kind === 'step' ? '↳' : (node.kind === 'model' ? (node.status === 'error' ? '✕' : '✦') : (node.kind === 'tool' ? '>' : '⑂')));
          return `<button class="pk-tree-row ${{colorClass}}" type="button" data-select="${{escapeHtml(node.key)}}" data-depth="${{node.depth}}" aria-pressed="${{String(isPressed)}}"><span class="pk-tree-icon">${{icon}}</span><span class="pk-tree-label">${{escapeHtml(node.label)}}</span><span class="pk-tree-meta">${{escapeHtml(node.meta)}}</span></button>`;
        }}).join('');

        treeContainer.querySelectorAll('[data-select]').forEach(btn => {{
          btn.addEventListener('click', () => {{
            selectItem(btn.dataset.select);
          }});
        }});
      }}

      function renderTimeline() {{
        timelineRangeEl.textContent = `${{timelineData.start_time_iso || ''}} — ${{timelineData.end_time_iso || ''}} (${{Math.round(timelineData.total_duration_ms || 0)}}ms)`;
        criticalPathEl.textContent = timelineData.critical_path_text || '';

        const ticksHtml = (timelineData.axis_ticks || []).map(t => `<span>${{escapeHtml(t)}}</span>`).join('');
        const axisHtml = `<div class="pk-axis"><span></span><div class="pk-axis-track">${{ticksHtml}}</div></div>`;

        const lanesHtml = (timelineData.lanes || []).map(lane => {{
          const barsHtml = lane.bars.map(bar => {{
            const isPressed = bar.key === selectedKey;
            const traceClass = bar.is_trace ? ' pk-trace' : '';
            return `<button class="pk-bar${{traceClass}}" data-select="${{escapeHtml(bar.key)}}" data-kind="${{escapeHtml(bar.kind)}}" data-status="${{escapeHtml(bar.status)}}" type="button" aria-pressed="${{String(isPressed)}}" style="--left:${{bar.left_pct}}%;--width:${{bar.width_pct}}%;"><span>${{escapeHtml(bar.label)}}</span><span class="pk-duration">${{escapeHtml(bar.duration_text)}}</span></button>`;
          }}).join('');
          return `<div class="pk-lane"><div class="pk-lane-label">${{escapeHtml(lane.name)}}</div><div class="pk-track">${{barsHtml}}</div></div>`;
        }}).join('');

        timelineContainer.innerHTML = axisHtml + lanesHtml;

        timelineContainer.querySelectorAll('[data-select]').forEach(btn => {{
          btn.addEventListener('click', () => {{
            selectItem(btn.dataset.select);
          }});
        }});
      }}

      function renderLineChart(container, series, maxVal, unitFormatter, lineClass) {{
        if (!container) return;
        const width = Math.max(260, Math.round(container.getBoundingClientRect().width || 280));
        const height = 142;
        const margin = {{ top: 20, right: 14, bottom: 28, left: 36 }};
        const plotWidth = width - margin.left - margin.right;
        const plotHeight = height - margin.top - margin.bottom;

        if (!series || series.length === 0) {{
          container.innerHTML = '<div style="color:var(--pk-muted);padding:40px;text-align:center;">暂无图表数据</div>';
          return;
        }}

        const maximum = Math.max(1.0, maxVal || 10.0);
        const x = index => series.length <= 1 ? (margin.left + plotWidth / 2) : (margin.left + (plotWidth * index) / (series.length - 1));
        const y = value => margin.top + plotHeight - (Math.min(maximum, Math.max(0, value || 0)) / maximum) * plotHeight;

        const validPoints = series.map((pt, index) => ({{ pt, index, x: x(index), y: y(pt.value) }})).filter(p => p.pt.value != null);
        const path = validPoints.map((p, i) => `${{i === 0 ? 'M' : 'L'}}${{p.x.toFixed(1)}} ${{p.y.toFixed(1)}}`).join(' ');

        const grid = [0, 0.5, 1].map(r => {{
          const lineY = margin.top + plotHeight * (1 - r);
          return `<line class="pk-grid-line" x1="${{margin.left}}" y1="${{lineY}}" x2="${{width - margin.right}}" y2="${{lineY}}"></line><text class="pk-axis-text" x="${{margin.left - 6}}" y="${{lineY + 4}}" text-anchor="end">${{unitFormatter(maximum * r)}}</text>`;
        }}).join('');

        const labels = series.map((pt, index) => `<text class="pk-axis-text" x="${{x(index)}}" y="${{height - 8}}" text-anchor="middle">${{escapeHtml(pt.label)}}</text>`).join('');

        const points = series.map((pt, index) => {{
          const isSelected = pt.key === selectedModelCallKey;
          const cx = x(index);
          const cy = pt.value != null ? y(pt.value) : (margin.top + plotHeight);
          const selClass = isSelected ? ' pk-selected-point' : '';

          if (pt.status === 'failed' && pt.value == null) {{
            return `<circle class="pk-failed-mark" cx="${{cx}}" cy="${{cy - 4}}" r="4"></circle><text class="pk-value-text pk-error-color" x="${{cx}}" y="${{cy - 12}}" text-anchor="middle">503</text>`;
          }}

          const valText = pt.value != null ? unitFormatter(pt.value) : '—';
          return `<circle class="pk-point ${{lineClass}}${{selClass}}" data-call-key="${{escapeHtml(pt.key)}}" cx="${{cx}}" cy="${{cy}}" r="4"></circle><text class="pk-value-text" x="${{cx}}" y="${{cy - 10}}" text-anchor="middle">${{valText}}</text>`;
        }}).join('');

        container.innerHTML = `<svg viewBox="0 0 ${{width}} ${{height}}" role="img"><line class="pk-axis-line" x1="${{margin.left}}" y1="${{margin.top + plotHeight}}" x2="${{width - margin.right}}" y2="${{margin.top + plotHeight}}"></line>${{grid}}<path class="pk-series-line ${{lineClass}}" d="${{path}}"></path>${{points}}${{labels}}</svg>`;

        container.querySelectorAll('[data-call-key]').forEach(el => {{
          el.addEventListener('click', () => {{
            selectItem(el.dataset.callKey);
          }});
        }});
      }}

      function renderTokenChart(container, series) {{
        if (!container) return;
        const width = Math.max(260, Math.round(container.getBoundingClientRect().width || 280));
        const height = 142;
        const margin = {{ top: 20, right: 14, bottom: 28, left: 42 }};
        const plotWidth = width - margin.left - margin.right;
        const plotHeight = height - margin.top - margin.bottom;

        if (!series || series.length === 0) {{
          container.innerHTML = '<div style="color:var(--pk-muted);padding:40px;text-align:center;">暂无 Token 数据</div>';
          return;
        }}

        const maxTotal = Math.max(1000, ...series.map(s => s.total || 0));
        const barWidth = Math.min(32, Math.max(12, plotWidth / (series.length * 2.5)));
        const y = value => margin.top + plotHeight - (Math.min(maxTotal, Math.max(0, value || 0)) / maxTotal) * plotHeight;

        const grid = [0, 0.5, 1].map(r => {{
          const val = maxTotal * r;
          const lineY = margin.top + plotHeight * (1 - r);
          const label = val >= 1000 ? `${{(val / 1000).toFixed(0)}}k` : Math.round(val);
          return `<line class="pk-grid-line" x1="${{margin.left}}" y1="${{lineY}}" x2="${{width - margin.right}}" y2="${{lineY}}"></line><text class="pk-axis-text" x="${{margin.left - 6}}" y="${{lineY + 4}}" text-anchor="end">${{label}}</text>`;
        }}).join('');

        const bars = series.map((s, index) => {{
          const center = series.length <= 1 ? (margin.left + plotWidth / 2) : (margin.left + (plotWidth * (index + 0.5)) / series.length);
          const isSelected = s.key === selectedModelCallKey;
          const selClass = isSelected ? ' pk-selected-label' : '';

          if (s.input == null || s.status === 'failed') {{
            return `<circle class="pk-failed-mark" cx="${{center}}" cy="${{margin.top + plotHeight - 4}}" r="4"></circle><text class="pk-value-text pk-error-color" x="${{center}}" y="${{margin.top + plotHeight - 12}}" text-anchor="middle">${{escapeHtml(s.error || 'Failed')}}</text><text class="pk-axis-text${{selClass}}" x="${{center}}" y="${{height - 8}}" text-anchor="middle">${{escapeHtml(s.label)}}</text>`;
          }}

          const cached = s.cached || 0;
          const uncached = s.uncached || 0;
          const output = s.output || 0;
          const baseY = y(0);

          const uncachedH = (plotHeight * uncached) / maxTotal;
          const cachedH = (plotHeight * cached) / maxTotal;
          const outputH = (plotHeight * output) / maxTotal;

          const totalLabel = s.total >= 1000 ? `${{(s.total / 1000).toFixed(1)}}k` : s.total;

          return `<g style="cursor:pointer;" data-call-key="${{escapeHtml(s.key)}}">
            <rect class="pk-bar-uncached" x="${{center - barWidth / 2}}" y="${{baseY - uncachedH}}" width="${{barWidth}}" height="${{uncachedH}}"></rect>
            <rect class="pk-bar-cached" x="${{center - barWidth / 2}}" y="${{baseY - uncachedH - cachedH}}" width="${{barWidth}}" height="${{cachedH}}"></rect>
            <rect class="pk-bar-output" x="${{center - barWidth / 2}}" y="${{baseY - uncachedH - cachedH - outputH}}" width="${{barWidth}}" height="${{Math.max(2, outputH)}}"></rect>
            <text class="pk-value-text" x="${{center}}" y="${{baseY - uncachedH - cachedH - outputH - 6}}" text-anchor="middle">${{totalLabel}}</text>
            <text class="pk-axis-text${{selClass}}" x="${{center}}" y="${{height - 8}}" text-anchor="middle">${{escapeHtml(s.label)}}</text>
          </g>`;
        }}).join('');

        container.innerHTML = `<svg viewBox="0 0 ${{width}} ${{height}}" role="img">${{grid}}<line class="pk-axis-line" x1="${{margin.left}}" y1="${{y(0)}}" x2="${{width - margin.right}}" y2="${{y(0)}}"></line>${{bars}}</svg>`;

        container.querySelectorAll('[data-call-key]').forEach(el => {{
          el.addEventListener('click', () => {{
            selectItem(el.dataset.callKey);
          }});
        }});
      }}

      function renderCharts() {{
        const latSeries = chartsData.latency || [];
        const maxLat = Math.max(2.0, ...latSeries.map(s => s.value || 0)) * 1.25;
        renderLineChart(root.querySelector('#pk-latency-chart'), latSeries, maxLat, v => `${{v.toFixed(1)}}s`, '');

        const cacheSeries = chartsData.cache || [];
        renderLineChart(root.querySelector('#pk-cache-chart'), cacheSeries, 100, v => `${{Math.round(v)}}%`, 'pk-cache-line pk-cache-point');

        const tokenSeries = chartsData.tokens || [];
        renderTokenChart(root.querySelector('#pk-token-chart'), tokenSeries);
      }}

      function renderSelectedModelCallHeader() {{
        const callItem = modelCalls.find(c => c.key === selectedModelCallKey);
        if (!callItem) {{
          root.querySelector('#pk-usage-call').textContent = '—';
          root.querySelector('#pk-selected-latency').textContent = '—';
          root.querySelector('#pk-cache-rate').textContent = '—';
          root.querySelector('#pk-total-tokens').textContent = '—';
          ['#pk-fact-status', '#pk-fact-attempt', '#pk-input-total', '#pk-cache-read', '#pk-output-tokens', '#pk-usage-ttft', '#pk-fact-finish'].forEach(selector => root.querySelector(selector).textContent = '—');
          if (cacheSemanticsEl) cacheSemanticsEl.textContent = '口径未知';
          return;
        }}

        root.querySelector('#pk-usage-call').textContent = `ModelCall ${{callItem.key.removeprefix ? callItem.key.removeprefix('call') : callItem.key.replace('call', '')}}`;
        root.querySelector('#pk-selected-latency').textContent = callItem.timing.latency_ms != null ? `${{(callItem.timing.latency_ms / 1000).toFixed(1)}}s` : '—';
        root.querySelector('#pk-cache-rate').textContent = callItem.usage.cache_hit_rate != null ? `${{callItem.usage.cache_hit_rate.toFixed(1)}}%` : '—';
        root.querySelector('#pk-total-tokens').textContent = formatNumber(callItem.usage.total_tokens);

        const cachePoint = (chartsData.cache || []).find(point => point.key === selectedModelCallKey);
        const semanticValue = value => value == null || value === '' ? '口径未知' : (typeof value === 'string' ? value : JSON.stringify(value));
        const formula = semanticValue(cachePoint?.formula);
        const denominator = semanticValue(cachePoint?.denominator);
        const source = semanticValue(cachePoint?.source);
        if (cacheSemanticsEl) cacheSemanticsEl.textContent = `公式：${{formula}} · 分母：${{denominator}} · 来源：${{source}}`;

        // 证据栏 selected facts
        root.querySelector('#pk-fact-status').textContent = callItem.status;
        root.querySelector('#pk-fact-attempt').textContent = String(callItem.attempt);
        root.querySelector('#pk-input-total').textContent = formatNumber(callItem.usage.input_tokens);
        root.querySelector('#pk-cache-read').textContent = formatNumber(callItem.usage.cache_read_tokens);
        root.querySelector('#pk-output-tokens').textContent = formatNumber(callItem.usage.output_tokens);
        root.querySelector('#pk-usage-ttft').textContent = callItem.timing.ttft_ms != null ? `${{Math.round(callItem.timing.ttft_ms)}}ms` : '—';
        root.querySelector('#pk-fact-finish').textContent = callItem.finish_reason || '—';
      }}

      function renderSelectedDetail() {{
        const callItem = modelCalls.find(c => c.key === selectedKey);
        const node = executionNodes.find(item => item.key === selectedKey);
        if (callItem) {{
          selectedDetailTitle.textContent = `ModelCall ${{callItem.model_call_id || callItem.key}}`;
          selectedDetailSubtitle.textContent = `${{callItem.provider || 'Provider'}} · ${{callItem.requested_model || 'model'}} · attempt #${{callItem.attempt ?? '—'}}`;
          const rows = [
            ['status', callItem.status], ['provider', callItem.provider],
            ['model', callItem.requested_model], ['api_kind', callItem.api_kind],
            ['endpoint', callItem.endpoint], ['operation_id', callItem.operation_id],
            ['step_id', callItem.step_id], ['request_content', callItem.request_content_ok ? '已保存' : (callItem.request_content_error || '缺失')],
            ['response_content', callItem.response_content_ok ? '已保存' : (callItem.response_content_error || '缺失')],
          ];
          selectedDetailGrid.innerHTML = rows.map(([key, value]) => `<div class="pk-kv-row"><dt>${{escapeHtml(key)}}</dt><dd>${{escapeHtml(value ?? '—')}}</dd></div>`).join('');
          return;
        }}
        if (node) {{
          selectedDetailTitle.textContent = node.label || selectedKey;
          selectedDetailSubtitle.textContent = `${{node.kind || 'item'}} · ${{node.status || 'unknown'}} · ${{node.meta || '—'}}`;
          selectedDetailGrid.innerHTML = `<div class="pk-kv-row"><dt>来源</dt><dd>可靠事实 / Trace 以时间线标识</dd></div>`;
          return;
        }}
        selectedDetailTitle.textContent = 'Operation';
        selectedDetailSubtitle.textContent = DATA.operation?.operation_id || '—';
        const summary = DATA.summary || {{}};
        selectedDetailGrid.innerHTML = Object.entries(summary).slice(0, 8).map(([key, value]) => `<div class="pk-kv-row"><dt>${{escapeHtml(key)}}</dt><dd>${{escapeHtml(value ?? '—')}}</dd></div>`).join('');
      }}

      function renderEvidence() {{
        const callEvidence = docEvidence[selectedKey];
        if (!callEvidence) {{
          const callItem = modelCalls.find(item => item.key === selectedKey);
          const evidenceUrl = callItem?.evidence_url;
          if (evidenceErrors[selectedKey]) {{
            evidenceTitle.textContent = 'ModelCall 证据加载失败';
            documentNav.innerHTML = '';
            documentCode.textContent = `// ${{evidenceErrors[selectedKey]}}`;
          }} else if (API_BASE && evidenceUrl && evidenceLoadingKey !== selectedKey) {{
            evidenceLoadingKey = selectedKey;
            evidenceTitle.textContent = '正在加载 ModelCall 证据…';
            documentNav.innerHTML = '';
            documentCode.textContent = '// Request / Response 内容按需读取';
            fetchJson(evidenceUrl).then(payload => {{
              if (payload?.evidence) docEvidence[selectedKey] = payload.evidence;
            }}).catch(error => {{
              evidenceErrors[selectedKey] = error.message || String(error);
              documentCode.textContent = `// 证据加载失败：${{evidenceErrors[selectedKey]}}`;
            }}).finally(() => {{
              evidenceLoadingKey = null;
              renderEvidence();
            }});
            return;
          }}
          evidenceTitle.textContent = '当前对象没有 ModelCall 内容';
          modelCallIdEl.textContent = '—';
          contentRefEl.textContent = '—';
          contentSchemaEl.textContent = 'schema —';
          contentIntegrityEl.textContent = '完整性不适用';
          contentIntegrityEl.classList.remove('pk-storage-color', 'pk-error-color');
          documentNav.innerHTML = '';
          documentPath.textContent = '—';
          documentCompleteness.textContent = '无 ModelCall 内容';
          documentCode.textContent = '// 当前选择是 Operation、Step、Tool 或 Child；请选择 ModelCall 查看完整 Request / Response';
          evidenceTabs.forEach(t => t.disabled = true);
          return;
        }}

        evidenceTabs.forEach(t => t.disabled = false);
        evidenceTitle.textContent = `${{callEvidence.label}} · 完整内容`;
        modelCallIdEl.textContent = callEvidence.model_call_id || '—';
        contentRefEl.textContent = activeEvidenceTab.startsWith('wire') || activeEvidenceTab === 'context'
          ? (callEvidence.request_content_ref || '—')
          : (callEvidence.response_content_ref || '—');

        evidenceTabs.forEach(t => t.setAttribute('aria-selected', String(t.dataset.evidenceTab === activeEvidenceTab)));

        const tabDoc = callEvidence[activeEvidenceTab] || {{ label: '', sections: [] }};
        const sections = tabDoc.sections || [];

        if (!sections.some(s => s.id === activeDocSection)) {{
          activeDocSection = sections.length > 0 ? sections[0].id : '';
        }}

        documentNav.innerHTML = `<div class="pk-document-label">${{escapeHtml(tabDoc.label)}}</div>` + sections.map(s => {{
          const isPressed = s.id === activeDocSection;
          return `<button class="pk-document-item" type="button" data-doc-sec="${{escapeHtml(s.id)}}" aria-pressed="${{String(isPressed)}}"><span>${{escapeHtml(s.label)}}</span><span class="pk-document-count">${{escapeHtml(s.count)}}</span></button>`;
        }}).join('');

        documentNav.querySelectorAll('[data-doc-sec]').forEach(btn => {{
          btn.addEventListener('click', () => {{
            activeDocSection = btn.dataset.docSec;
            renderEvidence();
          }});
        }});

        const currentSec = sections.find(s => s.id === activeDocSection);
        if (currentSec) {{
          documentPath.textContent = currentSec.path || '';
          documentCompleteness.textContent = currentSec.complete || '完整未截断';
          documentCode.textContent = typeof currentSec.value === 'string' ? currentSec.value : JSON.stringify(currentSec.value, null, 2);
          const schemaVersion = currentSec.schema_version ?? tabDoc.schema_version;
          contentSchemaEl.textContent = schemaVersion == null ? 'schema 未知' : `schema v${{schemaVersion}}`;
          const integrity = currentSec.canonical_bytes_verified === true || tabDoc.canonical_bytes_verified === true;
          const integrityFailed = currentSec.canonical_bytes_verified === false || tabDoc.canonical_bytes_verified === false || Boolean(currentSec.error || tabDoc.error);
          contentIntegrityEl.textContent = integrity ? 'canonical bytes verified' : (integrityFailed ? 'canonical bytes 校验失败' : '完整性未声明');
          contentIntegrityEl.classList.toggle('pk-storage-color', integrity);
          contentIntegrityEl.classList.toggle('pk-error-color', integrityFailed);
        }} else {{
          documentPath.textContent = '—';
          documentCompleteness.textContent = '无内容';
          documentCode.textContent = '// 无内容';
          contentSchemaEl.textContent = 'schema 未知';
          contentIntegrityEl.textContent = '完整性未声明';
          contentIntegrityEl.classList.remove('pk-storage-color', 'pk-error-color');
        }}
      }}

      function selectItem(key) {{
        if (key !== selectedKey) delete evidenceErrors[key];
        selectedKey = key;
        selectedModelCallKey = docEvidence[key] ? key : null;
        renderTree();
        renderTimeline();
        renderCharts();
        renderSelectedModelCallHeader();
        renderSelectedDetail();
        renderEvidence();
      }}

      // 事件绑定
      evidenceTabs.forEach(tab => {{
        tab.addEventListener('click', () => {{
          activeEvidenceTab = tab.dataset.evidenceTab;
          renderEvidence();
        }});
      }});

      const errorOnlyCheck = root.querySelector('#pk-errors-only');
      if (errorOnlyCheck) {{
        errorOnlyCheck.addEventListener('change', e => {{
          root.querySelectorAll('.pk-bar').forEach(bar => {{
            const isAbnormal = bar.dataset.status === 'error' || bar.dataset.status === 'affected';
            bar.classList.toggle('pk-dimmed', e.target.checked && !isAbnormal);
          }});
        }});
      }}

      // ResizeObserver
      if (typeof ResizeObserver !== 'undefined') {{
        const ro = new ResizeObserver(() => {{
          renderCharts();
        }});
        root.querySelectorAll('.pk-chart').forEach(c => ro.observe(c));
      }}

      // 初始化渲染
      renderOperationOptions(DATA.available_operations || []);
      renderTree();
      renderTimeline();
      renderCharts();
      renderSelectedModelCallHeader();
      renderSelectedDetail();
      renderEvidence();
      if (API_BASE && DATA.available_operations?.length) {{
        const initialOperation = DATA.session?.active_operation_id || DATA.available_operations.at(-1)?.operation_id;
        if (initialOperation) loadOperation(initialOperation);
      }}
    }})();
  </script>
</div>
</body>
</html>"""


def _script_safe_json(value: str) -> str:
    """返回可安全放入 ``<script type=application/json>`` 的 JSON。"""

    return (
        value.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
