export function fmtPrice(p: number | null | undefined): string {
  if (p === null || p === undefined || !isFinite(p)) return "—";
  const a = Math.abs(p);
  const d = a >= 1000 ? 2 : a >= 10 ? 3 : a >= 1 ? 4 : a >= 0.01 ? 5 : 8;
  return p.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

export function fmtPct(p: number | null | undefined, digits = 2, sign = true): string {
  if (p === null || p === undefined || !isFinite(p)) return "—";
  return `${sign && p > 0 ? "+" : ""}${p.toFixed(digits)}%`;
}

export function fmtNum(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || !isFinite(n)) return "—";
  return n.toFixed(digits);
}

export function fmtVol(v: number): string {
  if (v >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(0)}K`;
  return v.toFixed(0);
}

export function fmtTime(ms: number, withDate = false): string {
  const d = new Date(ms);
  const t = d.toLocaleTimeString("en-GB", { hour12: false });
  return withDate ? `${d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" })} ${t}` : t;
}

export function ago(ms: number): string {
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

export const sid = (strategy: string) => strategy.split("@")[0];
export const shortSid = (strategy: string) => sid(strategy).split("_")[0];

export function stateClass(dir: number, state: string): string {
  if (!dir || state === "NONE") return "tc-none";
  const d = dir > 0 ? "up" : "dn";
  if (state === "EARLY") return `tc-${d}-early`;
  if (state === "EXHAUSTING") return `tc-${d}-exh`;
  if (state === "ENDED") return "tc-none";
  return `tc-${d}`;
}
