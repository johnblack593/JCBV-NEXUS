// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — Telemetry Store (Zustand + WebSocket)
//  Zero-polling. Pure WebSocket state management.
// ═══════════════════════════════════════════════════════════════

import { create } from 'zustand';
import { WSEngine } from '../lib/ws-engine';
import { WS_ENDPOINTS, ENDPOINTS } from '../lib/constants';
import type { WSConnectionStatus, TelemetryFrame, PriceFrame, LogFrame, WSFrame } from '../types/ws';
import type { MacroRegime } from '../types/api';

// ── Telemetry Store ─────────────────────────────────────────────

interface TelemetryState {
  // Pipeline state (from /ws/telemetry)
  macroRegime: MacroRegime;
  circuitBreaker: boolean;
  panicMode: boolean;
  aiMode: boolean;
  executionVenue: string;
  accountType: string;
  lastTelemetryTimestamp: string;

  // Connection state
  wsStatus: WSConnectionStatus;
  latencyMs: number;

  // Actions
  connect: () => void;
  disconnect: () => void;
}

let telemetryEngine: WSEngine | null = null;

export const useTelemetryStore = create<TelemetryState>((set) => ({
  macroRegime: 'GREEN',
  circuitBreaker: false,
  panicMode: false,
  aiMode: false,
  executionVenue: 'IQ_OPTION',
  accountType: 'PRACTICE',
  lastTelemetryTimestamp: '',

  wsStatus: 'DISCONNECTED',
  latencyMs: 0,

  connect: () => {
    if (telemetryEngine) return;

    telemetryEngine = new WSEngine({
      url: WS_ENDPOINTS.TELEMETRY,
      onFrame: (frame: WSFrame) => {
        if (frame.type === 'telemetry') {
          const tf = frame as TelemetryFrame;
          set({
            macroRegime: tf.macro_regime,
            circuitBreaker: tf.circuit_breaker,
            panicMode: tf.panic_mode,
            aiMode: tf.ai_mode,
            executionVenue: tf.execution_venue,
            accountType: tf.account_type,
            lastTelemetryTimestamp: tf.timestamp,
            latencyMs: telemetryEngine?.latencyMs ?? 0,
          });
        }
      },
      onStatus: (status: WSConnectionStatus) => {
        set({ wsStatus: status });
      },
    });

    telemetryEngine.connect();
  },

  disconnect: () => {
    if (telemetryEngine) {
      telemetryEngine.disconnect();
      telemetryEngine = null;
    }
  },
}));


// ── Price Store (for /ws/prices) ────────────────────────────────

interface PriceTick {
  time: number; // Unix timestamp in seconds
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

interface PriceState {
  asset: string;
  currentPrice: number;
  ticks: PriceTick[];
  wsStatus: WSConnectionStatus;
  connect: () => void;
  disconnect: () => void;
}

let priceEngine: WSEngine | null = null;
const MAX_TICKS = 500;

export const usePriceStore = create<PriceState>((set, get) => ({
  asset: 'EURUSD',
  currentPrice: 0,
  ticks: [],
  wsStatus: 'DISCONNECTED',

  connect: async () => {
    if (priceEngine) return;

    // Fetch initial historical candles from QuestDB
    try {
      const token = localStorage.getItem('nexus_token');
      if (token) {
        const res = await fetch(`${ENDPOINTS.CANDLES}?asset=EURUSD&timeframe=1m&limit=100`, {
          headers: { Authorization: `Bearer ${token}` }
        });
        if (res.ok) {
          const data = await res.json();
          if (data.candles && data.candles.length > 0) {
            // deduplicate and ensure strictly ascending
            let lastT = 0;
            const validCandles = data.candles.filter((c: any) => {
              if (c.time > lastT) {
                lastT = c.time;
                return true;
              }
              return false;
            });
            set({ ticks: validCandles, asset: data.asset });
          }
        }
      }
    } catch (err) {
      console.warn('Failed to fetch historical candles', err);
    }

    priceEngine = new WSEngine({
      url: WS_ENDPOINTS.PRICES,
      onFrame: (frame: WSFrame) => {
        if (frame.type === 'price') {
          const pf = frame as PriceFrame;
          const tick: PriceTick = {
            time: Math.floor(new Date(pf.timestamp).getTime() / 1000),
            open: pf.open,
            high: pf.high,
            low: pf.low,
            close: pf.close,
            volume: pf.volume,
          };

          const prev = get().ticks;
          const updated = prev.length >= MAX_TICKS
            ? [...prev.slice(-MAX_TICKS + 1), tick]
            : [...prev, tick];

          set({
            asset: pf.asset,
            currentPrice: pf.close,
            ticks: updated,
          });
        }
      },
      onStatus: (status: WSConnectionStatus) => {
        set({ wsStatus: status });
      },
    });

    priceEngine.connect();
  },

  disconnect: () => {
    if (priceEngine) {
      priceEngine.disconnect();
      priceEngine = null;
    }
  },
}));


// ── Logs Store (for /ws/logs) ───────────────────────────────────

interface LogEntry {
  line: string;
  timestamp: string;
}

interface LogsState {
  entries: LogEntry[];
  wsStatus: WSConnectionStatus;
  connect: () => void;
  disconnect: () => void;
  clear: () => void;
}

let logsEngine: WSEngine | null = null;
const MAX_LOG_ENTRIES = 500;

export const useLogsStore = create<LogsState>((set, get) => ({
  entries: [],
  wsStatus: 'DISCONNECTED',

  connect: () => {
    if (logsEngine) return;

    logsEngine = new WSEngine({
      url: WS_ENDPOINTS.LOGS,
      onFrame: (frame: WSFrame) => {
        if (frame.type === 'log') {
          const lf = frame as LogFrame;
          const entry: LogEntry = {
            line: lf.line,
            timestamp: lf.timestamp,
          };

          const prev = get().entries;
          const updated = prev.length >= MAX_LOG_ENTRIES
            ? [...prev.slice(-MAX_LOG_ENTRIES + 1), entry]
            : [...prev, entry];

          set({ entries: updated });
        }
      },
      onStatus: (status: WSConnectionStatus) => {
        set({ wsStatus: status });
      },
    });

    logsEngine.connect();
  },

  disconnect: () => {
    if (logsEngine) {
      logsEngine.disconnect();
      logsEngine = null;
    }
  },

  clear: () => {
    set({ entries: [] });
  },
}));
