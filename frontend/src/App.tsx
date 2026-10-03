import { useCallback, useEffect } from "react";
import { post } from "./api";
import BottomTabs from "./components/BottomTabs";
import ChartPanel from "./components/ChartPanel";
import RightColumn from "./components/RightColumn";
import TopBar from "./components/TopBar";
import Watchlist from "./components/Watchlist";
import { useTerminal } from "./useTerminal";

export default function App() {
  const t = useTerminal();
  const { status, wsUp, rows, symbol, setSymbol, tf, setTf, signals, radar, news, events, macroBlock, account,
    coupling, stop, setStop, toast, notify, nextEvent } = t;

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

  const onPickPrice = useCallback((p: number) => setStop(p.toPrecision(8).replace(/\.?0+$/, "")), [setStop]);

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
