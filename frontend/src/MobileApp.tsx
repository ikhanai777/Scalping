// Phone layout: compact top bar, one screen at a time, bottom navigation, trade sheet.
import { useCallback, useEffect, useState } from "react";
import { get, post } from "./api";
import { CouplingTable, Positions, Trends } from "./components/BottomTabs";
import ChartPanel from "./components/ChartPanel";
import RightColumn from "./components/RightColumn";
import Watchlist from "./components/Watchlist";
import { ago, fmtNum, fmtPct, fmtPrice, fmtTime, sid } from "./format";
import type { MoveEvent, NewsItem, SignalJ, StrategyInfo } from "./types";
import { useTerminal, type Terminal } from "./useTerminal";

type Screen = "chart" | "scan" | "signals" | "radar" | "more";
type Sub = null | "trends" | "news" | "positions" | "strategies" | "coupling" | "settings" | "logs";

declare global {
  interface Window { AndroidBridge?: { restartEngine(): void; appVersion(): string; openExternal(url: string): void; exitApp(): void } }
}

const ACTIVE = ["pending", "triggered", "tp1_hit"];

export default function MobileApp() {
  const t = useTerminal();
  const [screen, setScreen] = useState<Screen>(() => (localStorage.getItem("m_screen") as Screen) || "chart");
  const [sub, setSub] = useState<Sub>(null);
  const [trade, setTrade] = useState(false);
  useEffect(() => { try { localStorage.setItem("m_screen", screen); } catch { /* ignore */ } }, [screen]);

  // Android back button / browser back closes sheets and sub-screens first.
  useEffect(() => {
    const onPop = () => { if (trade) setTrade(false); else if (sub) setSub(null); };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [trade, sub]);
  const openSub = (s: Sub) => { history.pushState({ s }, ""); setSub(s); };
  const openTrade = () => { history.pushState({ trade: true }, ""); setTrade(true); };

  const { setSymbol, setStop } = t;
  const select = useCallback((s: string) => { setSymbol(s); setSub(null); setScreen("chart"); }, [setSymbol]);
  const onPickPrice = useCallback((p: number) => setStop(p.toPrecision(8).replace(/\.?0+$/, "")), [setStop]);
  const row = t.rows.find((r) => r.symbol === t.symbol);
  const activeSignals = t.signals.filter((s) => ACTIVE.includes(s.status)).length;
  const activeRadar = t.radar.filter((e) => e.stage && e.stage !== "WATCH").length;
  const live = t.status?.phase === "live" && t.wsUp;

  return (
    <div className="m-app">
      <header className="m-top">
        <span className={`dot ${live ? "ok" : t.status?.phase === "error" ? "bad" : "warn"}`} title={t.status?.phase ?? "connecting"} />
        <button className="m-sym" onClick={() => { setSub(null); setScreen("scan"); }}>{t.symbol.replace("USDT", "")}<span className="faint">/USDT ▾</span></button>
        {row && <span className="mono">{fmtPrice(row.price)}</span>}
        {row && <span className={`mono ${row.change_pct >= 0 ? "up" : "dn"}`}>{fmtPct(row.change_pct, 1)}</span>}
        <span className="grow" />
        {t.account && <span className="mono muted">{t.account.equity.toFixed(0)}</span>}
        <button className="icon" onClick={() => { setScreen("more"); openSub("settings"); }} aria-label="Settings">⚙</button>
      </header>
      {t.status && t.status.phase !== "live" && t.status.phase !== "error" && (
        <div className="banner">Engine {t.status.phase}: loading market history…</div>
      )}
      {t.macroBlock && <div className="banner">Macro event window — new entries are blocked</div>}

      <main className="m-main">
        {screen === "chart" && (
          <div className="m-chart">
            <ChartPanel symbol={t.symbol} tf={t.tf} setTf={t.setTf} onPickPrice={onPickPrice} compact />
            <button className="fab" onClick={openTrade}>Trade</button>
          </div>
        )}
        {screen === "scan" && <Watchlist rows={t.rows} selected={t.symbol} onSelect={select} />}
        {screen === "signals" && <MSignals t={t} onSelect={select} />}
        {screen === "radar" && <MRadar radar={t.radar} onSelect={select} />}
        {screen === "more" && !sub && <MoreMenu open={openSub} t={t} />}
        {screen === "more" && sub && (
          <div className="m-sub">
            <div className="m-subhead"><button onClick={() => history.back()}>‹ Back</button><b>{SUB_TITLES[sub]}</b></div>
            <div className="m-subbody">
              {sub === "trends" && <Trends onSelect={select} />}
              {sub === "news" && <MNews news={t.news} t={t} onSelect={select} />}
              {sub === "positions" && <Positions account={t.account} notify={t.notify} onSelect={select} />}
              {sub === "strategies" && <MStrategies />}
              {sub === "coupling" && <CouplingTable rows={t.coupling} onSelect={select} />}
              {sub === "settings" && <Settings notify={t.notify} />}
              {sub === "logs" && <Logs />}
            </div>
          </div>
        )}
      </main>

      <nav className="m-nav">
        {([["chart", "📈", "Chart"], ["scan", "☰", "Scanner"], ["signals", "⚡", "Signals"], ["radar", "🚀", "Radar"], ["more", "⋯", "More"]] as const).map(([k, icon, label]) => (
          <button key={k} className={screen === k ? "active" : ""} onClick={() => { setSub(null); setScreen(k); }}>
            <span className="ico">{icon}</span>{label}
            {k === "signals" && activeSignals > 0 && <span className="pip">{activeSignals}</span>}
            {k === "radar" && activeRadar > 0 && <span className="pip">{activeRadar}</span>}
          </button>
        ))}
      </nav>

      {trade && (
        <div className="sheet-backdrop" onClick={() => history.back()}>
          <div className="sheet" onClick={(e) => e.stopPropagation()}>
            <div className="sheet-handle" />
            <RightColumn symbol={t.symbol} account={t.account} stop={t.stop} setStop={t.setStop} notify={t.notify} compact />
            <div className="row" style={{ padding: "6px 8px 10px" }}>
              <span className="muted grow">Tip: tap the chart or book to set the stop.</span>
              <button className="kill" onClick={async () => {
                if (confirm("KILL SWITCH: close all paper positions and block trading until reset?")) { await post("/api/kill"); t.notify("All positions closed"); }
              }}>KILL</button>
            </div>
          </div>
        </div>
      )}
      {t.toast && <div className={`m-toast ${t.toast.bad ? "bad" : ""}`}>{t.toast.msg}</div>}
    </div>
  );
}

const SUB_TITLES: Record<Exclude<Sub, null>, string> = {
  trends: "Trend Board", news: "News & calendar", positions: "Paper positions", strategies: "Strategies & stats",
  coupling: "BTC coupling", settings: "Settings", logs: "Engine log",
};

function MoreMenu({ open, t }: { open: (s: Sub) => void; t: Terminal }) {
  const items: [Sub, string, string][] = [
    ["trends", "📊", "Trend Board"], ["news", "📰", "News & calendar"], ["positions", "💼", `Positions${t.account?.open.length ? ` (${t.account.open.length})` : ""}`],
    ["strategies", "🧪", "Strategies & stats"], ["coupling", "🔗", "BTC coupling"], ["settings", "⚙", "Settings"], ["logs", "📄", "Engine log"],
  ];
  const fg = t.status?.fear_greed;
  return (
    <div className="m-more">
      <div className="m-cards">
        {items.map(([k, icon, label]) => <button key={k} className="m-tile" onClick={() => open(k)}><span className="ico">{icon}</span>{label}</button>)}
      </div>
      <div className="m-info">
        {fg && <div>Fear & Greed: <b>{fg.value}</b> · {fg.label}</div>}
        {t.nextEvent && <div>Next macro event: {t.nextEvent.country} {t.nextEvent.title} · {fmtTime(t.nextEvent.ts, true)}</div>}
        {t.status && <div className="muted">Binance {t.status.ws.streams} streams · futures data {t.status.futures_data ? "on" : "off"} · {t.status.trend_symbols} tracked · {t.status.radar_symbols} on radar</div>}
        <div className="muted">Paper trading only. Signals are probabilistic — see Strategies & stats for measured results.</div>
      </div>
    </div>
  );
}

function MSignals({ t, onSelect }: { t: Terminal; onSelect: (s: string) => void }) {
  const take = async (s: SignalJ) => {
    try { await post(`/api/orders/from-signal/${s.id}`); t.notify(`Paper position opened from ${sid(s.strategy)} ${s.symbol}`); }
    catch (e) { t.notify(String(e).replace("Error: ", ""), true); }
  };
  if (!t.signals.length) return <div className="empty">No signals yet. A signal appears when a setup passes the spread, news, macro, cost and regime checks and its confluence score.</div>;
  return (
    <div className="m-list">
      {t.signals.slice(0, 150).map((s) => {
        const active = ACTIVE.includes(s.status);
        return (
          <div key={s.id} className={`m-card ${active ? "" : "dim"}`} onClick={() => onSelect(s.symbol)}>
            <div className="row">
              <b className={s.side === "LONG" ? "up" : "dn"}>{s.side === "LONG" ? "▲" : "▼"} {s.symbol.replace("USDT", "")}</b>
              <span className="chip">{sid(s.strategy)}</span><span className="chip">{s.tf}</span>
              <span className="grow" /><span className="muted">{ago(s.ts)}</span>
            </div>
            <div className="mono m-kv">
              <span>entry {fmtPrice(s.entry.price)}</span><span className="dn">SL {fmtPrice(s.stop)}</span>
              <span className="up">TP {s.targets.map((x) => fmtPrice(x.price)).join(" / ")}</span>
            </div>
            <div className="row" style={{ gap: 10 }}>
              <span>P(win) <b>{s.p_win !== null ? `${Math.round(s.p_win * 100)}%` : "unvalidated"}</b></span>
              <span>EV {s.expected_R !== null ? `${fmtNum(s.expected_R, 2)}R` : "—"}</span>
              <span>conf {s.confluence}</span>
              <span className="grow" />
              <span className="muted">{s.status}{s.outcome_r !== null && ` ${fmtNum(s.outcome_r, 2)}R`}</span>
            </div>
            <div className="muted m-reasons">{s.reasons.join(" · ")}{s.risks.length ? ` · risks: ${s.risks.join("; ")}` : ""}</div>
            {active && <button className="buy" onClick={(e) => { e.stopPropagation(); take(s); }}>Take (paper)</button>}
          </div>
        );
      })}
    </div>
  );
}

function MRadar({ radar, onSelect }: { radar: MoveEvent[]; onSelect: (s: string) => void }) {
  const [recent, setRecent] = useState<MoveEvent[]>([]);
  useEffect(() => { get<{ recent: MoveEvent[] }>("/api/radar").then((r) => setRecent(r.recent)).catch(() => undefined); }, [radar.length]);
  const active = radar.filter((e) => e.stage && e.stage !== "WATCH");
  const watch = radar.filter((e) => e.stage === "WATCH");
  const rows = [...active, ...recent.filter((r) => !active.some((a) => a.id === r.id))].slice(0, 60);
  if (!rows.length && !watch.length) return <div className="empty">No abnormal moves yet. The radar scans liquid USDT pairs every second.</div>;
  return (
    <div className="m-list">
      {rows.map((e) => (
        <div key={e.id} className={`m-card ${e.outcome ? "dim" : ""}`} onClick={() => onSelect(e.symbol)}>
          <div className="row">
            <b className={e.direction === "PUMP" ? "up" : "dn"}>{e.direction === "PUMP" ? "🚀" : "💥"} {e.symbol.replace("USDT", "")}</b>
            <span className="chip">{e.stage}</span><span className="chip">{e.classification}</span>
            <span className="grow" /><span className="muted">{e.ignition ? ago(e.ignition.ts) : ""}</span>
          </div>
          <div className="mono m-kv">
            <span>move {fmtPct(e.move_pct)}</span><span>now {fmtPct(e.current_pct)}</span><span>vol ×{e.vol_x}</span>
            <span>too late beyond {fmtPrice(e.chase_limit)}</span>
          </div>
          {(e.catalyst || e.outcome || e.exhaustion.length > 0) && (
            <div className="muted m-reasons">{e.catalyst ? `catalyst: ${e.catalyst} · ` : ""}{e.outcome ? `ended: ${e.outcome}` : e.exhaustion.join(", ")}</div>
          )}
        </div>
      ))}
      {watch.length > 0 && <div className="muted" style={{ padding: 8 }}>Watching: {watch.map((w) => <span key={w.symbol} className="chip" onClick={() => onSelect(w.symbol)}>{w.symbol.replace("USDT", "")}</span>)}</div>}
    </div>
  );
}

function MNews({ news, t, onSelect }: { news: NewsItem[]; t: Terminal; onSelect: (s: string) => void }) {
  const open = (url: string) => (window.AndroidBridge ? window.AndroidBridge.openExternal(url) : window.open(url, "_blank"));
  return (
    <div className="m-list">
      {t.events.filter((e) => e.impact === "High").slice(0, 6).map((e, i) => (
        <div key={`e${i}`} className="m-card"><span className="dn">●</span> <b>{e.country}</b> {e.title} <span className="muted">· {fmtTime(e.ts, true)}{e.forecast ? ` · f ${e.forecast}` : ""}</span></div>
      ))}
      {news.slice(0, 150).map((n) => (
        <div key={n.id} className="m-card" onClick={() => open(n.url)}>
          <div className="row"><span className="chip">{n.event_type}</span><span className="muted">{n.source} · {ago(n.ts)}</span><span className="grow" />
            <span className={n.sentiment > 0.15 ? "up" : n.sentiment < -0.15 ? "dn" : "muted"}>{fmtNum(n.sentiment, 2)}</span></div>
          <div>{n.title}</div>
          {n.symbols.length > 0 && <div>{n.symbols.map((s) => <span key={s} className="chip" onClick={(ev) => { ev.stopPropagation(); onSelect(`${s}USDT`); }}>{s}</span>)}</div>}
        </div>
      ))}
    </div>
  );
}

function MStrategies() {
  const [d, setD] = useState<{ strategies: StrategyInfo[] } | null>(null);
  useEffect(() => { get<typeof d>("/api/strategies").then(setD).catch(() => undefined); }, []);
  if (!d) return <div className="empty">Loading…</div>;
  return (
    <div className="m-list">
      <div className="muted" style={{ padding: "4px 8px" }}>Net of fees and slippage. Strategies with negative expected value after costs are suppressed automatically.</div>
      {d.strategies.map((s) => {
        const b = s.stats?.backtest, l = s.stats?.live;
        return (
          <div key={s.id} className="m-card">
            <div className="row"><b>{s.id}</b><span className="grow" />{s.stats?.shadow ? <span className="warn">shadow</span> : <span className="up">active</span>}</div>
            <div className="muted">{s.description}</div>
            <div className="mono m-kv">
              <span>backtest {b && b.n ? `${b.n} · ${Math.round((b.win_rate ?? 0) * 100)}% · ${fmtNum(b.expectancy_r, 2)}R` : "no data"}</span>
              <span>live {l && l.n ? `${l.n} · ${Math.round((l.win_rate ?? 0) * 100)}% · ${fmtNum(l.expectancy_r, 2)}R` : "no data"}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

const FIELDS: { key: string; label: string; type: "int" | "float" | "bool" | "text" | "secret" | "select"; options?: string[]; help?: string }[] = [
  { key: "universe.trend_universe_size", label: "Coins with trend tracking", type: "int", help: "More = more CPU and data" },
  { key: "universe.radar_max_symbols", label: "Coins on pump radar", type: "int", help: "~40 KB/s of data per 100 coins" },
  { key: "trend.sensitivity", label: "Trend sensitivity", type: "select", options: ["early", "balanced", "conservative"] },
  { key: "execution.market", label: "Market (fees, shorts)", type: "select", options: ["spot", "futures"] },
  { key: "risk.equity", label: "Paper equity", type: "float" },
  { key: "risk.risk_per_trade", label: "Risk per trade (0.005 = 0.5%)", type: "float" },
  { key: "risk.max_daily_loss", label: "Max daily loss (0.02 = 2%)", type: "float" },
  { key: "risk.max_open_positions", label: "Max open positions", type: "int" },
  { key: "signals.min_confluence", label: "Min confluence score", type: "float" },
  { key: "signals.show_unvalidated", label: "Show unvalidated signals", type: "bool" },
  { key: "alerts.local_notifications", label: "Phone notifications", type: "bool" },
  { key: "alerts.pump_alerts", label: "Pump/dump alerts", type: "bool" },
  { key: "alerts.min_p_win", label: "Alert when P(win) ≥", type: "float" },
  { key: "alerts.telegram_token", label: "Telegram bot token", type: "secret" },
  { key: "alerts.telegram_chat_id", label: "Telegram chat id", type: "text" },
  { key: "news.enabled", label: "News feed", type: "bool" },
  { key: "news.cryptopanic_token", label: "CryptoPanic key (optional)", type: "secret" },
  { key: "bootstrap.enabled", label: "Backtest at start (seed stats)", type: "bool" },
];

function Settings({ notify }: { notify: (m: string, bad?: boolean) => void }) {
  const [vals, setVals] = useState<Record<string, unknown> | null>(null);
  const [dirty, setDirty] = useState<Record<string, unknown>>({});
  const [saved, setSaved] = useState(false);
  useEffect(() => { get<{ settings: Record<string, unknown> }>("/api/settings").then((d) => setVals(d.settings)).catch((e) => notify(String(e), true)); }, [notify]);
  if (!vals) return <div className="empty">Loading…</div>;
  const v = (k: string) => (k in dirty ? dirty[k] : vals[k]);
  const set = (k: string, x: unknown) => { setDirty({ ...dirty, [k]: x }); setSaved(false); };
  const save = async () => {
    try {
      await post("/api/settings", dirty);
      setVals({ ...vals, ...dirty }); setDirty({}); setSaved(true);
      notify("Saved. Restart the engine to apply.");
    } catch (e) { notify(String(e).replace("Error: ", ""), true); }
  };
  const restart = () => {
    if (window.AndroidBridge) window.AndroidBridge.restartEngine();
    else notify("Restart the server process to apply the new settings.");
  };
  return (
    <div className="m-settings">
      {FIELDS.map((f) => (
        <label key={f.key} className="m-field">
          <span>{f.label}{f.help && <small className="faint"> · {f.help}</small>}</span>
          {f.type === "bool" ? (
            <input type="checkbox" checked={!!v(f.key)} onChange={(e) => set(f.key, e.target.checked)} />
          ) : f.type === "select" ? (
            <select value={String(v(f.key) ?? "")} onChange={(e) => set(f.key, e.target.value)}>
              {f.options!.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          ) : (
            <input type={f.type === "secret" ? "password" : f.type === "text" ? "text" : "number"} inputMode={f.type === "int" || f.type === "float" ? "decimal" : undefined}
              value={f.type === "secret" && v(f.key) === "set" ? "" : String(v(f.key) ?? "")}
              placeholder={f.type === "secret" && vals[f.key] === "set" ? "•••• saved" : ""}
              onChange={(e) => set(f.key, f.type === "int" ? parseInt(e.target.value || "0", 10) : f.type === "float" ? parseFloat(e.target.value || "0") : e.target.value)} />
          )}
        </label>
      ))}
      <div className="row" style={{ padding: 10, gap: 8 }}>
        <button className="active grow" disabled={!Object.keys(dirty).length} onClick={save}>Save</button>
        <button className="grow" onClick={restart}>{saved ? "Restart engine now" : "Restart engine"}</button>
      </div>
      {window.AndroidBridge && (
        <div style={{ padding: "0 10px 10px" }}>
          <button className="kill" style={{ width: "100%" }} onClick={() => {
            if (confirm("Stop the background engine and close the app? Alerts stop until you open it again.")) window.AndroidBridge!.exitApp();
          }}>Stop engine & exit</button>
        </div>
      )}
      <div className="muted" style={{ padding: "0 10px 14px" }}>
        {window.AndroidBridge ? `App ${window.AndroidBridge.appVersion()} · ` : ""}All trading is paper-simulated. Settings are stored on this device only.
      </div>
    </div>
  );
}

function Logs() {
  const [lines, setLines] = useState<string[]>([]);
  useEffect(() => {
    const load = () => get<{ lines: string[] }>("/api/logs").then((d) => setLines(d.lines)).catch(() => undefined);
    load();
    const i = window.setInterval(load, 5000);
    return () => window.clearInterval(i);
  }, []);
  return <pre className="m-logs mono">{lines.length ? lines.slice().reverse().join("\n") : "No log lines."}</pre>;
}
