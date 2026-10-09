"""把 vite --mode standalone 构建产物合并为单文件 HTML（内联 JS/CSS）。

用法：python scripts/export_standalone_html.py
前置：cd frontend && npx vite build --mode standalone（产物在 frontend/dist/standalone.html）
输出：share/风格主题研判总览.html（双击即开、与网页首页完全一致）
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIST = PROJECT_ROOT / "frontend" / "dist"
OUTPUT = PROJECT_ROOT / "share" / "风格主题研判总览.html"


def main() -> None:
    html = (DIST / "standalone.html").read_text(encoding="utf-8")

    # 内联 CSS
    def inline_css(m: re.Match) -> str:
        css = (DIST / m.group(1)).read_text(encoding="utf-8")
        return f"<style>{css}</style>"

    # 内联 JS：产物为 IIFE（非 module），普通 script 即可，file:// 下稳定执行
    def inline_js(m: re.Match) -> str:
        return ""  # 从 head 中移除，移到 body 末尾

    def inline_css(m: re.Match) -> str:
        css = (DIST / m.group(1)).read_text(encoding="utf-8")
        return f"<style>{css}</style>"

    html = re.sub(r'<link rel="stylesheet"[^>]*href="\./([^"]+)"[^>]*>', inline_css, html)
    html = re.sub(r'<script type="module"[^>]*src="\./([^"]+)"[^>]*></script>', inline_js, html)

    # 把内联 JS 移到 </body> 前（避免 React 在 DOM 未就绪时 createRoot）
    js_path = next(iter((DIST / "assets").glob("standalone-*.js")))
    js_content = js_path.read_text(encoding="utf-8")
    inline_script = f"<script>{js_content}</script>"
    if "</body>" in html:
        html = html.replace("</body>", inline_script + "</body>")
    else:
        html = html.replace("</html>", inline_script + "</html>")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(html, encoding="utf-8")
    size_mb = OUTPUT.stat().st_size / 1024 / 1024
    print(f"已输出：{OUTPUT}（{size_mb:.1f} MB，单文件自包含）")


if __name__ == "__main__":
    main()
