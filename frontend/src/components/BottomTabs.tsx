import { useEffect, useState } from "react";
import { get, post } from "../api";
import { ago, fmtNum, fmtPct, fmtPrice, fmtTime, sid } from "../format";
import type { Account, CalEvent, Coupling, MoveEvent, NewsItem, SignalJ, StrategyInfo, TrendBoard } from "../types";
import { couplingClass } from "./bits";

type Tab = "signals" | "trends" | "radar" | "news" | "positions" | "strategies" | "coupling";
const TABS: [Tab, string][] = [["signals", "Signals"], ["trends", "Trend Board"], ["radar", "Pump/Dump Radar"], ["news", "News & Events"],
  ["positions", "Paper positions"], ["strategies", "Strategies & stats"], ["coupling", "BTC coupling"]];

interface Props {
  signals: SignalJ[]; radar: MoveEvent[]; news: NewsItem[]; events: CalEvent[]; account: Account | null;
  coupling: Coupling[]; onSelect: (s: string) => void; notify: (m: string, bad?: boolean) => void;
}

export default function BottomTabs(p: Props) {
  const [tab, setTab] = useState<Tab>(() => (localStorage.getItem("tab") as Tab) || "signals");
  useEffect(() => { try { localStorage.setItem("tab", tab); } catch { /* ignore */ } }, [tab]);
  const counts: Partial<Record<Tab, number>> = {
    signals: p.signals.filter((s) => ["pending", "triggered", "tp1_hit"].includes(s.status)).length,
    radar: p.radar.filter((e) => e.stage && e.stage !== "WATCH").length,
    positions: p.account?.open.length,
  };
  return (
    <div style={{ display: "grid", gridTemplateRows: "auto 1fr", minHeight: 0 }}>
      <div className="tabs">
        {TABS.map(([k, label]) => (
          <button key={k} className={tab === k ? "active" : ""} onClick={() => setTab(k)}>
            {label}{counts[k] ? ` (${counts[k]})` : ""}
          </button>
        ))}
      </div>
      <div className="tab-body">
        {tab === "signals" && <Signals {...p} />}
        {tab === "trends" && <Trends onSelect={p.onSelect} />}
        {tab === "radar" && <Radar radar={p.radar} onSelect={p.onSelect} />}
        {tab === "news" && <News news={p.news} events={p.events} onSelect={p.onSelect} />}
        {tab === "positions" && <Positions account={p.account} notify={p.notify} onSelect={p.onSelect} />}
        {tab === "strategies" && <Strategies />}
        {tab === "coupling" && <CouplingTable rows={p.coupling} onSelect={p.onSelect} />}
      </div>
    </div>
  );
}

