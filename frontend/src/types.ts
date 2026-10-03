export type Tf = "1m" | "5m" | "15m" | "1h" | "4h";
export const TFS: Tf[] = ["1m", "5m", "15m", "1h", "4h"];
export const TF_SEC: Record<string, number> = { "1s": 1, "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400 };

export interface BarJ { t: number; o: number; h: number; l: number; c: number; v: number; q: number; n: number; tb: number; x: boolean }

export interface TrendCell { dir: number; state: string; score?: number; since?: number | null }

export interface WatchRow {
  symbol: string; base: string; price: number; change_pct: number; quote_volume: number;
  trend?: Record<string, TrendCell>; coupling?: string | null; rvol_5m?: number; atr_pct_5m?: number;
  tracked?: boolean; pump?: { stage: string; direction: string; move_pct: number }; signals: number; opportunity: number;
}

export interface Target { price: number; size_pct: number }

export interface SignalJ {
  id: string; ts: number; symbol: string; tf: string; side: "LONG" | "SHORT"; strategy: string;
  entry: { type: string; price: number }; stop: number; targets: Target[]; time_stop_bars: number; trailing: boolean;
  p_win: number | null; p_win_ci: [number, number] | null; expected_R: number | null; confluence: number;
  confluence_parts: Record<string, number>; regime: string; reasons: string[]; risks: string[];
  validated: boolean; probability_source: string; status: string; outcome_r: number | null; drivers: string[];
}

export interface TrendSnap {
  symbol: string; tf: string; state: string; direction: "UP" | "DOWN" | null; score: number;
  evidence: Record<string, number>; drivers: string[]; exhaustion: string[]; trail_stop: number | null;
  p_continue_2atr: number | null; btc_coupling: string | null;
  origin?: { ts: number; price: number }; detected?: { ts: number; price: number; lead_atr: number };
  confirmed?: { ts: number; price: number } | null; trend_id?: string;
  channel?: { base: [number, number][]; parallel: [number, number][] } | null;
}

export interface TrendRecord {
  id: string; symbol: string; tf: string; direction: "UP" | "DOWN";
  origin: { ts: number; price: number }; detected: { ts: number; price: number; lead_atr: number };
  confirmed: { ts: number; price: number } | null; max_move_atr: number; max_move_pct: number;
  end: { ts: number; price: number; reason: string } | null;
}

export interface MoveEvent {
  id: string; symbol: string; direction: "PUMP" | "DUMP"; stage: string;
  origin: { ts: number; price: number }; ignition: { ts: number; price: number } | null;
  confirmed: { ts: number; price: number } | null; extreme: number; last: number; move_pct: number;
  current_pct: number; vwap: number; vol_x: number; chase_limit: number; classification: string;
  catalyst: string | null; features: Record<string, number | string>; exhaustion: string[]; outcome: string | null;
}

export interface Coupling {
  symbol: string; rho: number; beta: number; r2: number; spearman: number; rho_24h?: number; beta_24h?: number;
  residual_session_pct?: number; residual_1h_pct?: number; residual_1h_z?: number;
  lag_s?: number; lag_corr?: number; lag_significant?: boolean; label: string;
}

export interface NewsItem { id: string; ts: number; source: string; title: string; url: string; symbols: string[]; event_type: string; sentiment: number; impact: number }
export interface CalEvent { ts: number; title: string; country: string; impact: string; forecast?: string; previous?: string; actual?: string | null }

export interface ChartData {
  symbol: string; tf: string; bars: BarJ[]; forming: BarJ | null;
  series: Record<string, (number | null)[]>;
  trend_history: [number, number, number, number, number | null][];
  trend: TrendSnap; trend_records: TrendRecord[];
  mtf: Record<string, TrendCell>; btc_mtf: Record<string, TrendCell> | null;
  coupling: Coupling | null; btc_overlay: [number, number | null][] | null;
  signals: SignalJ[]; pump_events: MoveEvent[]; news: NewsItem[];
  derivs: Record<string, number> | null; book: BookMetrics | null; regime: string;
  indicators: Record<string, number | null>;
}

export interface BookMetrics {
  bid: number; ask: number; mid: number; spread_bps: number; microprice: number; obi: number; obi_10bps: number;
  pressure_1pct: number; pressure_z: number; depth_10bps_notional: number; intensity_z?: number; aggr_buy_share?: number;
}

export interface Position {
  id: string; symbol: string; side: string; qty: number; entry: number; stop: number; targets: Target[];
  status: string; remaining: number; last: number; unrealized: number; pnl: number; r: number;
  risk_amount: number; opened_at: number; closed_at: number | null; exit_reason: string | null;
  signal_id: string | null; source: string; strategy: string;
}

export interface Account {
  mode: string; market: string; equity: number; cash: number; open: Position[]; history: Position[];
  risk: { blocked: string | null; realized_today: number; daily_loss_limit: number; consecutive_losses: number; killed: boolean; cooldown_s: number; config: Record<string, number | boolean> };
}

export interface Status {
  phase: string; uptime_s: number; market: string; execution_mode: string; trend_symbols: number; radar_symbols: number;
  ws: { connections: number; connected: number; streams: number; messages: number; reconnects: number; max_silence_s: number };
  futures_data: boolean; clock_offset_ms: number; weight_used: number;
  news_health: Record<string, { ok: boolean; items?: number; error?: string }>;
  alerts_enabled: boolean; model_loaded: boolean; errors: { ts: number; error: string }[];
  fear_greed: { value: number; label: string; previous: number | null } | null;
  bootstrap: { status?: string; symbols?: string[]; days?: number; error?: string };
}

export interface StratStats { n: number; win_rate: number | null; p_win: number; p_win_ci: [number, number]; expectancy_r: number; profit_factor: number | null; recent_expectancy_r: number }
export interface StrategyInfo {
  id: string; version: string; description: string; timeframes: string[]; regimes: string[]; needs: string[];
  stats: { backtest: StratStats; live: StratStats; shadow: string | null } | null;
  model: Record<string, unknown> | null;
}

export interface TrendBoard {
  tfs: string[];
  rows: { symbol: string; cells: Record<string, TrendCell>; alignment: number; direction: string | null; newest: number; coupling: string | null }[];
  stats: Record<string, { detected: number; confirmed: number; false_starts: number; confirmed_reached_2atr: StratStats; median_lead_atr: number | null }>;
}
