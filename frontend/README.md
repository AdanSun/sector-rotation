# 风格主题研判前端

这是基于 React + TypeScript + Vite 重建的前端源码。页面直接读取本地
`public/data/` 下的静态数据快照，因此不依赖 FastAPI 或任何公司 API。

开发启动：

```bash
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5174
```

然后打开 `http://127.0.0.1:5174`。

生产构建：

```bash
npm run build
npm run preview -- --host 127.0.0.1 --port 4174
```

构建产物位于 `dist/`，可作为独立静态网站发布。

更新模型后，在项目根目录运行以下任一命令：

macOS / Linux：

```bash
./scripts/refresh_web_data.sh
```

Windows PowerShell：

```powershell
python scripts\refresh_web_data.py
```

已有 raw 缓存、无需连接数据库时，在相应命令后添加 `--skip-extract`。

```bash
./scripts/refresh_web_data.sh --skip-extract
```

刷新脚本会导出 `frontend/public/data/`，开发页面和生产构建都会读取这些数据。

`data/` 保留为兼容旧静态发布版的数据快照；请勿手工修改其中任一目录的数据，统一通过刷新脚本生成。
