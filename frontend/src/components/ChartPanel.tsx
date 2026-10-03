import { useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries, ColorType, createChart, createSeriesMarkers, CrosshairMode, HistogramSeries, LineSeries, LineStyle,
  type IChartApi, type IPriceLine, type ISeriesApi, type ISeriesMarkersPluginApi, type MouseEventParams,
  type SeriesMarker, type Time, type UTCTimestamp,
} from "lightweight-charts";
import { get, push } from "../api";
import { fmtNum, fmtPct, fmtPrice, fmtTime, shortSid, sid } from "../format";
import { TF_SEC, TFS, type BarJ, type ChartData, type MoveEvent, type NewsItem, type SignalJ, type Tf, type TrendRecord } from "../types";
import { CouplingBadge, MtfStrip } from "./bits";

type Toggles = Record<"ema" | "vwap" | "bb" | "trend" | "trail" | "signals" | "markers" | "pump" | "news" | "btc" | "channel", boolean>;
const DEFAULT_TOGGLES: Toggles = { ema: true, vwap: true, bb: false, trend: true, trail: true, signals: true, markers: true, pump: true, news: true, btc: false, channel: true };
const TOGGLE_LABELS: [keyof Toggles, string][] = [
  ["ema", "EMA"], ["vwap", "VWAP σ"], ["bb", "BB"], ["trend", "Trend shade"], ["trail", "Trail stop"], ["channel", "Channel"],
  ["signals", "Signals"], ["markers", "Trend marks"], ["pump", "Pump/Dump"], ["news", "News"], ["btc", "BTC overlay"],
];

const sec = (ms: number) => Math.floor(ms / 1000) as UTCTimestamp;

function loadToggles(): Toggles {
  try { return { ...DEFAULT_TOGGLES, ...JSON.parse(localStorage.getItem("chartToggles") || "{}") }; } catch { return DEFAULT_TOGGLES; }
}

interface Props { symbol: string; tf: Tf; setTf: (t: Tf) => void; onPickPrice?: (p: number) => void }

interface Series {
  candles: ISeriesApi<"Candlestick">; volume: ISeriesApi<"Histogram">; shade: ISeriesApi<"Histogram">;
  lines: Record<string, ISeriesApi<"Line">>; cvd: ISeriesApi<"Line">; delta: ISeriesApi<"Histogram">; rsi: ISeriesApi<"Line">;
  markers: ISeriesMarkersPluginApi<Time>;
}

type Hover = { x: number; y: number; html: React.ReactNode } | null;

