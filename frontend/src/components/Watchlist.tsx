import { useMemo, useState } from "react";
import { fmtPct, fmtPrice, fmtVol } from "../format";
import type { WatchRow } from "../types";
import { couplingClass, TrendDots } from "./bits";

type Sort = "opportunity" | "volume" | "change" | "trend";

export default function Watchlist({ rows, selected, onSelect }: { rows: WatchRow[]; selected: string; onSelect: (s: string) => void }) {
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<Sort>("opportunity");
  const [trackedOnly, setTrackedOnly] = useState(true);
  const list = useMemo(() => {
    let r = rows.filter((x) => (!trackedOnly || x.tracked || x.pump) && x.symbol.includes(q.toUpperCase()));
    const key: Record<Sort, (x: WatchRow) => number> = {
      opportunity: (x) => -x.opportunity,
      volume: (x) => -x.quote_volume,
      change: (x) => -Math.abs(x.change_pct),
      trend: (x) => -Math.abs(Object.values(x.trend || {}).reduce((a, c) => a + (c.state !== "NONE" ? c.dir : 0), 0)),
    };
    r = [...r].sort((a, b) => key[sort](a) - key[sort](b));
    return r.slice(0, 300);
  }, [rows, q, sort, trackedOnly]);

  return (
    <div className="panel">
      <div className="panel-h">Scanner <span className="grow" /><span className="faint">{list.length}</span></div>
      <div style={{ padding: 6, display: "grid", gap: 4, borderBottom: "1px solid var(--border)" }}>
        <input placeholder="Search symbol…" value={q} onChange={(e) => setQ(e.target.value)} />
        <div className="row">
          <select value={sort} onChange={(e) => setSort(e.target.value as Sort)} className="grow">
            <option value="opportunity">Sort: opportunity</option>
            <option value="trend">Sort: trend alignment</option>
            <option value="volume">Sort: 24h volume</option>
            <option value="change">Sort: |24h change|</option>
          </select>
          <label className="row muted" title="Only symbols with live trend tracking (plus active pumps)">
            <input type="checkbox" checked={trackedOnly} onChange={(e) => setTrackedOnly(e.target.checked)} /> tracked
          </label>
        </div>
      </div>
      <div className="scroll">
        {list.map((r) => (
          <div key={r.symbol} className={`wl-row ${r.symbol === selected ? "sel" : ""}`} onClick={() => onSelect(r.symbol)}>
            <div style={{ minWidth: 0 }}>
              <div className="row"><span className="wl-sym">{r.base}</span>
                {r.pump && <span className={`badge ${r.pump.direction === "PUMP" ? "pump" : "dump"}`}>{r.pump.direction === "PUMP" ? "🚀" : "💥"} {r.pump.stage.slice(0, 4)}</span>}
                {r.signals > 0 && <span className="badge sig">{r.signals} sig</span>}
              </div>
              <div className="wl-sub"><TrendDots trend={r.trend} />
                {r.coupling && <span className={`badge ${couplingClass(r.coupling)}`}>{r.coupling.replace("+LAGGER", "+lag").slice(0, 11)}</span>}
              </div>
            </div>
            <div className="right mono">{fmtPrice(r.price)}<div className="faint">{fmtVol(r.quote_volume)}</div></div>
            <div className={`right mono ${r.change_pct >= 0 ? "up" : "dn"}`}>{fmtPct(r.change_pct, 1)}</div>
          </div>
        ))}
        {!list.length && <div className="empty">No symbols yet — the engine is loading the market.</div>}
      </div>
    </div>
  );
}
