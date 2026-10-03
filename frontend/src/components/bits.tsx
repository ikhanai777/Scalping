import { fmtNum, stateClass } from "../format";
import { TFS, type Coupling, type TrendCell } from "../types";

export function TrendDots({ trend }: { trend?: Record<string, TrendCell> }) {
  if (!trend) return <span className="faint">—</span>;
  return (
    <span className="strip" title={TFS.map((t) => `${t}: ${trend[t]?.state ?? "—"}${trend[t]?.dir ? (trend[t].dir > 0 ? " ▲" : " ▼") : ""}`).join("\n")}>
      {TFS.map((t) => <span key={t} className={`tc ${stateClass(trend[t]?.dir ?? 0, trend[t]?.state ?? "NONE")}`} />)}
    </span>
  );
}

export function MtfStrip({ mtf, label }: { mtf: Record<string, TrendCell>; label: string }) {
  return (
    <span className="strip-labeled" title={`${label} trend per timeframe (Trend Catcher)`}>
      <span className="muted" style={{ fontSize: 10.5 }}>{label}</span>
      {TFS.map((t) => (
        <span key={t} className="cell">
          <span className={`tc ${stateClass(mtf[t]?.dir ?? 0, mtf[t]?.state ?? "NONE")}`} />
          {t}
        </span>
      ))}
    </span>
  );
}

export function couplingClass(label?: string | null): string {
  if (!label) return "";
  if (label === "LEADER") return "leader";
  if (label.startsWith("FOLLOWER")) return "follower";
  if (label.startsWith("INDEPENDENT")) return "independent";
  if (label.startsWith("DECOUPLING")) return "decoupling";
  return "";
}

export function CouplingBadge({ c, symbol }: { c: Coupling | null; symbol: string }) {
  if (symbol === "BTCUSDT") return <span className="badge leader">BTC · leader</span>;
  if (!c) return <span className="badge">BTC coupling: warming up</span>;
  return (
    <span className={`badge ${couplingClass(c.label)}`} title={`Correlation with BTC (1m returns, 4h): ρ ${c.rho}\n24h ρ ${c.rho_24h ?? "—"}\nβ ${c.beta}\nR² ${c.r2}\nResidual (own move) 1h ${c.residual_1h_pct ?? "—"}%`}>
      BTC {c.label.replace("+LAGGER", " · lagger")} · ρ {fmtNum(c.rho, 2)} · β {fmtNum(c.beta, 2)}
      {c.lag_significant && c.lag_s ? ` · lag≈${c.lag_s}s` : ""}
    </span>
  );
}
