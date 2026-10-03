import { useEffect, useState } from "react";
import { get, post, push } from "../api";
import { fmtNum, fmtPrice, fmtTime } from "../format";
import type { Account, BookMetrics } from "../types";

type Level = [number, number];
interface Wall { side: string; price: number; qty: number; age_s: number }

export default function RightColumn({ symbol, account, stop, setStop, notify }: {
  symbol: string; account: Account | null; stop: string; setStop: (s: string) => void; notify: (msg: string, bad?: boolean) => void;
}) {
  const [bids, setBids] = useState<Level[]>([]);
  const [asks, setAsks] = useState<Level[]>([]);
  const [metrics, setMetrics] = useState<BookMetrics | null>(null);
  const [walls, setWalls] = useState<Wall[]>([]);
  const [trades, setTrades] = useState<[number, number, number, string, boolean][]>([]);

  useEffect(() => {
    setBids([]); setAsks([]); setTrades([]); setMetrics(null); setWalls([]);
    get<{ synced: boolean; bids?: Level[]; asks?: Level[]; metrics?: BookMetrics }>(`/api/book/${symbol}`).then((b) => {
      if (b.synced) { setBids(b.bids || []); setAsks(b.asks || []); setMetrics(b.metrics || null); }
    }).catch(() => undefined);
    return push.on((m) => {
      if (m.symbol !== symbol) return;
      if (m.type === "book") { setBids(m.bids); setAsks(m.asks); setMetrics(m.metrics); setWalls(m.walls || []); }
      if (m.type === "trades") setTrades((t) => [...(m.data as typeof t).slice().reverse(), ...t].slice(0, 120));
    });
  }, [symbol]);

  const maxQ = Math.max(1e-12, ...bids.slice(0, 15).map((b) => b[1]), ...asks.slice(0, 15).map((a) => a[1]));
  const wallSet = new Set(walls.map((w) => w.price));
  const mid = metrics?.mid ?? (bids[0] && asks[0] ? (bids[0][0] + asks[0][0]) / 2 : null);

  return (
    <div className="panel right-col">
      <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
        <div className="panel-h">Order book <span className="grow" />
          {metrics && <span title="Order book imbalance (top 10 levels)" className={metrics.obi > 0 ? "up" : "dn"}>OBI {fmtNum(metrics.obi, 2)}</span>}
        </div>
        <div className="scroll" style={{ flex: 1 }}>
          {!bids.length && <div className="empty">Syncing order book…</div>}
          {asks.slice(0, 15).reverse().map(([p, q]) => (
            <div key={`a${p}`} className={`dom-row ask ${wallSet.has(p) ? "wall" : ""}`} onClick={() => setStop(String(p))} title="Click to use as stop/price">
              <span className="dn">{fmtPrice(p)}</span><span className="right">{fmtNum(q, 4)}</span><span className="right faint">{wallSet.has(p) ? "wall" : ""}</span>
              <span className="bar" style={{ width: `${(q / maxQ) * 100}%` }} />
            </div>
          ))}
          {mid !== null && (
            <div className="dom-mid">
              <b>{fmtPrice(mid)}</b>
              {metrics && <span className="muted">spread {fmtNum(metrics.spread_bps, 2)}bps · μ {fmtPrice(metrics.microprice)}</span>}
            </div>
          )}
          {bids.slice(0, 15).map(([p, q]) => (
            <div key={`b${p}`} className={`dom-row bid ${wallSet.has(p) ? "wall" : ""}`} onClick={() => setStop(String(p))} title="Click to use as stop/price">
              <span className="up">{fmtPrice(p)}</span><span className="right">{fmtNum(q, 4)}</span><span className="right faint">{wallSet.has(p) ? "wall" : ""}</span>
              <span className="bar" style={{ width: `${(q / maxQ) * 100}%` }} />
            </div>
          ))}
        </div>
        {metrics && (
          <div className="muted" style={{ padding: "3px 8px", borderTop: "1px solid var(--border)", fontSize: 11 }}>
            pressure ±1% {fmtNum(metrics.pressure_1pct, 2)} (z {fmtNum(metrics.pressure_z, 1)}) · aggressor buys {metrics.aggr_buy_share !== undefined ? `${Math.round(metrics.aggr_buy_share * 100)}%` : "—"} · intensity z {fmtNum(metrics.intensity_z ?? null, 1)}
          </div>
        )}
      </div>
      <div style={{ display: "flex", flexDirection: "column", minHeight: 0, borderTop: "1px solid var(--border)" }}>
        <div className="panel-h">Time & sales</div>
        <div className="scroll">
          {trades.map(([t, p, q, side, large], i) => (
            <div key={`${t}-${i}`} className={`tape-row ${large ? "large" : ""}`}>
              <span className="faint">{fmtTime(t)}</span>
              <span className={side === "BUY" ? "up" : "dn"}>{fmtPrice(p)}</span>
              <span className="right">{fmtNum(q, 4)}</span>
            </div>
          ))}
          {!trades.length && <div className="empty">Waiting for trades…</div>}
        </div>
      </div>
      <Ticket symbol={symbol} price={mid} account={account} stop={stop} setStop={setStop} notify={notify} />
    </div>
  );
}

function Ticket({ symbol, price, account, stop, setStop, notify }: {
  symbol: string; price: number | null; account: Account | null; stop: string; setStop: (s: string) => void; notify: (m: string, bad?: boolean) => void;
}) {
  const [tp, setTp] = useState("");
  const [riskScale, setRiskScale] = useState(1);
  const riskPct = Number(account?.risk.config.risk_per_trade ?? 0.005) * riskScale;
  const st = parseFloat(stop);
  const qty = price && st && account ? (account.equity * riskPct) / Math.abs(price - st) : null;
  const send = async (side: "LONG" | "SHORT") => {
    try {
      const targets = tp ? tp.split(/[ ,/]+/).map(Number).filter((x) => x > 0) : undefined;
      await post("/api/orders", { symbol, side, stop: st || null, targets, risk_scale: riskScale });
      notify(`Paper ${side} ${symbol} opened`);
    } catch (e) {
      notify(String(e).replace("Error: ", ""), true);
    }
  };
  return (
    <div className="ticket">
      <div className="panel-h" style={{ padding: 0, border: "none" }}>Paper order · risk-sized</div>
      <label>Stop<input className="mono" value={stop} onChange={(e) => setStop(e.target.value)} placeholder="click chart / book" /></label>
      <label>Targets<input className="mono" value={tp} onChange={(e) => setTp(e.target.value)} placeholder="default 1R / 2R" /></label>
      <label>Risk
        <select value={riskScale} onChange={(e) => setRiskScale(Number(e.target.value))}>
          {[0.25, 0.5, 1, 1.5, 2].map((x) => <option key={x} value={x}>{x}× = {(Number(account?.risk.config.risk_per_trade ?? 0.005) * x * 100).toFixed(2)}% equity</option>)}
        </select>
      </label>
      <div className="muted mono" style={{ fontSize: 11 }}>
        {qty ? `size ≈ ${fmtNum(qty, 4)} (${fmtNum(qty * (price || 0), 2)} notional)` : "set a stop to size the position"}
      </div>
      <div className="row">
        <button className="buy grow" onClick={() => send("LONG")} title="Shift+B">BUY / LONG</button>
        <button className="sell grow" onClick={() => send("SHORT")} title="Shift+S">SELL / SHORT</button>
      </div>
    </div>
  );
}
