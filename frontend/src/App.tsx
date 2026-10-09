import { useEffect, useState } from "react";
import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";

type Row = Record<string, unknown>;
const DATA = `${import.meta.env.BASE_URL}data`;
const pct = (v: unknown, digits = 2) => typeof v === "number" && Number.isFinite(v) ? `${(v * 100).toFixed(digits)}%` : "—";
const month = (v: unknown) => typeof v === "string" ? v.slice(0, 7) : "—";
const nextMonth = (value: string) => { const d = new Date(`${value.slice(0, 7)}-01T00:00:00Z`); d.setUTCMonth(d.getUTCMonth() + 1); return d.toISOString().slice(0, 7); };
const read = async <T,>(name: string): Promise<T> => { const response = await fetch(`${DATA}/${name}`); if (!response.ok) throw new Error(`无法读取 ${name}`); return response.json() as Promise<T>; };

function useData<T>(name: string) {
  const [data, setData] = useState<T | null>(null); const [error, setError] = useState("");
  useEffect(() => { read<T>(name).then(setData).catch((e: Error) => setError(e.message)); }, [name]);
  return { data, error };
}

function Shell({ children }: { children: React.ReactNode }) {
  const meta = useData<Row>("meta.json"); const location = useLocation();
  const nav = [["/", "◉", "风格主题研判总览"], ["/decisions", "◇", "风格主题历史研判"], ["/performance", "📈", "策略表现"], ["/data-status", "▦", "数据底稿"]];
  const latest = String(meta.data?.latest_month ?? "—");
  return <div className="shell"><aside><div className="brand"><b>研</b><div><strong>风格主题研判</strong><small>研究决策支持</small></div></div><nav>{nav.map(([to, icon, label]) => <Link key={to} className={location.pathname === to ? "active" : ""} to={to}><span>{icon}</span>{label}</Link>)}</nav><footer>数据截止：{month(latest)}<br />仅供研究，不构成投资建议</footer></aside><main><header><strong>风格主题研判</strong><div className="chips"><span>信号月 <b>{month(latest)}</b></span><span>对应配置月 <b>{latest === "—" ? "—" : nextMonth(latest)}</b></span><span>申万2级</span></div></header><div className="content">{meta.error ? <ErrorBox message={meta.error} /> : children}</div></main></div>;
}

function Card({ title, hint, children }: { title?: string; hint?: string; children: React.ReactNode }) { return <section className="card">{title && <h2>{title}{hint && <small>{hint}</small>}</h2>}{children}</section>; }
function ErrorBox({ message }: { message: string }) { return <div className="error">{message}</div>; }
function Loading() { return <div className="loading">正在加载数据…</div>; }
function Tags({ names }: { names: string[] }) { return <div className="tags">{names.map((x) => <span key={x}>{x}</span>)}</div>; }

