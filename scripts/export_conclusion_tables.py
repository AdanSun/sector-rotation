# -*- coding: utf-8 -*-
"""把前端 JSON 快照导出为人工可读的表格文件（CSV + 多 Sheet Excel）。

数据源：frontend/public/data/ 下 performance_final / overview / recommendations / meta
产物（写入 output/tables/）：
  1. performance_daily_nav.csv     日频收益/净值表（组合 vs 基准）
  2. performance_monthly_nav.csv   月度收益/净值表（组合 vs 基准）
  3. 风格研判结论与行业推荐_YYYY-MM.xlsx
     Sheet1 风格研判结论 | Sheet2 行业推荐Top(总览页) | Sheet3 行业推荐全部(当月)
     Sheet4 资产时间线(近12月) | Sheet5 月度净值收益 | Sheet6 日频净值收益

用法：在项目根目录运行  python scripts/export_conclusion_tables.py
数据刷新后重跑一次即可更新。
"""
import json
import csv
from pathlib import Path

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "frontend" / "public" / "data"
OUT = ROOT / "output" / "tables"

# 数值在 Excel 中的显示格式
PCT = "0.00%"
NAV = "0.0000"
NUM2 = "0.00"
NUM3 = "0.000"

HEAD_FILL = PatternFill("solid", fgColor="1F4E79")
HEAD_FONT = Font(color="FFFFFF", bold=True, size=10)
NOTE_FONT = Font(color="808080", italic=True, size=9)


def load(name):
    with open(DATA / name, encoding="utf-8") as f:
        return json.load(f)


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


def style_sheet(ws, header_row=1):
    for cell in ws[header_row]:
        cell.fill = HEAD_FILL
        cell.font = HEAD_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1).coordinate


