"""将 Vite 产物与静态数据合并为可直接发送的单文件 HTML。"""

from __future__ import annotations

import base64
import gzip
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "frontend" / "dist"
OUTPUT = ROOT / "share" / "风格主题研判.html"


def main() -> None:
    html = (DIST / "index.html").read_text(encoding="utf-8")
    script_match = re.search(r'<script type="module"[^>]*src="([^"]+)"[^>]*></script>', html)
    style_match = re.search(r'<link rel="stylesheet"[^>]*href="([^"]+)"[^>]*>', html)
    if not script_match or not style_match:
        raise RuntimeError("未找到 Vite 主脚本或样式表")

    script = (DIST / script_match.group(1).lstrip("./")).read_text(encoding="utf-8")
    style = (DIST / style_match.group(1).lstrip("./")).read_text(encoding="utf-8")

    embedded: dict[str, dict[str, str]] = {}
    for path in sorted((DIST / "data").rglob("*")):
        if path.is_file():
            key = "data/" + path.relative_to(DIST / "data").as_posix()
            if path.suffix.lower() == ".json":
                embedded[key] = {
                    "kind": "gzip",
                    "data": base64.b64encode(gzip.compress(path.read_bytes(), compresslevel=9)).decode("ascii"),
                }
    for path in sorted((DIST / "assets").glob("*.png")):
        embedded["assets/" + path.name] = {
            "kind": "base64",
            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
        }

    payload = json.dumps(embedded, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    bootstrap = fr"""
<script>
window.addEventListener('error',event=>{{
  document.body.innerHTML='<pre style="padding:24px;white-space:pre-wrap;font:14px/1.6 sans-serif;color:#9b1c1c">页面启动失败：'+String(event.message||'未知错误')+'</pre>';
}});
const __FILES__={payload};
const __nativeFetch__=window.fetch.bind(window);
window.fetch=async function(input,init){{
  const raw=typeof input==='string'?input:input.url;
  const key=raw.replace(/^.*?(data\/|assets\/)/,'$1').split(/[?#]/)[0];
  const item=__FILES__[key];
  if(!item)return __nativeFetch__(input,init);
  const bytes=Uint8Array.from(atob(item.data),c=>c.charCodeAt(0));
  if(item.kind==='gzip'){{
    const stream=new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
    return new Response(stream,{{status:200,headers:{{'Content-Type':'application/json; charset=utf-8'}}}});
  }}
  return new Response(bytes,{{status:200,headers:{{'Content-Type':'image/png'}}}});
}};
</script>
"""
    html = html.replace(script_match.group(0), bootstrap + f"<script>{script}</script>")
    html = html.replace(style_match.group(0), f"<style>{style}</style>")
    html = re.sub(r'<link rel="modulepreload"[^>]*>\s*', "", html)
    html = html.replace("量化大势研判系统——研究决策支持工具", "风格主题研判——研究决策支持工具")
    html = html.replace("<title>量化大势研判</title>", "<title>风格主题研判</title>")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"{OUTPUT} ({OUTPUT.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
