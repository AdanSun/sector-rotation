# 量化大势研判复现

本项目复现民生证券《量化大势研判：产业周期变革与资产全局比较》，主线为：

`Dickinson 生命周期 → 行业因子 → 估值与红利 → 资产选择信号 → 决策树回测`

## 目录约定

- `src/`：模型与回测代码。
- `data/raw/`：原始 Parquet 缓存，可用于离线复现。
- `data/processed/`：可重建的中间面板和明细数据，主要供程序读取。
- `output/figures/`：仅保留核心图。
- `output/tables/`：仅保留适合人工阅读的汇总表。
- `tests/`：核心口径和模型工具测试。

`output/` 是精简交付层，不再重复存放月度持仓、逐月回归和状态明细；这些数据统一存在 `data/processed/` 的 Parquet 文件中。

## 环境与取数

1. 创建 `.env` 并填写只读数据库账号：

   ```text
   FINANCEDATA_HOST=127.0.0.1
   FINANCEDATA_PORT=3306
   FINANCEDATA_USER=...
   FINANCEDATA_PASSWORD=...
   FINANCEDATA_DATABASE=financedata
   # 可选：国金库与上述配置不同时单独设置
   GUOJIN_HOST=...
   GUOJIN_PORT=3306
   GUOJIN_USER=...
   GUOJIN_PASSWORD=...
   GUOJIN_DATABASE=...
   # 2=申万二级展示，3=申万三级展示
   INDUSTRY_OUTPUT_LEVEL=2
   ```

2. 安装依赖并取数：

   ```bash
   pip install -r requirements.txt
   python run.py --step inventory
   python run.py --step extract
   python -m src.validate_data
   ```

仅需使用现有本地缓存时，无需连接数据库。`extract` 默认执行增量合并：日频数据从缓存最大日期续拉，财报数据回看至少 24 个月，行业归属等小型快照整体刷新。`--overwrite` 仍表示强制全量重拉。数据库模块只允许 `SELECT` 和 `SHOW` 查询。

增量提取后会自动清理可重建的 `data/processed/` 缓存，防止新 raw 数据与旧模型面板混用。数据覆盖与结构验证结果分别见 `output/tables/data_coverage.csv` 和 `data_validation.csv`。

## 行业展示层级

内部因子、信号和回测始终使用 hybrid 行业键 `industry_old_code`。最终面板和持仓额外提供：

- `display_industry_code`
- `display_industry_name`

默认是申万二级展示。可在单次运行时切换为三级：

```bash
python -m src.strategy --industry-output-level 3
python -m src.backtest --industry-output-level 3
```

展示层级只改变新增列，不会改变持仓权重、行业去重键或回测指标。

## 复现流程

按以下顺序运行：

```bash
python -m src.lifecycle --overwrite
python -m src.factors --overwrite
python -m src.plots
python -m src.valuation --overwrite
python -m src.valuation_regression --overwrite
python -m src.dividend_assets --overwrite
python -m src.strategy --overwrite
python -m src.backtest --overwrite
```

关键口径：

- 生命周期使用 CFO / CFI / CFF 符号的 Dickinson 分类，并使用公告日控制可得性。
- 行业默认使用申万三级为主、小样本回收到二级的 hybrid 口径。
- 因子按月末历史行业归属、自由流通市值加权。
- PB-ROE 和 PE-g 按月度截面、分生命周期带截距 OLS 检验。
- 回测在月末形成信号，使用下一月申万行业指数收益，并扣除配置的交易成本。

## 核心交付物

核心图：

- `fig4_lifecycle_growth.png` 至 `fig6_lifecycle_dividend.png`：生命周期与成长、ROE、股息率。
- `fig13_lifecycle_return_decomposition.png`：生命周期收益分解。
- `fig19_pe_g_extended_r2.png`：PE-g 扩展模型解释度。
- `fig22_quality_value_dividend_nav.png`：质量/价值红利净值。
- `fig32_strategy_signal_nav_compare.png`：子策略净值对比。
- `fig33_asset_decision_tree.png` 至 `fig35_asset_comparison_annual_returns.png`：决策树与最终回测。

核心表：

- `lifecycle_future_factor_stats.csv`
- `lifecycle_return_decomposition.csv`
- `valuation_regression_summary.csv`
- `dividend_asset_yearly_returns.csv`
- `strategy_signal_summary.csv`
- `backtest_annual_returns.csv`
- `backtest_metrics.csv`

## 测试

```bash
pytest -q
```

当更改生命周期映射、收益分解、回归或回测口径时，应同步增加对应测试。

---

# Web 系统（量化大势研判仪表盘）

在量化模型产物之上构建的只读投研仪表盘：把 `data/processed/` 与 `output/tables/` 转化为
可阅读、可筛选、可追溯的月度研判产品。定位为“研究决策支持工具”，不接入下单、账户或实时行情。

架构：**静态快照 + React 18 / TypeScript / Vite**。

- 数据源 = **国金数据库**（阿里云 RDS，表每日更新）。
- 数据流向 = 国金库 → `scripts/refresh_web_data.sh`（增量提取 + 重算模型 + 导出快照）→
  `frontend/public/data/*.json` → 前端直接读取。
- 前端为**纯静态站点**：构建后任意静态托管即可打开，不依赖任何运行中的 API 服务。

## 数据刷新（月度/定期更新）

每次运行会依次：① 从国金库增量提取最新数据到 `data/raw/`；② 按模型流水线重算
`data/processed/`；③ 导出前端 JSON 快照到 `frontend/public/data/`。

```bash
# 完整刷新（推荐每月末执行一次；耗时取决于增量大小，首次全量约 30-60 分钟）
./scripts/refresh_web_data.sh

# 只重算模型并导出（不连库，使用现有 data/raw 缓存）
./scripts/refresh_web_data.sh --skip-extract

# 仅重新导出快照（不连库、不重算模型）
./scripts/refresh_web_data.sh --skip-model
```

