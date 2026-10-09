完整项目目录结构(核心代码分布)
0709自下而上大势研判/
├─ src/              ← 【后端量化研究核心】11 个模块,数据→因子→策略→回测
├─ src/web_data/     ← 【网页数据派生】本地读取 Parquet 并导出静态 JSON，不启动 API 服务
├─ scripts/          ← 运维脚本(如 refresh_web_data.py 刷新数据快照)
├─ frontend/         ← 【静态网页发布版】构建资源 + 数据快照，可直接打开
├─ config.py         ← 全局配置(日期、路径、阈值)
└─ run.py            ← 项目一键入口(调 src.database.main)


src/ 下的核心模块
数据 → 因子 → 模型 → 策略 → 回测 → 输出 量化研究流水线:
模块	职责	流水线阶段
database.py	Wind/国金库只读取数 → Parquet	① 数据采集
validate_data.py	数据完整性校验	① 数据校验
lifecycle.py	Dickinson 现金流法产业生命周期	② 因子/分类
factors.py	量价/财务因子计算	② 因子计算
valuation.py	估值与收益分解	② 估值口径
valuation_regression.py	估值回归模型	② 模型
strategy.py	行业推荐策略打分	③ 策略合成
dividend_assets.py	红利资产组合	③ 策略合成
backtest.py	回测引擎、净值/回撤/换手	④ 回测
output_industry_map.py	内部代码 → 展示代码映射	⑤ 输出
plots.py	图表/快照生成	⑤ 输出