export default function ChartPanel({ symbol, tf, setTf, onPickPrice }: Props) {
  const el = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const s = useRef<Series | null>(null);
  const priceLines = useRef<IPriceLine[]>([]);
  const [data, setData] = useState<ChartData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [toggles, setToggles] = useState<Toggles>(loadToggles);
  const [hover, setHover] = useState<Hover>(null);
  const [legend, setLegend] = useState<BarJ | null>(null);
  const dataRef = useRef<ChartData | null>(null);
  const togglesRef = useRef(toggles);
  togglesRef.current = toggles;
  const reloadTimer = useRef<number | null>(null);

  // ---------- chart creation ----------
  useEffect(() => {
    if (!el.current) return;
    const chart = createChart(el.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#0b0f14" }, textColor: "#7d8b9a", fontSize: 11,
        panes: { separatorColor: "#1f2a36", separatorHoverColor: "#2c3b4b" } },
      grid: { vertLines: { color: "#121a22" }, horzLines: { color: "#121a22" } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#1f2a36" },
      timeScale: { borderColor: "#1f2a36", timeVisible: true, secondsVisible: false, rightOffset: 8 },
    });
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: "#22c55e", downColor: "#ef4444", borderVisible: false, wickUpColor: "#22c55e", wickDownColor: "#ef4444",
    });
    const shade = chart.addSeries(HistogramSeries, { priceScaleId: "shade", priceLineVisible: false, lastValueVisible: false, base: 0 });
    chart.priceScale("shade").applyOptions({ scaleMargins: { top: 0, bottom: 0 }, visible: false });
    const volume = chart.addSeries(HistogramSeries, { priceScaleId: "vol", priceLineVisible: false, lastValueVisible: false, priceFormat: { type: "volume" } });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 }, visible: false });
    const mk = (color: string, width: 1 | 2 = 1, style: LineStyle = LineStyle.Solid, extra = {}) =>
      chart.addSeries(LineSeries, { color, lineWidth: width, lineStyle: style, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, ...extra });
    const lines: Record<string, ISeriesApi<"Line">> = {
      ema9: mk("#fbbf24"), ema21: mk("#60a5fa"), ema50: mk("#a78bfa"), ema200: mk("#f472b6", 2),
      vwap: mk("#e5e7eb", 2), vwap_u1: mk("#94a3b8", 1, LineStyle.Dotted), vwap_l1: mk("#94a3b8", 1, LineStyle.Dotted),
      vwap_u2: mk("#64748b", 1, LineStyle.Dashed), vwap_l2: mk("#64748b", 1, LineStyle.Dashed),
      bb_up: mk("#38bdf8", 1, LineStyle.Dotted), bb_lo: mk("#38bdf8", 1, LineStyle.Dotted),
      trail: mk("#f59e0b", 2, LineStyle.Dashed), ch_base: mk("#22d3ee", 1), ch_par: mk("#22d3ee", 1, LineStyle.Dashed),
      btc: mk("#fb923c", 1, LineStyle.Solid, { priceScaleId: "btc" }),
    };
    chart.priceScale("btc").applyOptions({ visible: false, scaleMargins: { top: 0.1, bottom: 0.25 } });
    const cvd = chart.addSeries(LineSeries, { color: "#22d3ee", lineWidth: 1, priceLineVisible: false, lastValueVisible: true, title: "CVD" }, 1);
    const delta = chart.addSeries(HistogramSeries, { priceScaleId: "delta", priceLineVisible: false, lastValueVisible: false }, 1);
    chart.priceScale("delta", 1).applyOptions({ visible: false, scaleMargins: { top: 0.5, bottom: 0 } });
    const rsi = chart.addSeries(LineSeries, { color: "#c084fc", lineWidth: 1, priceLineVisible: false, title: "RSI" }, 2);
    rsi.createPriceLine({ price: 70, color: "#374151", lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: false, title: "" });
    rsi.createPriceLine({ price: 30, color: "#374151", lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: false, title: "" });
    const panes = chart.panes();
    panes[0]?.setStretchFactor(0.7);
    panes[1]?.setStretchFactor(0.17);
    panes[2]?.setStretchFactor(0.13);
    const markers = createSeriesMarkers(candles, [], { zOrder: "top" });
    s.current = { candles, volume, shade, lines, cvd, delta, rsi, markers };
    chartRef.current = chart;

    const onMove = (p: MouseEventParams<Time>) => {
      const d = dataRef.current;
      if (!p.time || !p.point || !d) { setHover(null); setLegend(null); return; }
      const t = (p.time as number) * 1000;
      const bar = d.bars.find((b) => b.t === t) || (d.forming && d.forming.t === t ? d.forming : null);
      setLegend(bar);
      setHover(buildHover(d, t, p.point.x, p.point.y, togglesRef.current));
    };
    chart.subscribeCrosshairMove(onMove);
    const onClick = (p: MouseEventParams<Time>) => {
      if (p.point && onPickPrice) {
        const price = candles.coordinateToPrice(p.point.y);
        if (price !== null) onPickPrice(price as number);
      }
    };
    chart.subscribeClick(onClick);
    return () => { chart.unsubscribeCrosshairMove(onMove); chart.unsubscribeClick(onClick); chart.remove(); chartRef.current = null; s.current = null; };
  }, [onPickPrice]);

  // ---------- data loading ----------
  const load = useMemo(() => async (fit: boolean) => {
    try {
      const d = await get<ChartData>(`/api/chart/${symbol}?tf=${tf}&limit=600`);
      dataRef.current = d;
      setData(d);
      setErr(null);
      render(d, fit);
    } catch (e) {
      setErr(String(e));
    }
  }, [symbol, tf]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setData(null);
    dataRef.current = null;
    push.setFocus(symbol);
    load(true);
  }, [load, symbol]);

  // ---------- live updates ----------
  useEffect(() => {
    const scheduleReload = (delay = 400) => {
      if (reloadTimer.current) window.clearTimeout(reloadTimer.current);
      reloadTimer.current = window.setTimeout(() => load(false), delay);
    };
    return push.on((m) => {
      if (m.symbol !== symbol && m.data?.symbol !== symbol) return;
      const d = dataRef.current;
      if (!d || !s.current) return;
      if (m.type === "bar") {
        const f: BarJ | null = m.forming?.[tf] ?? null;
        if (f) {
          d.forming = f;
          s.current.candles.update({ time: sec(f.t), open: f.o, high: f.h, low: f.l, close: f.c });
          s.current.volume.update({ time: sec(f.t), value: f.v, color: f.c >= f.o ? "rgba(34,197,94,.35)" : "rgba(239,68,68,.35)" });
        }
      } else if (m.type === "bar_closed" && (m.tfs as string[]).includes(tf)) {
        scheduleReload(300);
      } else if (["signal", "signal_update", "pump", "trend"].includes(m.type)) {
        scheduleReload(800);
      }
    });
  }, [symbol, tf, load]);

  useEffect(() => {
    try { localStorage.setItem("chartToggles", JSON.stringify(toggles)); } catch { /* private mode */ }
    if (dataRef.current) render(dataRef.current, false);
  }, [toggles]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---------- rendering ----------
  function render(d: ChartData, fit: boolean) {
    const S = s.current;
    if (!S) return;
    const tg = togglesRef.current;
    const bars = d.forming && (!d.bars.length || d.forming.t > d.bars[d.bars.length - 1].t) ? [...d.bars, d.forming] : d.bars;
    S.candles.setData(bars.map((b) => ({ time: sec(b.t), open: b.o, high: b.h, low: b.l, close: b.c })));
    S.volume.setData(bars.map((b) => ({ time: sec(b.t), value: b.v, color: b.c >= b.o ? "rgba(34,197,94,.35)" : "rgba(239,68,68,.35)" })));
    const times = d.bars.map((b) => sec(b.t));
    const line = (name: string, on: boolean) => {
      const arr = d.series[name] || [];
      S.lines[name].setData(on ? times.map((t, i) => (arr[i] === null || arr[i] === undefined ? { time: t } : { time: t, value: arr[i] as number })) : []);
    };
    ["ema9", "ema21", "ema50", "ema200"].forEach((n) => line(n, tg.ema));
    ["vwap", "vwap_u1", "vwap_l1", "vwap_u2", "vwap_l2"].forEach((n) => line(n, tg.vwap && tf !== "1h" && tf !== "4h"));
    ["bb_up", "bb_lo"].forEach((n) => line(n, tg.bb));
    // trend shading + trailing stop from per-bar trend history
    const hist = new Map(d.trend_history.map((h) => [h[0], h]));
    S.shade.setData(tg.trend ? times.map((t) => {
      const h = hist.get(t * 1000);
      if (!h || !h[1] || h[2] === 0 || h[2] === 5) return { time: t };
      const [, dir, state] = h;
      const a = state === 1 ? 0.07 : state === 4 ? 0.1 : 0.14;
      const color = state === 4 ? `rgba(245,158,11,${a})` : dir > 0 ? `rgba(34,197,94,${a})` : `rgba(239,68,68,${a})`;
      return { time: t, value: 1, color };
    }) : []);
    S.lines.trail.setData(tg.trail ? times.map((t) => {
      const h = hist.get(t * 1000);
      return h && h[4] !== null && h[2] >= 1 && h[2] <= 4 ? { time: t, value: h[4] as number } : { time: t };
    }) : []);
    const ch = d.trend.channel;
    if (tg.channel && ch) {
      S.lines.ch_base.setData(ch.base.map(([t, p]) => ({ time: sec(snap(t, tf)), value: p })).filter((x, i, a) => i === 0 || x.time > a[i - 1].time));
      S.lines.ch_par.setData(ch.parallel.map(([t, p]) => ({ time: sec(snap(t, tf)), value: p })).filter((x, i, a) => i === 0 || x.time > a[i - 1].time));
    } else {
      S.lines.ch_base.setData([]);
      S.lines.ch_par.setData([]);
    }
    S.lines.btc.setData(tg.btc && d.btc_overlay ? d.btc_overlay.map(([t, v]) => (v === null ? { time: sec(t) } : { time: sec(t), value: v })) : []);
    const cvd = d.series.cvd || [], delta = d.series.delta || [], rsi = d.series.rsi || [];
    S.cvd.setData(times.map((t, i) => (cvd[i] == null ? { time: t } : { time: t, value: cvd[i] as number })));
    S.delta.setData(times.map((t, i) => (delta[i] == null ? { time: t } : { time: t, value: delta[i] as number, color: (delta[i] as number) >= 0 ? "rgba(34,197,94,.5)" : "rgba(239,68,68,.5)" })));
    S.rsi.setData(times.map((t, i) => (rsi[i] == null ? { time: t } : { time: t, value: rsi[i] as number })));
    S.markers.setMarkers(buildMarkers(d, tf, tg, bars.length ? bars[0].t : 0));
    // price lines for active signals + active pump chase limit
    priceLines.current.forEach((pl) => S.candles.removePriceLine(pl));
    priceLines.current = [];
    if (tg.signals) {
      for (const sg of d.signals.filter((x) => ["pending", "triggered", "tp1_hit"].includes(x.status)).slice(0, 2)) {
        const c = sg.side === "LONG" ? "#22c55e" : "#ef4444";
        priceLines.current.push(S.candles.createPriceLine({ price: sg.entry.price, color: c, lineStyle: LineStyle.Solid, lineWidth: 1, title: `${shortSid(sg.strategy)} entry` }));
        priceLines.current.push(S.candles.createPriceLine({ price: sg.stop, color: "#ef4444", lineStyle: LineStyle.Dashed, lineWidth: 1, title: "SL" }));
        sg.targets.forEach((t, i) => priceLines.current.push(S.candles.createPriceLine({ price: t.price, color: "#22c55e", lineStyle: LineStyle.Dashed, lineWidth: 1, title: `TP${i + 1}` })));
      }
    }
    if (tg.pump) {
      const ev = d.pump_events.find((e) => !e.outcome && e.stage !== "REVERSAL");
      if (ev) priceLines.current.push(S.candles.createPriceLine({ price: ev.chase_limit, color: "#a78bfa", lineStyle: LineStyle.Dotted, lineWidth: 1, title: "chase limit — too late beyond" }));
    }
    if (fit) chartRef.current?.timeScale().scrollToRealTime();
  }

  const d = data;
  const ind = d?.indicators || {};
  const lb = legend || (d ? d.forming || d.bars[d.bars.length - 1] : null);
  return (
    <div className="chart-wrap">
      <div className="chart-head">
        <span className="chart-sym">{symbol}</span>
        <div className="toggles">
          {TFS.map((t) => <button key={t} className={t === tf ? "active" : ""} onClick={() => setTf(t)}>{t}</button>)}
        </div>
        {d && <MtfStrip mtf={d.mtf} label="Trend" />}
        {d?.btc_mtf && symbol !== "BTCUSDT" && <MtfStrip mtf={d.btc_mtf} label="BTC" />}
        {d && <CouplingBadge c={d.coupling} symbol={symbol} />}
        {d && <span className="pill">regime: {d.regime.replace("_", " ")}</span>}
        {d?.derivs?.funding !== undefined && <span className="pill">funding {fmtPct((d.derivs.funding ?? 0) * 100, 4)}</span>}
        {d?.derivs?.oi_change_pct !== undefined && <span className="pill">OI 5m {fmtPct(d.derivs.oi_change_pct)}</span>}
        <div className="grow" />
        <div className="toggles">
          {TOGGLE_LABELS.map(([k, label]) => (
            <button key={k} className={toggles[k] ? "active" : ""} onClick={() => setToggles({ ...toggles, [k]: !toggles[k] })}>{label}</button>
          ))}
        </div>
      </div>
      <div className="chart-area">
        <div ref={el} className="chart-el" />
        {lb && (
          <div className="legend">
            <span>{fmtTime(lb.t, true)}</span>
            <span>O <b>{fmtPrice(lb.o)}</b></span><span>H <b>{fmtPrice(lb.h)}</b></span>
            <span>L <b>{fmtPrice(lb.l)}</b></span><span>C <b className={lb.c >= lb.o ? "up" : "dn"}>{fmtPrice(lb.c)}</b></span>
            <span>Δ <b className={2 * lb.tb - lb.v >= 0 ? "up" : "dn"}>{fmtNum(2 * lb.tb - lb.v, 2)}</b></span>
            <span>RSI {fmtNum(ind.rsi as number, 1)}</span><span>ADX {fmtNum(ind.adx as number, 1)}</span>
            <span>ATR {fmtPrice(ind.atr as number)}</span><span>RVOL {fmtNum(ind.rvol as number, 2)}</span>
          </div>
        )}
        {d && <TrendBox d={d} />}
        {hover && <div className="tooltip" style={{ left: Math.min(hover.x + 16, (el.current?.clientWidth || 800) - 340), top: Math.max(8, hover.y - 20) }}>{hover.html}</div>}
        {!d && !err && <div className="loading">Loading {symbol} {tf}…</div>}
        {err && <div className="loading">Could not load chart: {err}</div>}
      </div>
    </div>
  );
}

