import { post } from "../api";
import { fmtPrice } from "../format";
import type { Account, CalEvent, Status, WatchRow } from "../types";
import { TrendDots } from "./bits";

interface Props { status: Status | null; wsUp: boolean; btc?: WatchRow; account: Account | null; nextEvent: CalEvent | null; macroBlock: boolean }

export default function TopBar({ status, wsUp, btc, account, nextEvent, macroBlock }: Props) {
  const phaseOk = status?.phase === "live";
  const kill = async () => {
    if (!confirm("KILL SWITCH: close all paper positions and block trading until reset?")) return;
    await post("/api/kill");
  };
  const fg = status?.fear_greed;
  return (
    <div className="topbar">
      <div className="brand">
        <svg width="18" height="18" viewBox="0 0 32 32"><path d="M5 22l7-8 5 5 10-11" stroke="#22c55e" strokeWidth="3.2" fill="none" strokeLinecap="round" strokeLinejoin="round" /></svg>
        Scalping Terminal
      </div>
      <span className={`pill ${phaseOk ? "ok" : status?.phase === "error" ? "bad" : "warn"}`} title={status?.errors.map((e) => e.error).join("\n")}>
        {status ? status.phase : "connecting"}
      </span>
      <span className={`pill ${wsUp ? "ok" : "bad"}`}>UI {wsUp ? "live" : "offline"}</span>
      {status && <span className={`pill ${status.ws.connected === status.ws.connections && status.ws.connections ? "ok" : "warn"}`}
        title={`${status.ws.streams} streams, ${status.ws.messages} msgs, ${status.ws.reconnects} reconnects`}>Binance {status.ws.streams} streams</span>}
      {status && <span className={`pill ${status.futures_data ? "ok" : "warn"}`} title="Funding, open interest and liquidations">futures data {status.futures_data ? "on" : "off"}</span>}
      {btc && (
        <span className="row"><b>BTC</b><span className="mono">{fmtPrice(btc.price)}</span><TrendDots trend={btc.trend} /></span>
      )}
      {fg && <span className="pill" title="Fear & Greed index (alternative.me)">F&G {fg.value} · {fg.label}</span>}
      {macroBlock && <span className="pill bad">macro event window — new entries blocked</span>}
      {!macroBlock && nextEvent && <span className="pill warn" title={nextEvent.title}>next: {nextEvent.country} {nextEvent.title.slice(0, 26)} {new Date(nextEvent.ts).toLocaleString("en-GB", { weekday: "short", hour: "2-digit", minute: "2-digit" })}</span>}
      <div className="grow" />
      {status?.bootstrap?.status === "running" && <span className="pill warn">computing strategy stats…</span>}
      {account && <span className="mono">paper equity <b>{account.equity.toFixed(2)}</b></span>}
      {account?.risk.blocked && <span className="pill bad" title={account.risk.blocked}>trading blocked</span>}
      <button className="kill" onClick={kill} title="Close all positions and block trading">KILL</button>
    </div>
  );
}
