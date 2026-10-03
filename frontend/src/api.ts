// REST helpers and a reconnecting WebSocket client for the backend push channel.

export async function get<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json() as Promise<T>;
}

export async function post<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    let msg = await r.text();
    try { msg = JSON.parse(msg).detail ?? msg; } catch { /* plain text */ }
    throw new Error(msg);
  }
  return r.json() as Promise<T>;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type WsMsg = { type: string; [k: string]: any };
type Listener = (m: WsMsg) => void;

class Push {
  private ws: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private backoff = 1000;
  connected = false;
  private focus: string | null = null;

  start() {
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) return;   // already connecting/connected (StrictMode)
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.ws = new WebSocket(`${proto}://${location.host}/ws`);
    this.ws.onopen = () => {
      this.connected = true;
      this.backoff = 1000;
      if (this.focus) this.send({ type: "focus", symbol: this.focus });
      this.emit({ type: "_ws", connected: true });
    };
    this.ws.onmessage = (e) => {
      try { this.emit(JSON.parse(e.data)); } catch { /* ignore */ }
    };
    this.ws.onclose = () => {
      this.connected = false;
      this.emit({ type: "_ws", connected: false });
      setTimeout(() => this.start(), this.backoff);
      this.backoff = Math.min(this.backoff * 2, 15000);
    };
  }

  send(m: WsMsg) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(m));
  }

  setFocus(symbol: string) {
    this.focus = symbol;
    this.send({ type: "focus", symbol });
  }

  on(l: Listener): () => void {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  }

  private emit(m: WsMsg) {
    this.listeners.forEach((l) => l(m));
  }
}

export const push = new Push();