function TrendBox({ d }: { d: ChartData }) {
  const t = d.trend;
  if (!t.direction || t.state === "NONE") {
    return <div className="trend-box"><span className="muted">Trend Catcher {d.tf}:</span> no trend · score {fmtNum(t.score, 0)}</div>;
  }
  const up = t.direction === "UP";
  return (
    <div className="trend-box">
      <div><b className={up ? "up" : "dn"}>{up ? "▲" : "▼"} {d.tf} {t.state}</b> · score {fmtNum(t.score, 0)}
        {t.p_continue_2atr !== null && <> · P(+2 ATR) <b>{Math.round(t.p_continue_2atr * 100)}%</b></>}
        {t.trail_stop !== null && <> · trail {fmtPrice(t.trail_stop)}</>}
      </div>
      {t.origin && <div className="muted">origin {fmtPrice(t.origin.price)} @ {fmtTime(t.origin.ts)} · detected {t.detected && fmtTime(t.detected.ts)}</div>}
      {t.drivers.length > 0 && <div>{t.drivers.map((x) => <span key={x} className="chip">{x}</span>)}</div>}
      {t.exhaustion.length > 0 && <div className="warn">exhaustion: {t.exhaustion.join(", ")}</div>}
    </div>
  );
}

function snap(ms: number, tf: string): number {
  const step = TF_SEC[tf] * 1000;
  return ms - (ms % step);
}