def auto_width(ws, max_width=42):
    for col in ws.columns:
        width = 0
        letter = get_column_letter(col[0].column)
        for cell in col:
            v = cell.value
            if v is None:
                continue
            # 中文字符按 2 个宽度估算
            est = sum(2 if ord(ch) > 127 else 1 for ch in str(v))
            width = max(width, est)
        ws.column_dimensions[letter].width = min(max(width + 2, 8), max_width)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    perf = load("performance_final.json")
    overview = load("overview.json")
    recs_all = load("recommendations.json")
    meta = load("meta.json")

    as_of = overview["as_of_date"]
    asset_type_map = {a["code"]: a["name"] for a in meta["asset_types"]}
    strategy_map = {s["id"]: s["name"] for s in meta["strategies"]}

    # ---------- CSV：日频 / 月度 ----------
    def nav_rows(seq):
        rows = []
        for x in seq:
            rows.append([
                x["month"],
                round(x["strategy_return"], 6) if x["strategy_return"] is not None else None,
                round(x["benchmark_return"], 6) if x["benchmark_return"] is not None else None,
                round(x["strategy_nav"], 6) if x["strategy_nav"] is not None else None,
                round(x["benchmark_nav"], 6) if x["benchmark_nav"] is not None else None,
                round(x["drawdown"], 6) if x["drawdown"] is not None else None,
                round(x["benchmark_drawdown"], 6) if x["benchmark_drawdown"] is not None else None,
                round(x["turnover"], 6) if x.get("turnover") is not None else None,
                x.get("asset_type") or "",
                x.get("asset_name") or "",
                x.get("valid"),
            ])
        return rows

    nav_header = ["日期", "策略收益", "基准收益", "策略净值", "基准净值",
                  "策略回撤", "基准回撤", "换手率", "当期资产类型", "当期资产名称", "信号有效"]
    write_csv(OUT / "performance_daily_nav.csv", nav_header, nav_rows(perf["daily"]))
    write_csv(OUT / "performance_monthly_nav.csv", nav_header, nav_rows(perf["monthly"]))

    # ---------- Excel ----------
    wb = openpyxl.Workbook()

    # Sheet1 风格研判结论（键值长表，便于粘贴到文档）
    ws = wb.active
    ws.title = "风格研判结论"
    ws.append(["指标", "数值", "说明"])
    perf_snap = overview.get("performance", {})
    p = overview["performance"]
    kv_rows = [
        ("研判月份", as_of, "模型信号形成月（月末）"),
        ("资产类型", overview.get("asset_type"), "代码：" + (asset_type_map.get(overview.get("asset_type"), "") or "")),
        ("当前资产", overview.get("asset_name"), "本月实际配置组合"),
        ("决策得分", overview.get("decision_score"), "0–3，越高代表主流资产越明确"),
        ("决策依据", overview.get("decision_reason"), ""),
        ("持仓行业数", overview.get("holding_count"), "当期持仓行业个数"),
        ("实际增速趋势", overview["trends"].get("actual_growth_trend"), "模型原始值（实际增速近月变化）"),
        ("预期增速趋势", overview["trends"].get("expected_growth_trend"), "模型原始值（预期增速近月变化）"),
        ("ROE 趋势", overview["trends"].get("roe_trend"), "模型原始值（ROE 近月变化）"),
        ("ROE 拥挤度偏高", ("是" if overview["trends"].get("roe_crowding_high") else "否"), ""),
        ("数据月份", p.get("latest_settled_month"), "净值已结算至该月末"),
        ("策略净值", p.get("strategy_nav"), "累计净值（2009-02 起，含交易成本）"),
        ("基准净值", p.get("benchmark_nav"), "申万行业指数等权基准"),
        ("近1月收益", p.get("return_1m"), "策略收益"),
        ("近3月收益", p.get("return_3m"), ""),
        ("近6月收益", p.get("return_6m"), ""),
        ("近12月收益", p.get("return_12m"), ""),
    ]
    num_fmt = {i + 1: NUM2 for i in range(len(kv_rows))}
    for k, v, note in kv_rows:
        ws.append([k, v, note])
    # 按行设置数值格式：得分/趋势用 0.00；净值用 0.0000；收益用 0.00%
    for i, (k, v, note) in enumerate(kv_rows, start=2):
        if k in ("决策得分", "实际增速趋势", "预期增速趋势", "ROE 趋势"):
            ws.cell(row=i, column=2).number_format = NUM2
        elif k in ("策略净值", "基准净值"):
            ws.cell(row=i, column=2).number_format = NAV
        elif k.endswith("收益"):
            ws.cell(row=i, column=2).number_format = PCT
    ws.append([])
    ws.append(["注", "本表数据来自模型快照 " + as_of + "，刷新数据后请重跑本脚本；收益/回撤/换手为小数，Excel 中已按百分数显示", ""])
    ws.cell(row=ws.max_row, column=1).font = NOTE_FONT
    ws.cell(row=ws.max_row, column=2).font = NOTE_FONT
    style_sheet(ws)
    auto_width(ws)

    # Sheet2 行业推荐 Top（总览页口径，最新月）
    top10 = overview.get("recommendations", [])
    ws = wb.create_sheet("行业推荐Top(总览)")
    ws.append(["排名", "策略", "二级行业(展示)", "三级行业", "生命周期", "评分"])
    for x in top10:
        ws.append([x.get("rank"), x.get("strategy_name"), x.get("display_industry_name"),
                   x.get("industry_name"), x.get("lifecycle_stage_zh"),
                   round(x["score"], 4) if x.get("score") is not None else None])
        ws.cell(row=ws.max_row, column=6).number_format = NUM3
    style_sheet(ws)
    auto_width(ws)

    # Sheet3 行业推荐全部（最新月，7 策略 × 前 5）
    last_recs = [x for x in recs_all if x["month"] == as_of]
    last_recs.sort(key=lambda x: (x.get("strategy_name") or "", x.get("rank") or 99))
    ws = wb.create_sheet("行业推荐全部(当月)")
    ws.append(["策略", "排名", "二级行业(展示)", "三级行业", "生命周期", "评分",
               "资产类型", "下月已实现收益(对数)"])
    for x in last_recs:
        ws.append([x.get("strategy_name"), x.get("rank"), x.get("display_industry_name"),
                   x.get("industry_name"), x.get("lifecycle_stage_zh"),
                   round(x["score"], 4) if x.get("score") is not None else None,
                   x.get("asset_type"),
                   round(x["next_month_log_return"], 6) if x.get("next_month_log_return") is not None else None])
        row = ws.max_row
        ws.cell(row=row, column=6).number_format = NUM3
        if x.get("next_month_log_return") is not None:
            ws.cell(row=row, column=8).number_format = PCT
    ws.append([])
    ws.append(["注", "下月已实现收益为该行业次月真实对数收益（用于事后核验推荐效果）；未产生下月数据的行为空。", "", "", "", "", "", ""])
    for c in range(1, 9):
        ws.cell(row=ws.max_row, column=c).font = NOTE_FONT
    style_sheet(ws)
    auto_width(ws)

    # Sheet4 资产时间线（近 12 月）
    ws = wb.create_sheet("资产时间线(近12月)")
    ws.append(["月份", "资产类型", "资产名称", "是否选中"])
    for x in overview.get("asset_history", []):
        ws.append([x.get("month"),
                   (asset_type_map.get(x.get("asset_type")) or x.get("asset_type")) or "",
                   x.get("asset_name"),
                   "是" if x.get("selected") else ""])
    style_sheet(ws)
    auto_width(ws)

    # Sheet5 月度净值收益
    ws = wb.create_sheet("月度净值收益")
    ws.append(nav_header)
    for r in nav_rows(perf["monthly"]):
        ws.append(r)
    _fmt_nav_sheet(ws)
    style_sheet(ws)
    auto_width(ws)

    # Sheet6 日频净值收益
    ws = wb.create_sheet("日频净值收益")
    ws.append(nav_header)
    for r in nav_rows(perf["daily"]):
        ws.append(r)
    _fmt_nav_sheet(ws)
    style_sheet(ws)
    auto_width(ws)

    xlsx_path = OUT / f"风格研判结论与行业推荐_{as_of[:7]}.xlsx"
    wb.save(xlsx_path)
    print("已生成：")
    print(" ", OUT / "performance_daily_nav.csv")
    print(" ", OUT / "performance_monthly_nav.csv")
    print(" ", xlsx_path)


def _fmt_nav_sheet(ws):
    """净值表数值格式：收益/回撤/换手 = 百分数，净值 = 4 位小数"""
    for row in ws.iter_rows(min_row=2):
        for i, cell in enumerate(row, start=1):
            if cell.value is None:
                continue
            if i in (2, 3, 6, 7, 8):
                cell.number_format = PCT
            elif i in (4, 5):
                cell.number_format = NAV


if __name__ == "__main__":
    main()
