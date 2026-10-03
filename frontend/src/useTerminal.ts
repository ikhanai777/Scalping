// Shared live data for the desktop and mobile layouts: initial loads, polling and push updates.
import { useCallback, useEffect, useRef, useState } from "react";
import { get, push } from "./api";
import { fmtPrice } from "./format";
import type { Account, CalEvent, Coupling, MoveEvent, NewsItem, SignalJ, Status, Tf, WatchRow } from "./types";

export function stored<T extends string>(key: string, def: T): T {
  try { return (localStorage.getItem(key) as T) || def; } catch { return def; }
}

export type Toast = { msg: string; bad: boolean } | null;

export function useTerminal() {
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
  const [toast, setToast] = useState<Toast>(null);
  const toastTimer = useRef<number | null>(null);

  const notify = useCallback((msg: string, bad = false) => {
    setToast({ msg, bad });
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 4000);
  }, []);

  const loadAccount = useCallback(() => get<Account>("/api/account").then(setAccount).catch(() => undefined), []);

  useEffect(() => { try { localStorage.setItem("symbol", symbol); localStorage.setItem("tf", tf); } catch { /* ignore */ } }, [symbol, tf]);
  useEffect(() => setStop(""), [symbol]);

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
    get<Status>("/api/status").then(setStatus).catch(() => undefined);
    get<{ active: MoveEvent[] }>("/api/radar").then((d) => setRadar(d.active)).catch(() => undefined);
    get<Coupling[]>("/api/coupling").then(setCoupling).catch(() => undefined);
    loadSignals(); loadNews(); loadRows(); loadAccount();
    // Poll less when the page is hidden (saves battery and data on phones).
    const every = (fn: () => void, ms: number) => window.setInterval(() => { if (!document.hidden) fn(); }, ms);
    const timers = [every(loadRows, 5000), every(loadAccount, 3000), every(loadNews, 60000), every(loadSignals, 30000)];
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
    return () => { off(); timers.forEach((t) => window.clearInterval(t)); };
  }, [notify, loadAccount]);

  const nextEvent = events.find((e) => e.impact === "High" && e.ts > Date.now()) || null;
  return { status, wsUp, rows, symbol, setSymbol, tf, setTf, signals, radar, news, events, macroBlock, account,
    coupling, stop, setStop, toast, notify, nextEvent, loadAccount };
}

export type Terminal = ReturnType<typeof useTerminal>;
