import { useCallback, useEffect, useRef, useState } from "react";
import { get, post, push } from "./api";
import BottomTabs from "./components/BottomTabs";
import ChartPanel from "./components/ChartPanel";
import RightColumn from "./components/RightColumn";
import TopBar from "./components/TopBar";
import Watchlist from "./components/Watchlist";
import { fmtPrice } from "./format";
import type { Account, CalEvent, Coupling, MoveEvent, NewsItem, SignalJ, Status, Tf, WatchRow } from "./types";

function stored<T extends string>(key: string, def: T): T {
  try { return (localStorage.getItem(key) as T) || def; } catch { return def; }
}

export default function App() {
  const [status, setStatus] = useState<Status | null>(null);
  const [wsUp, setWsUp] = useState(false);
  const [rows, setRows] = useState<WatchRow[]>([]);
  const [symbol, setSymbol] = useState<string>(() => stored("symbol", "BTCUSDT"));
  const [tf, setTf] = useState<Tf>(() => stored<Tf>("tf", "5m"));
  const [signals, setSignals] = useState<SignalJ[]>([]);
  const [radar, setRadar] = useState<MoveEvent[]>([]);
  const [news, setNews] = useState<NewsItem[]>([]);
  const [events, setEvents] = useState<CalEvent[]>([]);
  const [macroBlock, setMacroBlock] = useState(false);
  const [account, setAccount] = useState<Account | null>(null);
  const [coupling, setCoupling] = useState<Coupling[]>([]);
  const [stop, setStop] = useState("");
  const [toast, setToast] = useState<{ msg: string; bad: boolean } | null>(null);
  const toastTimer = useRef<number | null>(null);

  const notify = useCallback((msg: string, bad = false) => {
    setToast({ msg, bad });
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 4000);
  }, []);

  useEffect(() => { try { localStorage.setItem("symbol", symbol); localStorage.setItem("tf", tf); } catch { /* ignore */ } }, [symbol, tf]);
  useEffect(() => setStop(""), [symbol]);

  // initial loads + polling
  useEffect(() => {
    push.start();
    const loadSignals = () => get<{ active: SignalJ[]; recent: SignalJ[] }>("/api/signals").then((d) => {
      const m = new Map<string, SignalJ>();
      [...d.recent, ...d.active].filter((s) => s && s.strategy).forEach((s) => m.set(s.id, s));
      setSignals([...m.values()].sort((a, b) => b.ts - a.ts));
    }).catch(() => undefined);
    const loadNews = () => get<{ items: NewsItem[]; events: CalEvent[]; macro_block_now: unknown }>("/api/news").then((d) => {
      setNews(d.items); setEvents(d.events); setMacroBlock(!!d.macro_block_now);
    }).catch(() => undefined);
    const loadRows = () => get<WatchRow[]>("/api/watchlist").then(setRows).catch(() => undefined);
    const loadAccount = () => get<Account>("/api/account").then(setAccount).catch(() => undefined);
    get<Status>("/api/status").then(setStatus).catch(() => undefined);
    get<{ active: MoveEvent[] }>("/api/radar").then((d) => setRadar(d.active)).catch(() => undefined);
    get<Coupling[]>("/api/coupling").then(setCoupling).catch(() => undefined);
    loadSignals(); loadNews(); loadRows(); loadAccount();
    const t1 = window.setInterval(loadRows, 5000);
    const t2 = window.setInterval(loadAccount, 3000);
    const t3 = window.setInterval(loadNews, 60000);
    const t4 = window.setInterval(loadSignals, 30000);
    const off = push.on((m) => {
      switch (m.type) {
        case "_ws": setWsUp(m.connected); break;
        case "status": setStatus(m.data); break;
        case "radar": setRadar(m.data); break;
        case "coupling": setCoupling(m.data); break;
        case "signal":
        case "signal_update":
          setSignals((prev) => [m.data, ...prev.filter((s) => s.id !== m.data.id)].sort((a, b) => b.ts - a.ts).slice(0, 400));
          if (m.type === "signal") notify(`${m.data.side} ${m.data.symbol} · ${m.data.strategy.split("@")[0]} ${m.data.tf} @ ${fmtPrice(m.data.entry.price)}`);
          break;
        case "news": setNews((prev) => [m.data, ...prev.filter((n) => n.id !== m.data.id)].slice(0, 300)); break;
        case "pump":
          if (m.data.stage === "IGNITION" || m.data.stage === "CONFIRMED") notify(`${m.data.direction === "PUMP" ? "🚀" : "💥"} ${m.data.symbol} ${m.data.direction} ${m.data.stage}`);
          break;
        case "position_opened": case "position_closed": case "kill": loadAccount(); break;
      }
    });
    return () => { off(); [t1, t2, t3, t4].forEach((t) => window.clearInterval(t)); };
  }, [notify]);

  // hotkeys
  useEffect(() => {
    const onKey = async (e: KeyboardEvent) => {
      if (!e.shiftKey || (e.target as HTMLElement)?.tagName === "INPUT") return;
      const k = e.key.toUpperCase();
      try {
        if (k === "B" || k === "S") {
          await post("/api/orders", { symbol, side: k === "B" ? "LONG" : "SHORT", stop: parseFloat(stop) || null });
          notify(`Paper ${k === "B" ? "LONG" : "SHORT"} ${symbol} opened`);
        } else if (k === "X") {
          const open = account?.open.filter((p) => p.symbol === symbol) ?? [];
          for (const p of open) await post(`/api/positions/${p.id}/close`);
          notify(`Closed ${open.length} position(s) on ${symbol}`);
        }
      } catch (err) { notify(String(err).replace("Error: ", ""), true); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [symbol, stop, account, notify]);

  const onPickPrice = useCallback((p: number) => setStop(p.toPrecision(8).replace(/\.?0+$/, "")), []);
  const nextEvent = events.find((e) => e.impact === "High" && e.ts > Date.now()) || null;

  return (
    <div className="app">
      <TopBar status={status} wsUp={wsUp} btc={rows.find((r) => r.symbol === "BTCUSDT")} account={account} nextEvent={nextEvent} macroBlock={macroBlock} />
      <div className="main">
        <Watchlist rows={rows} selected={symbol} onSelect={setSymbol} />
        <div className="center">
          {status && status.phase !== "live" && status.phase !== "error" && (
            <div className="banner" style={{ position: "absolute", zIndex: 30, left: 270, right: 300 }}>Engine {status.phase}: loading market history and connecting streams…</div>
          )}
          <ChartPanel symbol={symbol} tf={tf} setTf={setTf} onPickPrice={onPickPrice} />
          <BottomTabs signals={signals} radar={radar} news={news} events={events} account={account} coupling={coupling} onSelect={setSymbol} notify={notify} />
        </div>
        <RightColumn symbol={symbol} account={account} stop={stop} setStop={setStop} notify={notify} />
      </div>
      {toast && (
        <div style={{ position: "fixed", right: 14, bottom: 14, zIndex: 50, padding: "8px 12px", borderRadius: 6,
          background: toast.bad ? "#3b1212" : "#10261a", border: `1px solid ${toast.bad ? "#7f1d1d" : "#166534"}`, maxWidth: 420 }}>
          {toast.msg}
        </div>
      )}
    </div>
  );
}
