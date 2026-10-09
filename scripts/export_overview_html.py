"""把首页"风格主题研判总览"（研判总览 + 行业推荐）导出为自包含单文件 HTML。

- 数据从 frontend/public/data/*.json 读取并内联（无需联网、无需后端、双击即可打开）。
- 图表用内联 SVG（不依赖 ECharts/CDN），行业推荐支持策略切换（去"全部策略"，7 个具体策略）。
- 输出：share/风格主题研判总览.html

用法：python scripts/export_overview_html.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "frontend" / "public" / "data"
OUTPUT = PROJECT_ROOT / "share" / "风格主题研判总览.html"


def load(name: str):
    with open(DATA_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    overview = load("overview.json")
    meta = load("meta.json")
    perf = load("performance_final.json")
    rec_all = load("recommendations.json")

    latest = overview["as_of_date"]
    # 最新月的推荐行（含全部策略，供策略切换）
    rec_rows = [r for r in rec_all if r.get("month") == latest]
    # 净值近 24 月
    nav_rows = perf["monthly"][-24:]

    payload = {
        "overview": overview,
        "meta": meta,
        "nav": nav_rows,
        "recommendations": rec_rows,
    }
    payload_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    html = TEMPLATE.replace("/*__PAYLOAD__*/", payload_json)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"已输出：{OUTPUT}（{OUTPUT.stat().st_size / 1024:.0f} KB）")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>风格主题研判总览 · 量化大势研判</title>
<style>
:root {
  --c-primary-600: #2c5282;
  --c-up: #c0392b; --c-down: #1e8449; --c-warn: #b9770e;
  --c-border: #d9dde3; --c-text: #1f2933; --c-text-secondary: #5f6b7a; --c-text-muted: #8a94a3;
  --c-bg: #f4f5f7; --c-bg-card: #ffffff; --c-bg-sunken: #eef0f3; --c-zebra: #f6f7f9;
  --radius: 10px;
  --font: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--c-bg); color: var(--c-text); font-family: var(--font); font-size: 14px; line-height: 1.55; -webkit-font-smoothing: antialiased; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 24px 28px 60px; }
h1 { font-size: 24px; margin: 0 0 4px; }
.sub { color: var(--c-text-secondary); font-size: 13.5px; }
.card { background: var(--c-bg-card); border: 1px solid var(--c-border); border-radius: var(--radius); padding: 18px 20px; margin-top: 16px; box-shadow: 0 1px 2px rgba(20,30,45,.04), 0 2px 10px rgba(20,30,45,.05); }
.card-title { font-size: 14px; font-weight: 700; margin: 0 0 12px; display: flex; align-items: center; gap: 8px; }
.card-title .hint { font-weight: 400; font-size: 12px; color: var(--c-text-muted); }
.banner { border-radius: 8px; padding: 10px 14px; font-size: 13px; margin-top: 16px; }
.banner-warn { background: rgba(185,119,14,.12); color: var(--c-warn); }
.banner-info { background: rgba(44,82,130,.08); color: var(--c-primary-600); }
.hero { border-radius: var(--radius); padding: 20px 22px; border: 1px solid var(--c-border); background: var(--c-bg-card); border-left: 4px solid var(--c-primary-600); margin-top: 16px; box-shadow: 0 1px 2px rgba(20,30,45,.04), 0 2px 10px rgba(20,30,45,.05); }
.hero .asset-name { font-size: 26px; font-weight: 800; margin: 8px 0 6px; }
.tag { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; border: 1px solid var(--c-border); background: var(--c-bg-sunken); white-space: nowrap; }
.tag-warn { background: rgba(185,119,14,.12); color: var(--c-warn); border-color: transparent; }
.tag-fresh { background: rgba(30,132,73,.1); color: var(--c-down); border-color: transparent; }
.metric-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; }
.metric-card { background: var(--c-bg-card); border: 1px solid var(--c-border); border-radius: var(--radius); padding: 12px 14px; }
.metric-card-label { font-size: 12px; color: var(--c-text-secondary); margin-bottom: 6px; }
.metric-card-value { font-size: 20px; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1.2; }
.text-up { color: var(--c-up); } .text-down { color: var(--c-down); }
.grid2 { display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr); gap: 16px; align-items: stretch; }
.grid2 .card { margin-top: 16px; display: flex; flex-direction: column; }
.grid2 .card .grow { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.timeline { display: flex; gap: 4px; flex-wrap: wrap; }
.timeline .seg { flex: 1; min-width: 40px; border-radius: 4px; display: flex; align-items: center; justify-content: center; font-size: 11px; color: #fff; font-weight: 600; padding: 6px 2px; }
.legend { display: flex; gap: 14px; flex-wrap: wrap; margin-top: auto; padding-top: 12px; font-size: 12px; color: var(--c-text-secondary); }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 5px; vertical-align: -1px; }
.control-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
.control-row label { font-size: 13px; color: var(--c-text-secondary); }
select { background: var(--c-bg-card); color: var(--c-text); border: 1px solid var(--c-border); border-radius: 6px; padding: 7px 10px; font-size: 13px; font-family: inherit; }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
th { background: var(--c-bg-sunken); color: var(--c-text-secondary); text-align: left; padding: 7px 10px; white-space: nowrap; border-bottom: 1px solid var(--c-border); }
td { padding: 6px 10px; border-bottom: 1px solid var(--c-border); font-variant-numeric: tabular-nums; }
tbody tr:nth-child(even) { background: var(--c-zebra); }
.num { text-align: right; }
.foot { color: var(--c-text-muted); font-size: 12px; margin-top: 10px; }
a { color: var(--c-primary-600); text-decoration: none; }
svg text { font-family: var(--font); }
</style>
</head>
<body>
<div class="wrap">
  <h1>风格主题研判总览</h1>
  <div class="sub" id="page-sub"></div>

  <div id="banner"></div>
  <div class="hero" id="hero"></div>

  <div class="card">
    <div class="card-title">增长与盈利状态 <span class="hint">趋势数值方向：正=扩张，负=收缩</span></div>
    <div class="metric-grid" id="trends"></div>
  </div>

  <div class="grid2">
    <div class="card">
      <div class="card-title">最终策略 vs 基准净值 <span class="hint">近 24 月</span></div>
      <div class="grow"><div id="nav-chart"></div><div class="legend" id="nav-metrics" style="padding-top:10px"></div></div>
    </div>
    <div class="card">
      <div class="card-title">近 12 月资产类别时间线 <span class="hint">末段为当前月份</span></div>
      <div class="grow"><div class="timeline" id="timeline"></div><div class="legend" id="timeline-legend"></div></div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">行业推荐 <span class="hint" id="rec-sub"></span></div>
    <div class="control-row">
      <label>策略</label>
      <select id="strategy-select" aria-label="筛选子策略"></select>
      <label>展示层级</label>
      <select id="level-select" aria-label="展示层级">
        <option value="2">中信二级</option><option value="3">中信三级</option>
      </select>
      <span class="tag" id="rec-count"></span>
    </div>
    <div id="rec-chart"></div>
    <div style="overflow-x:auto; margin-top:14px"><table id="rec-table"></table></div>
    <p class="foot">* 下月收益为回测历史数据，仅在历史月份展示；最新月份不展示未来收益。仅供研究，不构成投资建议。</p>
  </div>

  <div class="banner banner-info">本系统为研究决策支持工具，所有结论基于模型输出，仅供研究参考，不构成投资建议。历史下月收益为回测结果，不代表未来表现。</div>
</div>

<script>
var DATA = /*__PAYLOAD__*/;
var OV = DATA.overview, META = DATA.meta, NAV = DATA.nav, REC = DATA.recommendations;

var ASSET_COLORS = { main_assets: "#2c5282", quality_dividend: "#b9770e", value_dividend: "#7f8c8d", cash: "#95a5a6" };
var STRATEGY_NAMES = {};
(META.strategies || []).forEach(function (s) { STRATEGY_NAMES[s.id] = s.name; });

function esc(s) { return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function pct(v, d) { if (v == null || isNaN(v)) return d || "—"; return (v >= 0 ? "+" : "") + (v * 100).toFixed(1) + "%"; }
function navFmt(v) { return v == null ? "—" : v.toFixed(3); }
function scoreFmt(v) { return v == null ? "—" : v.toFixed(3); }
function shortMonth(m) { return m ? m.slice(0, 7) : ""; }
function fmtMonth(m) { return m ? m.slice(0, 4) + "-" + m.slice(5, 7) : ""; }

function init() {
  document.getElementById("page-sub").innerHTML =
    "研判月份：" + esc(OV.as_of_date) + "（模型最新月）" +
    (OV.asset_name ? " · 当前资产类别：" + esc(OV.asset_name) : "");

  var b = document.getElementById("banner");
  if (OV.data_warnings && OV.data_warnings.length) {
    b.className = "banner banner-warn";
    b.innerHTML = "<strong>数据滞后提示：</strong>" + esc(OV.data_warnings.join(" "));
  }

  var hero = document.getElementById("hero");
  var perf = OV.performance || {};
  hero.innerHTML =
    '<span class="tag">当前资产类别</span> ' +
    '<div class="asset-name">' + esc(OV.asset_name || (OV.has_valid_signal ? "—" : "现金")) + "</div>" +
    (OV.decision_reason ? '<div style="color:var(--c-text-secondary);font-size:13.5px;max-width:860px"><strong>决策依据：</strong>' + esc(OV.decision_reason) + "</div>" : "") +
    '<div style="margin-top:10px;display:flex;gap:16px;flex-wrap:wrap">' +
      (OV.decision_score != null ? '<span class="tag">决策得分 ' + esc(scoreFmt(OV.decision_score)) + "</span>" : "") +
      (perf.strategy_nav != null ? '<span class="tag">策略净值 ' + esc(navFmt(perf.strategy_nav)) + "</span>" : "") +
      (perf.return_12m != null ? '<span class="tag">近12月收益 <span class="' + (perf.return_12m > 0 ? "text-up" : "text-down") + '">' + pct(perf.return_12m) + "</span></span>" : "") +
    "</div>";

  renderTrends();
  renderNav();
  renderTimeline();
  initRecommendations();
}

function renderTrends() {
  var t = OV.trends || {};
  var items = [
    { label: "实际增速趋势", avail: t.actual_growth_available, v: t.actual_growth_trend, unit: "pct" },
    { label: "一致预期增速趋势", avail: t.expected_growth_available, v: t.expected_growth_trend, unit: "pct" },
    { label: "ROE 趋势", avail: t.roe_available, v: t.roe_trend, unit: "raw" },
  ];
  var html = items.map(function (it) {
    var cls = it.v == null ? "" : it.unit === "pct" ? (it.v > 0 ? "text-up" : "text-down") : "";
    var val = it.v == null ? "—" : it.unit === "pct" ? pct(it.v) : it.v.toFixed(3);
    return '<div class="metric-card"><div class="metric-card-label">' + esc(it.label) +
      (it.avail ? "" : '<span class="tag tag-warn" style="margin-left:6px">不可用</span>') +
      '</div><div class="metric-card-value ' + cls + '">' + val + "</div></div>";
  }).join("");
  var crowd = t.roe_crowding_high == null ? "—" :
    (t.roe_crowding_high ? '<span class="tag tag-warn">拥挤</span>' : '<span class="tag tag-fresh">不拥挤</span>');
  html += '<div class="metric-card"><div class="metric-card-label">ROE 拥挤度</div>' +
    '<div class="metric-card-value">' + crowd + "</div></div>";
  document.getElementById("trends").innerHTML = html;
}

function renderNav() {
  var el = document.getElementById("nav-chart");
  var w = 560, h = 240, padL = 46, padR = 14, padT = 14, padB = 30;
  var months = NAV.map(function (m) { return fmtMonth(m.month); });
  var nav = NAV.map(function (m) { return m.strategy_nav; });
  var bnav = NAV.map(function (m) { return m.benchmark_nav; });
  var min = Math.min.apply(null, nav.concat(bnav).filter(function (x) { return x != null; }));
  var max = Math.max.apply(null, nav.concat(bnav).filter(function (x) { return x != null; }));
  var span = (max - min) || 1;
  var iw = w - padL - padR, ih = h - padT - padB;
  var px = function (i) { return padL + (i / (months.length - 1)) * iw; };
  var py = function (v) { return padT + ih - ((v - min) / span) * ih; };
  function path(arr) {
    var d = "";
    arr.forEach(function (v, i) { if (v == null) return; d += (d ? "L" : "M") + px(i).toFixed(1) + " " + py(v).toFixed(1); });
    return d;
  }
  var grid = "";
  for (var g = 0; g <= 4; g++) {
    var gy = padT + (ih / 4) * g;
    grid += '<line x1="' + padL + '" y1="' + gy + '" x2="' + (w - padR) + '" y2="' + gy + '" stroke="#d9dde3" stroke-dasharray="3 3"/>';
  }
  var labels = "";
  months.forEach(function (m, i) {
    if (i % 2 === 0 || i === months.length - 1) {
      labels += '<text x="' + px(i) + '" y="' + (h - 8) + '" font-size="10" fill="#5f6b7a" text-anchor="middle">' + m + "</text>";
    }
  });
  el.innerHTML =
    '<svg viewBox="0 0 ' + w + " " + h + '" width="100%" role="img" aria-label="净值走势">' +
    grid + labels +
    '<path d="' + path(bnav) + '" fill="none" stroke="#8a94a3" stroke-width="1.5" stroke-dasharray="5 4"/>' +
    '<path d="' + path(nav) + '" fill="none" stroke="#c0392b" stroke-width="2.2"/>' +
    '<text x="' + (w - padR) + '" y="' + (py(nav[nav.length - 1]) - 6) + '" font-size="10.5" fill="#c0392b" text-anchor="end">最终策略</text>' +
    '<text x="' + (w - padR) + '" y="' + (py(bnav[bnav.length - 1]) + 14) + '" font-size="10.5" fill="#8a94a3" text-anchor="end">基准</text>' +
    "</svg>";
  var perf = OV.performance || {};
  var items = [
    ["策略净值", navFmt(perf.strategy_nav), "text-up"], ["基准净值", navFmt(perf.benchmark_nav), ""],
    ["近1月", pct(perf.return_1m), perf.return_1m > 0 ? "text-up" : "text-down"],
    ["近3月", pct(perf.return_3m), perf.return_3m > 0 ? "text-up" : "text-down"],
    ["近6月", pct(perf.return_6m), perf.return_6m > 0 ? "text-up" : "text-down"],
    ["近12月", pct(perf.return_12m), perf.return_12m > 0 ? "text-up" : "text-down"],
  ];
  document.getElementById("nav-metrics").innerHTML = items.map(function (it) {
    return "<span>" + it[0] + " <strong class='" + it[2] + "'>" + esc(it[1]) + "</strong></span>";
  }).join("");
}

function renderTimeline() {
  var hist = OV.asset_history || [];
  var el = document.getElementById("timeline");
  el.innerHTML = hist.map(function (p) {
    return '<div class="seg" style="background:' + (ASSET_COLORS[p.asset_type] || "#95a5a6") +
      '" title="' + esc(p.month) + "：" + esc(p.asset_name) + '">' + esc(shortMonth(p.month).slice(5)) + "</div>";
  }).join("");
  document.getElementById("timeline-legend").innerHTML = Object.keys(ASSET_COLORS).map(function (k) {
    return "<span><i style='background:" + ASSET_COLORS[k] + "'></i>" + esc(k) + "</span>";
  }).join("");
}

function pickRows() {
  var sid = document.getElementById("strategy-select").value;
  var lvl = document.getElementById("level-select").value;
  return REC.filter(function (r) {
    if (sid && r.strategy_id !== sid) return false;
    if (lvl === "3" && !r.industry_old_code) return false;
    if (lvl === "2" && !r.display_industry_code) return false;
    return true;
  }).sort(function (a, b) { return (a.rank || 0) - (b.rank || 0); });
}

function rowIndustryName(r) {
  var lvl = document.getElementById("level-select").value;
  return lvl === "3" && r.industry_name ? r.industry_name : r.display_industry_name;
}
function rowIndustryCode(r) {
  var lvl = document.getElementById("level-select").value;
  return lvl === "3" ? (r.industry_old_code || r.display_industry_code) : r.display_industry_code;
}

function renderRecChart(rows) {
  var el = document.getElementById("rec-chart");
  if (!rows.length) { el.innerHTML = '<p class="foot">当前筛选条件下没有推荐结果。</p>'; return; }
  var top = rows.slice(0, 15);
  var w = 760, h = 56 + top.length * 22, padL = 150, padR = 30, padT = 10, padB = 26;
  var iw = w - padL - padR, ih = h - padT - padB;
  var x = function (v) { return padL + v * iw; };
  var bars = "", labels = "";
  top.forEach(function (r, i) {
    var y = padT + i * 22 + 6, bh = 14;
    var s = r.score == null ? 0 : Math.max(0, Math.min(1, r.score));
    bars += '<rect x="' + padL + '" y="' + y + '" width="' + x(s) + '" height="' + bh +
      '" rx="2" fill="#c0392b"><title>' + esc(rowIndustryName(r)) + "（" + esc(rowIndustryCode(r)) +
      "） 得分 " + scoreFmt(r.score) + "</title></rect>";
    var name = rowIndustryName(r);
    if (name.length > 8) name = name.slice(0, 8) + "…";
    labels += '<text x="' + (padL - 8) + '" y="' + (y + bh - 3) + '" font-size="11" fill="#5f6b7a" text-anchor="end">' + esc(name) + "</text>";
    if (r.score != null) {
      bars += '<text x="' + (x(s) + 4) + '" y="' + (y + bh - 3) + '" font-size="10" fill="#8a94a3">' + scoreFmt(r.score) + "</text>";
    }
  });
  for (var g = 0; g <= 4; g++) {
    var gx = x(g / 4);
    labels += '<line x1="' + gx + '" y1="' + padT + '" x2="' + gx + '" y2="' + (h - padB) + '" stroke="#d9dde3" stroke-dasharray="3 3"/>';
    labels += '<text x="' + gx + '" y="' + (h - 10) + '" font-size="9" fill="#8a94a3" text-anchor="middle">' + (g / 4).toFixed(2) + "</text>";
  }
  el.innerHTML = '<svg viewBox="0 0 ' + w + " " + h + '" width="100%" role="img" aria-label="推荐行业得分">' + labels + bars + "</svg>";
}

function renderRecTable(rows) {
  var el = document.getElementById("rec-table");
  var head = "<thead><tr><th>排名</th><th>行业</th><th>生命周期</th><th class='num'>得分</th><th class='num'>下月收益*</th><th>数据状态</th></tr></thead>";
  var body = rows.map(function (r, idx) {
    var ret = r.next_month_log_return == null ? "—" :
      '<span class="' + (r.next_month_log_return > 0 ? "text-up" : "text-down") + '">' + pct(r.next_month_log_return) + "</span>";
    var rname = rowIndustryName(r);
    var rcode = rowIndustryCode(r);
    var inner = (document.getElementById("level-select").value === "3" || !r.industry_name || r.industry_name === r.display_industry_name)
      ? "" : '<span style="color:var(--c-text-muted);font-size:11px;margin-left:6px">' + esc(r.industry_name) + "</span>";
    return "<tr><td>" + (idx + 1) + "</td><td>" + esc(rname) + inner +
      "</td><td>" + esc(r.lifecycle_stage_zh || "—") + "</td><td class='num'>" + scoreFmt(r.score) +
      "</td><td class='num'>" + ret + "</td><td>" +
      (r.data_available ? '<span class="tag tag-fresh">有效</span>' : '<span class="tag tag-warn">数据不可用</span>') +
      "</td></tr>";
  }).join("");
  el.innerHTML = head + "<tbody>" + body + "</tbody>";
  document.getElementById("rec-count").textContent = "推荐 " + rows.length + " 个行业";
  document.getElementById("rec-sub").textContent = "最新月：" + esc(OV.as_of_date) + " · 得分 Top 15 图示";
}

function initRecommendations() {
  var sel = document.getElementById("strategy-select");
  var ids = {};
  REC.forEach(function (r) { ids[r.strategy_id] = true; });
  var strategyIds = (META.strategies || []).map(function (s) { return s.id; }).filter(function (id) { return ids[id]; });
  var defaultId = strategyIds[0] || REC[0] && REC[0].strategy_id || "";
  strategyIds.forEach(function (id) {
    var o = document.createElement("option");
    o.value = id; o.textContent = STRATEGY_NAMES[id] || id;
    sel.appendChild(o);
  });
  sel.value = defaultId;
  sel.addEventListener("change", refreshRec);
  document.getElementById("level-select").addEventListener("change", refreshRec);
  refreshRec();
}

function refreshRec() {
  var rows = pickRows();
  renderRecChart(rows);
  renderRecTable(rows);
}

init();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
