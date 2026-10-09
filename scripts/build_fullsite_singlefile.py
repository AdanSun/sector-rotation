"""把 vite --mode singlefile 构建产物 + 全量数据快照合并为一个离线单文件 HTML。

与 export_standalone_html.py（仅总览页、__SNAPSHOT__ 内联 8 个 JSON）不同，本脚本：
- 覆盖全站全部路由（总览 / 行业推荐 / 资产决策 / 策略表现 / 行业详情 / 数据状态）；
- 用 fetch 拦截方式内联 dist/data/ 下全部文件（含 840 个行业详情 JSON 与底稿 xlsx），
  gzip+base64 存储，最终单文件约 25-30 MB；
- 内联主 JS（单 IIFE chunk）与 CSS，脚本置于 </body> 前（避免 createRoot 时 DOM 未就绪）；
- 拦截「下载底稿 Excel」等静态下载链接，从内联数据生成 Blob 下载。

用法：
  cd frontend && npx vite build --mode singlefile
  python scripts/build_fullsite_singlefile.py
输出：share/风格主题研判-全站单文件.html（双击即开、零外链、零联网）
"""

from __future__ import annotations

import base64
import gzip
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIST = PROJECT_ROOT / "frontend" / "dist"
OUTPUT = PROJECT_ROOT / "share" / "风格主题研判-全站单文件.html"

MIME = {
    ".json": "application/json; charset=utf-8",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}

BOOTSTRAP = """<script>
window.addEventListener('error',function(event){
  var el=document.getElementById('root');
  if(el && !el.hasChildNodes()){
    el.innerHTML='<pre style="padding:24px;white-space:pre-wrap;font:14px/1.6 sans-serif;color:#9b1c1c">页面启动失败：'+String(event.message||'未知错误')+'</pre>';
  }
});
window.__FILES__=__FILES_PAYLOAD__;
(function(){
  var nativeFetch=window.fetch?window.fetch.bind(window):null;
  function fileKey(raw){
    if(typeof raw!=='string')return null;
    var m=raw.match(/(?:data|assets)\\/.+?(?=[?#]|$)/);
    return m?m[0]:null;
  }
  window.fetch=function(input,init){
    var key=fileKey(typeof input==='string'?input:(input&&input.url)||'');
    var item=key?window.__FILES__[key]:null;
    if(!item)return nativeFetch?nativeFetch(input,init):Promise.reject(new Error('offline: '+key));
    var bytes=Uint8Array.from(atob(item.data),function(c){return c.charCodeAt(0);});
    if(item.kind==='gzip'){
      var stream=new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
      return Promise.resolve(new Response(stream,{status:200,headers:{'Content-Type':item.mime}}));
    }
    return Promise.resolve(new Response(bytes,{status:200,headers:{'Content-Type':item.mime}}));
  };
  /* 拦截静态下载链接（如「下载底稿 Excel」），从内联数据生成 Blob */
  document.addEventListener('click',function(e){
    var a=e.target&&e.target.closest?e.target.closest('a[href]'):null;
    if(!a)return;
    var key=fileKey(a.getAttribute('href'));
    var item=key?window.__FILES__[key]:null;
    if(!item)return;
    e.preventDefault();
    var bytes=Uint8Array.from(atob(item.data),function(c){return c.charCodeAt(0);});
    var url=URL.createObjectURL(new Blob([bytes],{type:item.mime}));
    var tmp=document.createElement('a');
    tmp.href=url;
    tmp.download=key.split('/').pop();
    document.body.appendChild(tmp);tmp.click();tmp.remove();
    setTimeout(function(){URL.revokeObjectURL(url);},4000);
  },true);
})();
</script>"""


def main() -> None:
    html = (DIST / "index.html").read_text(encoding="utf-8")

    # 1) 找到主 JS chunk（singlefile 模式只会有一个）
    script_match = re.search(
        r'<script[^>]*src="\./(assets/index-[^"]+\.js)"[^>]*></script>', html
    )
    if not script_match:
        raise RuntimeError("未找到 Vite 主脚本，请先执行: cd frontend && npx vite build --mode singlefile")
    css_match = re.search(r'<link rel="stylesheet"[^>]*href="\./([^"]+)"[^>]*>', html)
    if not css_match:
        raise RuntimeError("未找到样式表")

    # 2) 内联 CSS（从 head 中移除 link，稍后插入 style）
    css = (DIST / css_match.group(1)).read_text(encoding="utf-8")
    html = re.sub(r'<link rel="stylesheet"[^>]*href="\./[^"]+"[^>]*>', "", html, count=1)

    # 3) 移除主脚本与 modulepreload（JS 稍后放 </body> 前）
    js_path = DIST / script_match.group(1)
    html = re.sub(r'<script[^>]*src="\./assets/index-[^"]+\.js"[^>]*></script>', "", html, count=1)
    html = re.sub(r'<link rel="modulepreload"[^>]*>\s*', "", html)

    # 4) 收集全部数据文件（JSON → gzip+base64；二进制 → base64）
    files: dict[str, dict[str, str]] = {}
    for path in sorted((DIST / "data").rglob("*")):
        if not path.is_file():
            continue
        key = "data/" + path.relative_to(DIST / "data").as_posix()
        raw = path.read_bytes()
        mime = MIME.get(path.suffix.lower(), "application/octet-stream")
        if path.suffix.lower() == ".json":
            files[key] = {
                "kind": "gzip",
                "mime": mime,
                "data": base64.b64encode(gzip.compress(raw, compresslevel=9)).decode("ascii"),
            }
        else:
            files[key] = {"kind": "base64", "mime": mime, "data": base64.b64encode(raw).decode("ascii")}
    # 静态图片（如有 <img> 引用也一并兜底）
    for path in sorted((DIST / "assets").glob("*.png")):
        files["assets/" + path.name] = {
            "kind": "base64",
            "mime": "image/png",
            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
        }

    payload = json.dumps(files, ensure_ascii=False, separators=(",", ":"))
    bootstrap = BOOTSTRAP.replace("__FILES_PAYLOAD__", payload)

    # 5) JS 内联（IIFE，普通 script 即可）；防止内文出现 </script 截断
    js = js_path.read_text(encoding="utf-8")
    n_break = len(re.findall(r"</script", js, flags=re.I))
    if n_break:
        js = re.sub(r"</script", r"<\\/script", js, flags=re.I)
    html = html.replace(
        "</head>", f"<style>{css}</style>\n</head>"
    ).replace(
        "</body>", bootstrap + "\n<script>" + js + "</script>\n</body>"
    )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(html, encoding="utf-8")
    data_mb = sum(len(v["data"]) for v in files.values()) / 1024 / 1024
    print(f"内联数据文件 {len(files)} 个（压缩后 {data_mb:.1f} MB base64）；</script 冲突 {n_break} 处")
    print(f"已输出：{OUTPUT}（{OUTPUT.stat().st_size / 1024 / 1024:.1f} MB，单文件自包含）")


if __name__ == "__main__":
    main()
