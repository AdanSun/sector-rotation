"""生成不依赖 JavaScript 的可分享静态 HTML 研判报告。"""

from __future__ import annotations

import html
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "frontend" / "public" / "data"
SHARE_DIR = ROOT / "share" / "风格主题研判-轻量分享版"
OUTPUT = SHARE_DIR / "index.html"


def load(name: str):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def esc(value) -> str:
    return html.escape(str(value if value is not None else "—"))


def pct(value, digits: int = 2) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def num(value, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def svg_nav(monthly: list[dict]) -> str:
    rows = [row for row in monthly if row.get("strategy_nav") is not None and row.get("benchmark_nav") is not None]
    width, height, pad = 980, 320, 44
    values = [float(row[key]) for row in rows for key in ("strategy_nav", "benchmark_nav")]
    low, high = min(values), max(values)

    def points(key: str) -> str:
        coords = []
        for i, row in enumerate(rows):
            x = pad + i * (width - 2 * pad) / max(len(rows) - 1, 1)
            y = height - pad - (float(row[key]) - low) * (height - 2 * pad) / max(high - low, 1e-9)
            coords.append(f"{x:.1f},{y:.1f}")
        return " ".join(coords)

    return f"""<svg viewBox="0 0 {width} {height}" role="img" aria-label="策略与基准净值曲线">
      <rect width="100%" height="100%" fill="#fff"/><line x1="{pad}" y1="{height-pad}" x2="{width-pad}" y2="{height-pad}" stroke="#ccd5df"/>
      <polyline points="{points('strategy_nav')}" fill="none" stroke="#b42318" stroke-width="3"/><polyline points="{points('benchmark_nav')}" fill="none" stroke="#52708f" stroke-width="2"/>
      <text x="{pad}" y="22" fill="#b42318" font-size="14">● 策略</text><text x="{pad+90}" y="22" fill="#52708f" font-size="14">● Wind全A</text>
      <text x="{pad}" y="{height-10}" fill="#667085" font-size="12">{esc(rows[0]['month'][:7])}</text><text x="{width-pad-54}" y="{height-10}" fill="#667085" font-size="12">{esc(rows[-1]['month'][:7])}</text>
    </svg>"""


def main() -> None:
    overview = load("overview.json")
    performance = load("performance_final.json")
    holdings = load("holdings.json")
    status = load("data_status.json")
    latest = overview["as_of_date"]
    latest_holdings = [row for row in holdings if row["month"] == latest]
    metric_cards = "".join(
        f'<div class="metric"><small>{esc(item["label"])}</small><strong>{esc(int(item["value"])) if item["key"] == "months" else pct(item["value"]) if item["key"] not in ("sharpe_like", "calmar_like") else num(item["value"], 2)}</strong><span>{esc(item["description"])}</span></div>'
        for item in performance["metrics"]
    )
    holding_rows = "".join(
        f'<tr><td>{row["rank"]}</td><td>{esc(row["display_industry_name"])}</td><td>{esc(row["industry_name"])}</td><td>{pct(row["weight"])}</td><td>{num(row["score"])}</td></tr>'
        for row in latest_holdings
    ) or '<tr><td colspan="5">当月无行业持仓</td></tr>'
    recommendation_rows = "".join(
        f'<tr><td>{esc(row["strategy_name"])}</td><td>{row["rank"]}</td><td>{esc(row["display_industry_name"])}</td><td>{esc(row["industry_name"])}</td><td>{esc(row["lifecycle_stage_zh"])}</td><td>{num(row["score"])}</td></tr>'
        for row in overview["recommendations"]
    )
    annual_rows = "".join(
        f'<tr><td>{row["year"]}</td><td>{pct(row["strategy_return"])}</td><td>{pct(row["benchmark_return"])}</td><td>{pct(row["excess_return"])}</td><td>{row["month_count"]}</td></tr>'
        for row in performance["annual"]
    )
    monthly_rows = "".join(
        f'<tr><td>{esc(row["month"][:7])}</td><td>{esc(row.get("asset_name"))}</td><td>{pct(row.get("strategy_return"))}</td><td>{pct(row.get("benchmark_return"))}</td><td>{num(row.get("strategy_nav"))}</td><td>{pct(row.get("drawdown"))}</td></tr>'
        for row in reversed(performance["monthly"])
    )
    status_rows = "".join(
        f'<tr><td>{esc(row["label"])}</td><td>{esc(row["status"])}</td><td>{esc(row.get("rows"))}</td><td>{esc(row.get("min_date"))}</td><td>{esc(row.get("max_date"))}</td></tr>'
        for row in status["datasets"]
    )
    html_text = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>风格主题研判</title>
<style>*{{box-sizing:border-box}}body{{margin:0;background:#f4f6f8;color:#17212b;font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}}header{{background:#152c42;color:#fff;padding:28px max(24px,calc((100% - 1180px)/2))}}header h1{{margin:0;font-size:30px}}header p{{margin:6px 0 0;color:#cbd6e0}}nav{{position:sticky;top:0;background:#fff;border-bottom:1px solid #d9e0e7;padding:10px;text-align:center;z-index:2}}nav a{{color:#244e73;margin:0 14px;text-decoration:none;font-weight:600}}main{{max-width:1180px;margin:auto;padding:24px}}section{{background:#fff;border:1px solid #dbe2e8;border-radius:12px;padding:22px;margin-bottom:20px;box-shadow:0 2px 10px #1a2b3c0b}}h2{{margin:0 0 15px;font-size:20px;color:#173b5c}}.hero{{display:grid;grid-template-columns:1.3fr .7fr;gap:18px}}.decision{{font-size:25px;font-weight:700;color:#a12a22}}.muted{{color:#667085}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(185px,1fr));gap:12px}}.metric{{border:1px solid #e0e6ec;border-radius:9px;padding:14px;background:#fafbfc}}.metric small,.metric span{{display:block;color:#667085}}.metric strong{{display:block;font-size:24px;margin:5px 0;color:#173b5c}}.metric span{{font-size:11px}}table{{width:100%;border-collapse:collapse}}th,td{{border-bottom:1px solid #e5e9ee;padding:9px 10px;text-align:right}}th:first-child,td:first-child,th:nth-child(2),td:nth-child(2),th:nth-child(3),td:nth-child(3){{text-align:left}}th{{background:#f5f7f9;color:#34495e}}.scroll{{overflow:auto;max-height:520px}}details summary{{cursor:pointer;font-weight:700;color:#244e73;padding:8px 0}}footer{{text-align:center;color:#667085;padding:18px}}@media(max-width:760px){{.hero{{grid-template-columns:1fr}}main{{padding:12px}}nav a{{display:inline-block;margin:4px 8px}}}}</style></head><body>
<header><h1>风格主题研判</h1><p>产业周期·行业选择·资产比较｜数据截止 {esc(latest)}</p></header><nav><a href="#overview">最新研判</a><a href="#holdings">当期持仓</a><a href="#performance">策略表现</a><a href="#history">历史数据</a><a href="#status">数据状态</a></nav><main>
<section id="overview"><h2>最新研判</h2><div class="hero"><div><div class="decision">{esc(overview['asset_name'])}</div><p>{esc(overview['decision_reason'])}</p><p class="muted">实际增速：{'可用' if overview['trends']['actual_growth_available'] else '不可用'}｜预期增速：{'可用' if overview['trends']['expected_growth_available'] else '不可用'}｜ROE：{'可用' if overview['trends']['roe_available'] else '不可用'}</p></div><div><b>最新已结算净值</b><p>策略 {num(overview['performance']['strategy_nav'])}｜Wind全A {num(overview['performance']['benchmark_nav'])}</p><p>12个月收益 {pct(overview['performance']['return_12m'])}</p></div></div></section>
<section id="holdings"><h2>{esc(latest[:7])} 最终持仓</h2><div class="scroll"><table><thead><tr><th>排名</th><th>申万二级</th><th>内部行业</th><th>权重</th><th>得分</th></tr></thead><tbody>{holding_rows}</tbody></table></div></section>
<section><h2>最新子策略推荐</h2><div class="scroll"><table><thead><tr><th>策略</th><th>排名</th><th>申万二级</th><th>内部行业</th><th>生命周期</th><th>得分</th></tr></thead><tbody>{recommendation_rows}</tbody></table></div></section>
<section id="performance"><h2>策略表现</h2><div class="metrics">{metric_cards}</div><div style="margin-top:18px">{svg_nav(performance['monthly'])}</div></section>
<section><h2>年度收益</h2><table><thead><tr><th>年度</th><th>策略</th><th>Wind全A</th><th>超额</th><th>月数</th></tr></thead><tbody>{annual_rows}</tbody></table></section>
<section id="history"><details><summary>展开全部月度回测历史（{len(performance['monthly'])} 个月）</summary><div class="scroll"><table><thead><tr><th>月份</th><th>资产类别</th><th>策略收益</th><th>基准收益</th><th>策略净值</th><th>回撤</th></tr></thead><tbody>{monthly_rows}</tbody></table></div></details></section>
<section id="status"><details><summary>展开数据状态</summary><div class="scroll"><table><thead><tr><th>数据集</th><th>状态</th><th>行数</th><th>起始</th><th>截止</th></tr></thead><tbody>{status_rows}</tbody></table></div></details></section>
</main><footer>仅供研究，不构成投资建议。本文件为纯静态 HTML，不依赖 JavaScript 或外部资源。</footer></body></html>"""
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(html_text, encoding="utf-8")
    print(f"{OUTPUT} ({OUTPUT.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