刷新完成后，重新构建前端（或直接刷新已打开页面）即可看到最新数据。

**动态数据截止（活数据）**：

- extract 会先探测国金库关键表的最新日期（行情/指数/财报/一致预期），增量只拉取新数据，
  并把最新日期写入 `data/raw/.latest_end_date.txt`。
- `config.END_DATE` 自动跟随该文件（取数据完整覆盖的月末），因此每次刷新后
  模型输出月份会自动推进（例如 2026-08 刷新后最新研判月为 2026-07-31），
  无需手工修改任何日期。可用环境变量 `DATA_END_DATE` 显式覆盖。
- 刷新链路中模型覆盖输出时保留中间图（`SKIP_FIG_CLEANUP=1`），不影响任何模型计算口径。

## 快速启动（纯静态）

前置：Python 3.11+、Node 18+。

```bash
# 1. 后端依赖（导出/刷新脚本需要）
pip install -r requirements.txt

# 2. 前端依赖
cd frontend && npm install && cd ..

# 3. 生成数据快照（首次必须；此后每月刷新一次）
./scripts/refresh_web_data.sh --skip-extract   # 使用现有 raw 缓存生成快照
# 或完整刷新：./scripts/refresh_web_data.sh

# 4. 启动前端（开发模式）
cd frontend && npm run dev        # http://127.0.0.1:5174

# 生产构建 + 预览（构建产物在 frontend/dist/，可整体静态部署）
cd frontend && npm run build && npm run preview   # http://127.0.0.1:4174
```

## 数据快照结构

`frontend/public/data/` 由 `scripts/export_web_data.py` 生成（Vite 构建时自动打包）：

| 文件 | 内容 |
|---|---|
| `meta.json` | 可用月份、子策略、资产类型、生命周期、展示层级 |
| `overview.json` | 最新月份整体研判（结论由既有状态确定性模板生成） |
| `recommendations.json` | 全量子策略推荐原始表，前端本地筛选/聚合/层级切换 |
| `decision_history.json` | 月度资产类别决策状态与分支可用性 |
| `holdings.json` | 最终策略全量月度行业持仓与权重 |
| `performance_final.json` | 最终策略净值/回撤/年度收益/绩效指标 |
| `performance_sub.json` | 全部子策略月度序列与指标 |
| `data_status.json` | 数据集状态、日期滞后、验证信息、模型流水线 |
| `industry/{code}_{level}.json` | 每个展示行业（申万二级 + 三级）预计算详情 |
| `industry_index.json` | 行业索引与生命周期配色 |

口径说明：快照由 `src/web_data/services.py` 的本地派生逻辑预计算，不依赖 Web API；比率字段为小数、
NaN/Inf 一律转 `null`；展示层级仅派生展示列，不改动 `industry_old_code`。

## 页面

| 页面 | 路由 | 内容 |
|---|---|---|
| 研判总览 | `/` | 当前资产类别、决策依据、Top 推荐、增长/ROE/拥挤度、净值与资产时间线、数据滞后横幅 |
| 行业推荐 | `/recommendations` | 月份选择、申万二级/三级、明细/聚合切换、策略/资产类型/生命周期筛选、可排序表格、CSV 导出 |
| 资产决策 | `/decisions` | 决策树路径（基于模型状态渲染）、当月持仓与权重、历史资产类别时间线 |
| 策略表现 | `/performance` | 策略 vs 基准净值、回撤、年度收益、子策略对比、绩效指标、区间选择 |
| 行业详情 | `/industry/:code` | 生命周期带状图、成长/ROE/股息率/估值趋势、入选历史、聚合视图 hybrid 明细 |
| 数据与模型状态 | `/data-status` | 数据集状态表、最新日期可视化、缺失分区、模型流水线 |

全局顶部常驻：研判月份、展示层级、数据状态；支持浅色/深色模式；查询条件反映在 URL 中。

## 测试与构建

```bash
# 后端测试（无需数据库/外网）
pytest -q

# 前端测试
cd frontend && npm test -- --run

# 前端生产构建
cd frontend && npm run build
```

## 数据口径与已知缺口

- 模型最新月份随刷新时间变化（信号形成月为每月月末，由国金库最新数据的完整月末决定）。
  当月下月收益未发生，快照一律不返回未来收益。
- 行情数据在国金库中持续更新（例如 2026-08 已到当月中旬）；财报/分红/指数/一致预期
  按披露节奏更新。刷新后若某类数据仍停留在更早日期，前端总览与数据状态页会明确展示该差异，
  不会统一显示“数据更新到最新”。当前口径示例：行情/一致预期/指数到 2026-08-14，
  财报到 2026-06-30（2026 中报陆续披露中），分红到 2026-07-15。
- 展示层级（申万二级/三级）仅派生展示列，不改动 `industry_old_code`，不改变回测指标。
- 决策树路径由离线模型面板渲染，前端不重算策略。
- `config.END_DATE` 动态跟随国金库数据（见“数据刷新”一节），只影响模型覆盖的时间范围，
  不改变任何模型计算口径。

## 故障排查

- 前端提示“数据快照缺失”：尚未运行刷新脚本，或 `frontend/public/data/` 为空。
  执行 `./scripts/refresh_web_data.sh --skip-extract` 生成快照。
- 刷新失败：查看终端输出的失败步骤；extract 需要 `.env` 中的国金库只读账号可用。
- 数据滞后于预期：国金库各表更新时间不同，行情快、基本面慢属正常；重新运行完整刷新即可。
- 端口占用：前端 `5173`（预览 `4173`），可在 vite.config.ts 调整。