function buildMarkers(d: ChartData, tf: Tf, tg: Toggles, firstT: number): SeriesMarker<Time>[] {
  const out: SeriesMarker<Time>[] = [];
  const add = (ms: number, m: Omit<SeriesMarker<Time>, "time">) => {
    const t = snap(ms, tf);
    if (t >= firstT) out.push({ ...m, time: sec(t) } as SeriesMarker<Time>);
  };
  if (tg.signals) {
    for (const sg of d.signals) {
      const long = sg.side === "LONG";
      const p = sg.p_win;
      const strong = p !== null && p >= 0.6;
      const color = !sg.validated ? "#6b7280" : long ? (strong ? "#22c55e" : "#4ade80") : (strong ? "#ef4444" : "#f87171");
      const outcome = sg.outcome_r === null ? "" : sg.outcome_r > 0 ? " ✓" : " ✗";
      add(sg.ts, { position: long ? "belowBar" : "aboveBar", shape: long ? "arrowUp" : "arrowDown", color,
        text: `${shortSid(sg.strategy)}${p !== null ? " " + Math.round(p * 100) + "%" : ""}${outcome}`, size: 1.3 });
    }
  }
  if (tg.markers) {
    const recs: TrendRecord[] = [...d.trend_records];
    const t = d.trend;
    if (t.origin && t.detected && t.direction && t.state !== "NONE") {
      recs.push({ id: "cur", symbol: d.symbol, tf: d.tf, direction: t.direction, origin: t.origin, detected: t.detected,
        confirmed: t.confirmed ?? null, max_move_atr: 0, max_move_pct: 0, end: null });
    }
    for (const r of recs) {
      if (!r.confirmed && r.id !== "cur") continue;      // unconfirmed false starts stay off the chart
      const up = r.direction === "UP";
      add(r.origin.ts, { position: up ? "belowBar" : "aboveBar", shape: "circle", color: up ? "#16a34a" : "#dc2626", text: up ? "T▲" : "T▼", size: 0.8 });
      if (r.confirmed) add(r.confirmed.ts, { position: up ? "belowBar" : "aboveBar", shape: "square", color: up ? "#16a34a" : "#dc2626", text: "conf", size: 0.5 });
      if (r.end) add(r.end.ts, { position: up ? "aboveBar" : "belowBar", shape: "square", color: "#f59e0b", text: "", size: 0.4 });
    }
  }
  if (tg.pump) {
    for (const e of d.pump_events) {
      if (!e.ignition) continue;
      add(e.ignition.ts, { position: e.direction === "PUMP" ? "belowBar" : "aboveBar", shape: e.direction === "PUMP" ? "arrowUp" : "arrowDown",
        color: "#a78bfa", text: e.direction === "PUMP" ? "🚀" : "💥", size: 1.6 });
    }
  }
  if (tg.news) {
    for (const n of d.news.slice(0, 20)) {
      add(n.ts, { position: "aboveBar", shape: "circle", color: n.sentiment > 0.2 ? "#22c55e" : n.sentiment < -0.2 ? "#ef4444" : "#94a3b8", text: "📰", size: 0.6 });
    }
  }
  out.sort((a, b) => (a.time as number) - (b.time as number));
  return out;
}