function Overview() {
  const o = useData<Row>("overview.json"); const rec = useData<Row[]>("recommendations.json");
  if (o.error || rec.error) return <ErrorBox message={o.error || rec.error} />; if (!o.data || !rec.data) return <Loading />;
  const recommendations = rec.data;
  const signal = String(o.data.as_of_date); const trends = (o.data.trends ?? {}) as Row[] & Row;
  const active = new Set<string>(); if (trends.actual_growth_available) active.add("实际成长"); if (trends.expected_growth_available) active.add("预期成长"); if (trends.roe_available) active.add("盈利能力");
  const order = ["实际成长", "预期成长", "盈利能力", "质量红利", "价值红利"];
  const strategy = [{ id: "event_composite", name: "实际成长", logic: "SUE+SUR+JUMP" }, { id: "consensus_growth", name: "预期成长", logic: "分析师一致预期" }, { id: "roe_asset", name: "盈利能力", logic: "PB-ROE" }, { id: "quality_dividend", name: "质量红利", logic: "DP+ROE" }, { id: "value_dividend", name: "价值红利", logic: "DP+BP" }].sort((a, b) => Number(active.has(b.name)) - Number(active.has(a.name)));
  return <><div className="page-title"><div><h1>风格主题研判总览</h1><p>自下而上风格主题研判模型</p></div><b>信号月 / 对应配置月<br />{month(signal)} / {nextMonth(signal)}</b></div><Card title="综合判断占优风格"><Tags names={order.map((x) => `${active.has(x) ? "●" : "○"} ${x}`)} /><ul><li>基于行业生命周期、成长、盈利、估值与红利特征进行横向比较。</li><li>本月占优风格：{[...active].join(" + ") || "无"}。</li></ul></Card><Card title="占优风格研判" hint={`信号月 ${month(signal)}，对应配置月 ${nextMonth(signal)}`}><div className="table-wrap"><table><thead><tr><th>风格</th><th>筛选逻辑</th><th colSpan={5}>当下推荐（申万二级行业）</th></tr></thead><tbody>{strategy.map((s) => { const picks = recommendations.filter((r) => r.month === signal && r.strategy_id === s.id).slice(0, 5); return <tr className={active.has(s.name) ? "emphasis" : ""} key={s.id}><td><b>{s.name}</b><small>{active.has(s.name) ? "本月占优" : "当前未占优"}</small></td><td>{s.logic}</td>{Array.from({ length: 5 }, (_, i) => <td key={i}>{picks[i] ? <><b>{String(picks[i].display_industry_name)}</b><small>{String(picks[i].lifecycle_stage_zh ?? "")}</small></> : "—"}</td>)}</tr>; })}</tbody></table></div></Card><Card title="组合构建说明"><ol><li>在占优风格下分别选择 Top 5 行业，去重后等权配置。</li><li>若三类主流风格均不成立，则使用质量红利或价值红利分支。</li></ol></Card></>;
}

function Decisions() {
  const meta = useData<Row>("meta.json"); const history = useData<Row>("decision_history.json"); const holdings = useData<Row[]>("holdings.json");
  const [selected, setSelected] = useState(""); if (meta.error || history.error || holdings.error) return <ErrorBox message={meta.error || history.error || holdings.error} />; if (!meta.data || !history.data || !holdings.data) return <Loading />;
  const items = Array.isArray(history.data.items) ? history.data.items as Row[] : []; const selectedMonth = selected || String(meta.data.latest_month); const current = items.find((x) => x.month === selectedMonth); const picks = holdings.data.filter((x) => x.month === selectedMonth);
  return <><div className="page-title"><div><h1>风格主题历史研判</h1><p>模型月末研判与后续实际表现对比</p></div></div><Card title="所选月份行业推荐"><label>信号月 <select value={selectedMonth} onChange={(e) => setSelected(e.target.value)}>{(meta.data.months as string[]).slice().reverse().map((m) => <option key={m} value={m}>{month(m)}</option>)}</select></label><span className="muted">对应配置月：{nextMonth(selectedMonth)} · {String(current?.asset_name ?? "—")}</span><div className="table-wrap"><table><thead><tr><th>排名</th><th>推荐行业</th><th>所属资产</th><th>权重</th><th>得分</th></tr></thead><tbody>{picks.map((p) => <tr key={`${p.rank}-${p.industry_old_code}`}><td>{String(p.rank)}</td><td>{String(p.display_industry_name)}</td><td>{String(p.asset_name ?? "—")}</td><td>{pct(p.weight)}</td><td>{typeof p.score === "number" ? p.score.toFixed(3) : "—"}</td></tr>)}</tbody></table></div></Card><Card title="历史风格研判"><div className="table-wrap"><table><thead><tr><th>信号月</th><th>对应配置月</th><th>资产类别</th><th>研判观点</th></tr></thead><tbody>{items.slice().reverse().map((x) => <tr key={String(x.month)}><td>{month(x.month)}</td><td>{nextMonth(String(x.month))}</td><td>{String(x.asset_name)}</td><td>{String(x.reason ?? "—")}</td></tr>)}</tbody></table></div></Card></>;
}

