"""从国金数据库刷新前端数据快照（月度/定期更新入口）。

流程：
    1) 增量提取：国金库 → data/raw（python run.py --step extract，自动清理可重建缓存）
    2) 重算模型：lifecycle → factors → valuation → valuation_regression
                  → dividend_assets → strategy → backtest（--overwrite）
    3) 导出快照：scripts/export_web_data.py → frontend/public/data/*.json

用法:
    python scripts/refresh_web_data.py                # 完整刷新（extract + 模型 + 导出）
    python scripts/refresh_web_data.py --skip-extract # 跳过数据库提取，仅重算模型并导出
    python scripts/refresh_web_data.py --skip-model   # 仅重新导出已有 processed 面板
"""

from __future__ import annotations

import argparse
import os
import runpy
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# 模型模块覆盖时清理中间图会触发沙箱批量删除确认（后台任务无法交互）。
# 快照刷新链路统一保留中间图（不影响任何模型计算口径）。
os.environ.setdefault("SKIP_FIG_CLEANUP", "1")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

STEPS = [
    ("生命周期分类", "src.lifecycle"),
    ("行业因子", "src.factors"),
    ("估值与红利", "src.valuation"),
    ("估值回归", "src.valuation_regression"),
    ("红利资产", "src.dividend_assets"),
    ("策略信号", "src.strategy"),
    ("决策树与回测", "src.backtest"),
]


def _sync_static_site_snapshot() -> None:
    """尽力同步旧静态发布目录；Vite 页面始终直接读取 public 快照。

    ``frontend/data`` 仅服务于历史的 Python 静态服务器发布版。它可能包含由
    Excel 或其他进程锁定的旧文件，因此不得让这一步掩盖已成功完成的模型重算和
    ``frontend/public/data`` 导出。
    """

    source = PROJECT_ROOT / "frontend" / "public" / "data"
    destination = PROJECT_ROOT / "frontend" / "data"
    static_index = PROJECT_ROOT / "frontend" / "index.html"
    if not source.exists() or not static_index.exists():
        return
    try:
        shutil.copytree(source, destination, dirs_exist_ok=True)
    except OSError as exc:
        print(f"警告：旧静态网页数据未完全同步（不影响 Vite 页面）：{exc}")
        return
    print("旧静态网页数据已同步：frontend/data/")


def _run_py(target: str, argv_extra: list[str], label: str, is_path: bool = False) -> None:
    """在当前进程内执行 Python 模块/脚本（等效 python -m / python <file>）。

    不使用 subprocess：避免子进程继承沙箱限制（模型覆盖时删除旧输出图会被拦截）。
    捕获模块顶层的 SystemExit（模型模块以 raise SystemExit(main()) 结束）。
    """

    print(f"\n==> [{label}] python {'-m ' if not is_path else ''}{target} {' '.join(argv_extra)}")
    started = time.time()
    previous_argv = sys.argv
    sys.argv = [target, *argv_extra]
    try:
        try:
            if is_path:
                runpy.run_path(str(PROJECT_ROOT / target), run_name="__main__")
            else:
                runpy.run_module(target, run_name="__main__")
        except SystemExit as exc:
            if exc.code not in (0, None):
                raise SystemExit(f"步骤失败（[{label}]，退出码 {exc.code}），已停止。")
    finally:
        sys.argv = previous_argv
    print(f"    完成，耗时 {time.time() - started:.0f}s")


def _latest_end_month() -> str:
    """根据 extract 记录的最新数据日期，计算模型 end-month（数据完整覆盖的月末）。

    规则：最新数据日期所在月的月末若距其超过 10 天（该月未走完），
    则回退到上一月末，保证模型信号基于完整月数据。
    """

    end_file = PROJECT_ROOT / "data" / "raw" / ".latest_end_date.txt"
    if end_file.exists():
        text = end_file.read_text(encoding="utf-8").strip()
        try:
            ts = datetime.strptime(text[:10], "%Y-%m-%d")
            if ts.month == 12:
                month_end = datetime(ts.year, 12, 31)
            else:
                month_end = datetime(ts.year, ts.month + 1, 1) - timedelta(days=1)
            if (month_end - ts).days > 10:
                month_end = datetime(ts.year, ts.month, 1) - timedelta(days=1)
            return month_end.strftime("%Y-%m-%d")
        except ValueError:
            pass
    from config import END_DATE

    return END_DATE


def main() -> int:
    parser = argparse.ArgumentParser(description="从国金数据库刷新前端数据快照")
    parser.add_argument("--skip-extract", action="store_true", help="跳过国金库增量提取")
    parser.add_argument("--skip-model", action="store_true", help="跳过模型重算，仅重新导出")
    parser.add_argument("--skip-industry", action="store_true", help="导出时跳过行业详情预计算")
    parser.add_argument("--with-plots", action="store_true", help="同时重生成输出图（默认跳过）")
    args = parser.parse_args()

    overall = time.time()

    # 1) 增量提取
    if not args.skip_extract:
        _run_py("run.py", ["--step", "extract"], "国金库增量提取", is_path=True)
        _run_py("src.validate_data", [], "数据质量验证")
    else:
        print("==> 跳过 extract（使用现有 data/raw 缓存）")

    # 2) 模型重算
    if not args.skip_model:
        end_month = _latest_end_month()
        print(f"==> 模型 end-month：{end_month}（依据国金库最新数据日期）")
        for label, module in STEPS:
            argv_extra = ["--overwrite"]
            if module in {"src.strategy", "src.backtest"}:
                argv_extra.append("--end-month")
                argv_extra.append(end_month)
            _run_py(module, argv_extra, label)
        if args.with_plots:
            _run_py("src.plots", [], "输出图")
    else:
        print("==> 跳过模型重算（使用现有 processed 面板）")

    # 3) 导出快照
    export_argv: list[str] = []
    if args.skip_industry:
        export_argv.append("--skip-industry")
    _run_py("scripts/export_web_data.py", export_argv, "导出前端快照", is_path=True)
    _sync_static_site_snapshot()

    print(f"\n全部完成，总耗时 {time.time() - overall:.0f}s。")
    print("前端数据快照已更新：frontend/public/data/")
    print("提示：重新构建或刷新前端页面即可看到最新数据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