function buildHover(d: ChartData, t: number, x: number, y: number, tg: Toggles): Hover {
  const step = TF_SEC[d.tf] * 1000;
  const inBar = (ms: number) => ms >= t && ms < t + step;
  const sigs: SignalJ[] = tg.signals ? d.signals.filter((sg) => inBar(sg.ts)) : [];
  const pumps: MoveEvent[] = tg.pump ? d.pump_events.filter((e) => e.ignition && inBar(e.ignition.ts)) : [];
  const news: NewsItem[] = tg.news ? d.news.filter((n) => inBar(n.ts)) : [];
  const trends = tg.markers ? d.trend_records.filter((r) => inBar(r.origin.ts) || (r.confirmed && inBar(r.confirmed.ts))) : [];
  if (!sigs.length && !pumps.length && !news.length && !trends.length) return null;
  return {
    x, y,
    html: (
      <>
        {sigs.map((sg) => (
          <div key={sg.id} style={{ marginBottom: 6 }}>
            <h4 className={sg.side === "LONG" ? "up" : "dn"}>{sg.side} · {sid(sg.strategy)} · {sg.tf}</h4>
            <div className="mono">entry {fmtPrice(sg.entry.price)} · SL {fmtPrice(sg.stop)} · TP {sg.targets.map((x) => fmtPrice(x.price)).join(" / ")}</div>
            <div>P(win) <b>{sg.p_win !== null ? `${Math.round(sg.p_win * 100)}%` : "unvalidated"}</b>
              {sg.p_win_ci && <span className="muted"> ({Math.round(sg.p_win_ci[0] * 100)}–{Math.round(sg.p_win_ci[1] * 100)}%)</span>}
              {" "}· EV {sg.expected_R !== null ? `${fmtNum(sg.expected_R, 2)}R` : "—"} · confluence {sg.confluence} · {sg.probability_source}</div>
            <ul>{sg.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
            {sg.risks.length > 0 && <div className="warn">risks: {sg.risks.join("; ")}</div>}
            {sg.drivers.length > 0 && <div className="muted">model drivers: {sg.drivers.join(", ")}</div>}
            <div className="muted">status: {sg.status}{sg.outcome_r !== null && ` · result ${fmtNum(sg.outcome_r, 2)}R`}</div>
          </div>
        ))}
        {pumps.map((e) => (
          <div key={e.id} style={{ marginBottom: 6 }}>
            <h4>{e.direction === "PUMP" ? "🚀" : "💥"} {e.direction} · {e.stage} · {e.classification}</h4>
            <div>move {fmtPct(e.move_pct)} · volume ×{e.vol_x} · chase limit {fmtPrice(e.chase_limit)}</div>
            {e.catalyst && <div>catalyst: {e.catalyst}</div>}
            {e.outcome && <div className="muted">outcome: {e.outcome}</div>}
          </div>
        ))}
        {trends.map((r) => (
          <div key={r.id} style={{ marginBottom: 6 }}>
            <h4 className={r.direction === "UP" ? "up" : "dn"}>Trend {r.direction} ({r.tf})</h4>
            <div>origin {fmtPrice(r.origin.price)} · detected {fmtPrice(r.detected.price)} (lead {r.detected.lead_atr} ATR)</div>
            <div>max move {fmtPct(r.max_move_pct)} ({r.max_move_atr} ATR){r.end && ` · ended: ${r.end.reason}`}</div>
          </div>
        ))}
        {news.map((n) => (
          <div key={n.id}><b>📰 {n.source}</b> [{n.event_type}] {n.title} <span className={n.sentiment > 0 ? "up" : n.sentiment < 0 ? "dn" : "muted"}>({fmtNum(n.sentiment, 2)})</span></div>
        ))}
      </>
    ),
  };
}