function Performance() {
  const p = useData<Row>("performance_final.json"); const [year, setYear] = useState(""); if (p.error) return <ErrorBox message={p.error} />; if (!p.data) return <Loading />;
  const rows: Row[] = ((p.data.monthly ?? []) as Row[]).map((x) => ({ ...x, actual_month: nextMonth(String(x.month)) })); const settled = rows.filter((x) => x.strategy_return !== null); const years = [...new Set(settled.map((x) => String(x.actual_month).slice(0, 4)))]; const shown = settled.filter((x) => !year || String(x.actual_month).startsWith(year)); const metrics = (p.data.metrics ?? []) as Row[];
  return <><div className="page-title"><div><h1>策略表现</h1><p>实际收益月份口径 · 仅供研究，不构成投资建议</p></div></div><div className="metrics">{metrics.filter((x) => ["annual_return", "annual_volatility", "max_drawdown", "sharpe_like", "calmar_like", "win_rate", "avg_turnover", "months"].includes(String(x.key))).map((x) => <Card key={String(x.key)} title={String(x.label)} hint={String(x.description)}><strong className="metric">{x.key === "months" ? String(x.value) : ["sharpe_like", "calmar_like"].includes(String(x.key)) ? Number(x.value).toFixed(2) : pct(x.value)}</strong>{x.benchmark_value !== null && x.benchmark_value !== undefined && <small>Wind全A {pct(x.benchmark_value)}</small>}</Card>)}</div><Card title="每月策略表现汇总" hint="均为当月实际收益，非年化口径"><label>年份 <select value={year} onChange={(e) => setYear(e.target.value)}><option value="">全部</option>{years.map((y) => <option key={y}>{y}</option>)}</select></label><div className="table-wrap"><table><thead><tr><th>实际收益月份</th><th>策略当月收益</th><th>Wind全A当月收益</th><th>当月超额收益</th><th>策略回撤</th><th>月度换手</th></tr></thead><tbody>{shown.map((x) => <tr key={String(x.month)}><td>{month(x.actual_month)}</td><td>{pct(x.strategy_return)}</td><td>{pct(x.benchmark_return)}</td><td>{typeof x.strategy_return === "number" && typeof x.benchmark_return === "number" ? pct(x.strategy_return - x.benchmark_return) : "—"}</td><td>{pct(x.drawdown)}</td><td>{pct(x.turnover)}</td></tr>)}</tbody></table></div></Card></>;
}

function DataStatus() { const s = useData<Row>("data_status.json"); if (s.error) return <ErrorBox message={s.error} />; if (!s.data) return <Loading />; const sets = (s.data.datasets ?? []) as Row[]; return <><div className="page-title"><div><h1>数据与模型状态</h1><p>模型最新月份：{month(s.data.latest_model_month)} · 生成于 {String(s.data.generated_at ?? "—")}</p></div><a className="button" href={`${DATA}/source_draft.xlsx`}>下载数据底稿 XLSX</a></div><Card title="数据集状态"><div className="table-wrap"><table><thead><tr><th>数据集</th><th>状态</th><th>最新月份</th><th>行数</th><th>说明</th></tr></thead><tbody>{sets.map((x) => <tr key={String(x.key)}><td>{String(x.label ?? x.key)}</td><td>{String(x.status ?? "—")}</td><td>{month(x.latest_month)}</td><td>{String(x.rows ?? "—")}</td><td>{String(x.message ?? "—")}</td></tr>)}</tbody></table></div></Card></>; }

export default function App() { return <Shell><Routes><Route path="/" element={<Overview />} /><Route path="/decisions" element={<Decisions />} /><Route path="/performance" element={<Performance />} /><Route path="/data-status" element={<DataStatus />} /><Route path="*" element={<Navigate to="/" replace />} /></Routes></Shell>; }
