// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — WebSocket Message Types
// ═══════════════════════════════════════════════════════════════

import type { MacroRegime } from './api';

export interface TelemetryFrame {
  type: 'telemetry';
  macro_regime: MacroRegime;
  circuit_breaker: boolean;
  panic_mode: boolean;
  ai_mode: boolean;
  execution_venue: string;
  account_type: string;
  timestamp: string;
}

export interface PriceFrame {
  type: 'price';
  asset: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  timestamp: string;
}

export interface LogFrame {
  type: 'log';
  line: string;
  timestamp: string;
}

export type WSFrame = TelemetryFrame | PriceFrame | LogFrame;

export type WSConnectionStatus =
  | 'CONNECTING'
  | 'CONNECTED'
  | 'RECONNECTING'
  | 'DISCONNECTED';
