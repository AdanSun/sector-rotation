#!/usr/bin/env bash
# 启动量化大势研判 React/Vite 网页。

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR/frontend"

if [ ! -d "node_modules" ]; then
  echo "==> 安装前端依赖..."
  npm install
fi

if [ ! -d "public/data" ] || [ -z "$(ls -A public/data 2>/dev/null | head -1)" ]; then
  echo "警告：frontend/public/data/ 为空。请先运行:"
  echo "  ../scripts/refresh_web_data.sh --skip-extract"
  echo "（从国金数据库生成数据快照）"
fi

if [ "${1:-}" = "--prod" ]; then
  echo "==> 生产构建 + 预览: http://127.0.0.1:4174"
  npm run build
  npm run preview
else
  echo "==> 开发模式: http://127.0.0.1:5174"
  npm run dev
fi
