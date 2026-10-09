#!/usr/bin/env bash
# 一键刷新前端数据：国金库增量提取 → 重算模型 → 导出静态快照
# 用法:
#   ./scripts/refresh_web_data.sh              # 完整刷新（推荐每月末执行）
#   ./scripts/refresh_web_data.sh --skip-extract   # 不连库，仅重算并导出
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then echo "错误：未找到 python3" >&2; exit 1; fi

echo "==================================================================="
echo " 量化大势研判 · 数据刷新（国金库 → 前端快照）"
echo "==================================================================="
"$PY" scripts/refresh_web_data.py "$@"