function Signals({ signals, onSelect, notify }: Props) {
  const take = async (s: SignalJ) => {
    try { await post(`/api/orders/from-signal/${s.id}`); notify(`Paper position opened from ${sid(s.strategy)} ${s.symbol}`); }
    catch (e) { notify(String(e).replace("Error: ", ""), true); }
  };
  if (!signals.length) return <div className="empty">No signals yet. Signals appear when a strategy setup passes the vetoes (spread, news, macro, costs, regime), confluence and — once statistics exist — the probability and expected-value checks.</div>;
  return (
    <table className="t">
      <thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Strategy</th><th>TF</th><th className="right">Entry</th><th className="right">Stop</th><th className="right">Targets</th>
        <th className="right">P(win)</th><th className="right">EV</th><th className="right">Conf.</th><th>Status</th><th>Reasons</th><th /></tr></thead>
      <tbody>
        {signals.map((s) => {
          const active = ["pending", "triggered", "tp1_hit"].includes(s.status);
          return (
            <tr key={s.id} className="click" onClick={() => onSelect(s.symbol)}>
              <td className="mono">{fmtTime(s.ts)}</td><td><b>{s.symbol}</b></td>
              <td className={s.side === "LONG" ? "up" : "dn"}>{s.side}</td>
              <td title={s.strategy}>{sid(s.strategy)}</td><td>{s.tf}</td>
              <td className="right mono">{fmtPrice(s.entry.price)}</td><td className="right mono">{fmtPrice(s.stop)}</td>
              <td className="right mono">{s.targets.map((t) => fmtPrice(t.price)).join(" / ")}</td>
              <td className="right mono" title={s.p_win_ci ? `90% CI ${Math.round(s.p_win_ci[0] * 100)}–${Math.round(s.p_win_ci[1] * 100)}% · ${s.probability_source}` : s.probability_source}>
                {s.p_win !== null ? `${Math.round(s.p_win * 100)}%` : <span className="faint">unvalidated</span>}</td>
              <td className="right mono">{s.expected_R !== null ? `${fmtNum(s.expected_R, 2)}R` : "—"}</td>
              <td className="right mono" title={Object.entries(s.confluence_parts).map(([k, v]) => `${k}: ${v}`).join("\n")}>{s.confluence}</td>
              <td>{s.status}{s.outcome_r !== null && <span className={s.outcome_r > 0 ? "up" : "dn"}> {fmtNum(s.outcome_r, 2)}R</span>}</td>
              <td className="ellipsis" style={{ maxWidth: 320 }} title={[...s.reasons, ...s.risks.map((r) => "risk: " + r)].join("\n")}>{s.reasons.join(" · ")}</td>
              <td>{active && <button onClick={(e) => { e.stopPropagation(); take(s); }}>Take (paper)</button>}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function Trends({ onSelect }: { onSelect: (s: string) => void }) {
  const [b, setB] = useState<TrendBoard | null>(null);
  const [sort, setSort] = useState<"newest" | "alignment">("newest");
  useEffect(() => {
    const load = () => get<TrendBoard>("/api/trends").then(setB).catch(() => undefined);
    load();
    const t = window.setInterval(load, 10000);
    return () => window.clearInterval(t);
  }, []);
  if (!b) return <div className="empty">Loading…</div>;
  const rows = [...b.rows].sort((x, y) => (sort === "newest" ? y.newest - x.newest : y.alignment - x.alignment));
  return (
    <div>
      <div className="row" style={{ padding: "4px 6px", gap: 10 }}>
        <span className="muted">Sort</span>
        <button className={sort === "newest" ? "active" : ""} onClick={() => setSort("newest")}>newest trend</button>
        <button className={sort === "alignment" ? "active" : ""} onClick={() => setSort("alignment")}>MTF alignment</button>
        <span className="grow" />
        {b.tfs.map((tf) => {
          const st = b.stats[tf];
          const c = st?.confirmed_reached_2atr;
          return st ? <span key={tf} className="chip" title="Since engine start (history + live): confirmed trends that extended ≥ 2 ATR beyond the confirmation price">
            {tf}: {st.confirmed} conf · {c && c.n ? `${Math.round((c.win_rate ?? 0) * 100)}% reached +2ATR` : "—"} · {st.false_starts} false</span> : null;
        })}
      </div>
      <table className="t">
        <thead><tr><th>Symbol</th>{b.tfs.map((tf) => <th key={tf}>{tf}</th>)}<th>Alignment</th><th>BTC coupling</th><th>Latest change</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.symbol} className="click" onClick={() => onSelect(r.symbol)}>
              <td><b>{r.symbol}</b></td>
              {b.tfs.map((tf) => {
                const c = r.cells[tf];
                const active = c.dir && c.state !== "NONE" && c.state !== "ENDED";
                return <td key={tf} style={{ background: active ? (c.dir > 0 ? `rgba(34,197,94,${c.state === "EARLY" ? 0.12 : 0.3})` : `rgba(239,68,68,${c.state === "EARLY" ? 0.12 : 0.3})`) : undefined }}>
                  {active ? `${c.dir > 0 ? "▲" : "▼"} ${c.state.slice(0, 4)}` : <span className="faint">·</span>} <span className="faint">{c.score}</span></td>;
              })}
              <td className={r.direction === "UP" ? "up" : r.direction === "DOWN" ? "dn" : "muted"}>{r.alignment}/{b.tfs.length} {r.direction ?? ""}</td>
              <td>{r.coupling && <span className={`badge ${couplingClass(r.coupling)}`}>{r.coupling}</span>}</td>
              <td className="muted">{r.newest ? ago(r.newest) + " ago" : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Radar({ radar, onSelect }: { radar: MoveEvent[]; onSelect: (s: string) => void }) {
  const [recent, setRecent] = useState<MoveEvent[]>([]);
  useEffect(() => { get<{ recent: MoveEvent[] }>("/api/radar").then((r) => setRecent(r.recent)).catch(() => undefined); }, [radar.length]);
  const active = radar.filter((e) => e.stage && e.stage !== "WATCH");
  const watch = radar.filter((e) => e.stage === "WATCH");
  const rows = [...active, ...recent.filter((r) => !active.some((a) => a.id === r.id))].slice(0, 80);
  return (
    <div>
      <table className="t">
        <thead><tr><th>Symbol</th><th>Type</th><th>Stage</th><th className="right">Move</th><th className="right">Now</th><th className="right">Vol ×</th>
          <th>Since ignition</th><th className="right">Chase limit</th><th>Class</th><th>Catalyst</th><th>Exhaustion / outcome</th></tr></thead>
        <tbody>
          {rows.map((e) => (
            <tr key={e.id} className="click" onClick={() => onSelect(e.symbol)} style={{ opacity: e.outcome ? 0.6 : 1 }}>
              <td><b>{e.symbol}</b></td>
              <td className={e.direction === "PUMP" ? "up" : "dn"}>{e.direction === "PUMP" ? "🚀 PUMP" : "💥 DUMP"}</td>
              <td>{e.stage}</td>
              <td className="right mono">{fmtPct(e.move_pct)}</td><td className="right mono">{fmtPct(e.current_pct)}</td>
              <td className="right mono">{e.vol_x}</td>
              <td className="muted">{e.ignition ? ago(e.ignition.ts) : "—"}</td>
              <td className="right mono">{fmtPrice(e.chase_limit)}</td>
              <td><span className={`badge ${e.classification === "organic" ? "follower" : e.classification === "suspicious" ? "decoupling" : ""}`}>{e.classification}</span></td>
              <td className="ellipsis" style={{ maxWidth: 220 }}>{e.catalyst ?? <span className="faint">none</span>}</td>
              <td className="ellipsis" style={{ maxWidth: 240 }}>{e.outcome ? `ended: ${e.outcome}` : e.exhaustion.join(", ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {!rows.length && <div className="empty">No abnormal moves detected yet. The radar scans every liquid USDT pair second by second.</div>}
      {watch.length > 0 && <div style={{ padding: 6 }} className="muted">Watching ({watch.length}): {watch.map((w) => <span key={w.symbol} className="chip" style={{ cursor: "pointer" }} onClick={() => onSelect(w.symbol)}>{w.symbol}</span>)}</div>}
    </div>
  );
}

function News({ news, events, onSelect }: { news: NewsItem[]; events: CalEvent[]; onSelect: (s: string) => void }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 320px", minHeight: "100%" }}>
      <table className="t">
        <thead><tr><th>Time</th><th>Source</th><th>Type</th><th>Coins</th><th className="right">Sent.</th><th className="right">Impact</th><th>Headline</th></tr></thead>
        <tbody>
          {news.map((n) => (
            <tr key={n.id}>
              <td className="mono muted">{ago(n.ts)}</td><td>{n.source}</td><td><span className="chip">{n.event_type}</span></td>
              <td>{n.symbols.map((s) => <span key={s} className="chip" style={{ cursor: "pointer" }} onClick={() => onSelect(`${s}USDT`)}>{s}</span>)}</td>
              <td className={`right mono ${n.sentiment > 0.15 ? "up" : n.sentiment < -0.15 ? "dn" : "muted"}`}>{fmtNum(n.sentiment, 2)}</td>
              <td className="right mono">{n.impact.toFixed(0)}</td>
              <td className="ellipsis" style={{ maxWidth: 520 }}><a href={n.url} target="_blank" rel="noreferrer">{n.title}</a></td>
            </tr>
          ))}
        </tbody>
      </table>
      <div style={{ borderLeft: "1px solid var(--border)" }}>
        <div className="panel-h">Economic calendar (next 48h)</div>
        {events.filter((e) => e.impact === "High" || e.impact === "Medium").map((e, i) => (
          <div key={i} style={{ padding: "3px 8px", borderBottom: "1px solid #1a2330" }}>
            <span className={e.impact === "High" ? "dn" : "warn"}>●</span> <b>{e.country}</b> {e.title}
            <div className="muted">{fmtTime(e.ts, true)}{e.forecast ? ` · f ${e.forecast}` : ""}{e.previous ? ` · p ${e.previous}` : ""}</div>
          </div>
        ))}
        {!events.length && <div className="empty">No events loaded.</div>}
      </div>
    </div>
  );
}

function Positions({ account, notify, onSelect }: { account: Account | null; notify: (m: string, bad?: boolean) => void; onSelect: (s: string) => void }) {
  if (!account) return <div className="empty">Loading…</div>;
  const act = async (path: string, msg: string) => {
    try { await post(path); notify(msg); } catch (e) { notify(String(e), true); }
  };
  const rows = [...account.open, ...account.history];
  const closed = account.history;
  const wins = closed.filter((p) => p.pnl > 0).length;
  return (
    <div>
      <div className="row" style={{ padding: "4px 8px", gap: 14 }}>
        <span>Equity <b className="mono">{account.equity.toFixed(2)}</b></span>
        <span>Realized today <b className={`mono ${account.risk.realized_today >= 0 ? "up" : "dn"}`}>{account.risk.realized_today.toFixed(2)}</b> / limit −{account.risk.daily_loss_limit.toFixed(2)}</span>
        <span className="muted">closed {closed.length} · win {closed.length ? Math.round((wins / closed.length) * 100) : 0}%</span>
        {account.risk.blocked && <span className="dn">blocked: {account.risk.blocked}</span>}
        {account.risk.killed && <button onClick={() => act("/api/kill/reset", "Kill switch reset")}>Reset kill switch</button>}
      </div>
      <table className="t">
        <thead><tr><th>Symbol</th><th>Side</th><th>Source</th><th className="right">Qty</th><th className="right">Entry</th><th className="right">Stop</th><th className="right">Last</th>
          <th className="right">Unreal.</th><th className="right">P&L</th><th className="right">R</th><th>Status</th><th>Opened</th><th /></tr></thead>
        <tbody>
          {rows.map((p) => (
            <tr key={p.id} className="click" onClick={() => onSelect(p.symbol)}>
              <td><b>{p.symbol}</b></td><td className={p.side === "LONG" ? "up" : "dn"}>{p.side}</td><td>{p.source === "signal" ? sid(p.strategy) : "manual"}</td>
              <td className="right mono">{fmtNum(p.qty, 4)}</td><td className="right mono">{fmtPrice(p.entry)}</td><td className="right mono">{fmtPrice(p.stop)}</td>
              <td className="right mono">{fmtPrice(p.last)}</td>
              <td className={`right mono ${p.unrealized >= 0 ? "up" : "dn"}`}>{p.status === "open" ? p.unrealized.toFixed(2) : ""}</td>
              <td className={`right mono ${p.pnl >= 0 ? "up" : "dn"}`}>{p.closed_at ? p.pnl.toFixed(2) : ""}</td>
              <td className="right mono">{fmtNum(p.r, 2)}</td>
              <td>{p.closed_at ? p.exit_reason : p.status}</td><td className="muted">{ago(p.opened_at)}</td>
              <td>{!p.closed_at && <>
                <button onClick={(e) => { e.stopPropagation(); act(`/api/positions/${p.id}/breakeven`, "Stop moved to breakeven"); }}>BE</button>{" "}
                <button onClick={(e) => { e.stopPropagation(); act(`/api/positions/${p.id}/close`, "Position closed"); }}>Close</button>
              </>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {!rows.length && <div className="empty">No paper positions. Use the order ticket (right) or “Take (paper)” on a signal. Hotkeys: Shift+B buy, Shift+S sell, Shift+X close all on this symbol.</div>}
    </div>
  );
}

function Strategies() {
  const [d, setD] = useState<{ strategies: StrategyInfo[]; bootstrap: Record<string, unknown> | null } | null>(null);
  useEffect(() => { get<typeof d>("/api/strategies").then(setD).catch(() => undefined); }, []);
  if (!d) return <div className="empty">Loading…</div>;
  const cell = (s?: { n: number; win_rate: number | null; expectancy_r: number; profit_factor: number | null }) =>
    s && s.n ? <>{s.n} · {s.win_rate !== null ? `${Math.round(s.win_rate * 100)}%` : "—"} · {fmtNum(s.expectancy_r, 2)}R · PF {s.profit_factor ?? "—"}</> : <span className="faint">no data</span>;
  return (
    <div>
      <div className="muted" style={{ padding: "4px 8px" }}>
        Statistics are net of fees and slippage. Backtest stats come from the startup bootstrap ({String((d.bootstrap as { status?: string } | null)?.status ?? "not run")}) or <span className="mono">scalper backtest --save-stats</span>.
        A strategy whose live results fall below its backtest confidence band is moved to shadow mode automatically.
      </div>
      <table className="t">
        <thead><tr><th>Strategy</th><th>Timeframes</th><th>Description</th><th>Backtest (n · win · exp · PF)</th><th>Live (n · win · exp · PF)</th><th>ML model</th><th>State</th></tr></thead>
        <tbody>
          {d.strategies.map((s) => (
            <tr key={s.id}>
              <td><b>{s.id}</b></td><td>{s.timeframes.join(", ")}</td>
              <td className="ellipsis" style={{ maxWidth: 380 }} title={s.description}>{s.description}{s.needs.length ? <span className="warn"> (live data: {s.needs.join(", ")})</span> : null}</td>
              <td className="mono">{cell(s.stats?.backtest)}</td><td className="mono">{cell(s.stats?.live)}</td>
              <td>{s.model ? `AUC ${(s.model as { auc_oos?: number }).auc_oos ?? "—"}` : <span className="faint">—</span>}</td>
              <td>{s.stats?.shadow ? <span className="warn" title={s.stats.shadow}>shadow</span> : <span className="up">active</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CouplingTable({ rows, onSelect }: { rows: Coupling[]; onSelect: (s: string) => void }) {
  const sorted = [...rows].sort((a, b) => b.rho - a.rho);
  if (!rows.length) return <div className="empty">Coupling needs a few minutes of aligned data.</div>;
  return (
    <table className="t">
      <thead><tr><th>Symbol</th><th>Label</th><th className="right">ρ 4h</th><th className="right">ρ 24h</th><th className="right">β</th><th className="right">R²</th>
        <th className="right">Lag</th><th className="right">Own move 1h</th><th className="right">Own move z</th><th className="right">Own move today</th></tr></thead>
      <tbody>
        {sorted.map((r) => (
          <tr key={r.symbol} className="click" onClick={() => onSelect(r.symbol)}>
            <td><b>{r.symbol}</b></td><td><span className={`badge ${couplingClass(r.label)}`}>{r.label}</span></td>
            <td className="right mono">{fmtNum(r.rho, 2)}</td><td className="right mono">{fmtNum(r.rho_24h ?? null, 2)}</td>
            <td className="right mono">{fmtNum(r.beta, 2)}</td><td className="right mono">{fmtNum(r.r2, 2)}</td>
            <td className="right mono">{r.lag_s !== undefined ? `${r.lag_s}s${r.lag_significant ? " ✓" : ""}` : "—"}</td>
            <td className="right mono">{fmtPct(r.residual_1h_pct ?? null)}</td>
            <td className="right mono">{fmtNum(r.residual_1h_z ?? null, 1)}</td>
            <td className="right mono">{fmtPct(r.residual_session_pct ?? null)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
