// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — Constants & Configuration
// ═══════════════════════════════════════════════════════════════

const isDev = import.meta.env.DEV;

/** API base URL — proxied through Vite in dev, direct in prod */
export const API_BASE = isDev ? '/api' : (import.meta.env.VITE_API_URL ?? 'http://localhost:8000');

/** WebSocket base URL */
export const WS_BASE = isDev
  ? `ws://${window.location.host}/ws`
  : (import.meta.env.VITE_WS_URL ?? 'ws://localhost:8000/ws');

/** WebSocket endpoints */
export const WS_ENDPOINTS = {
  TELEMETRY: `${WS_BASE}/telemetry`,
  PRICES:    `${WS_BASE}/prices`,
  LOGS:      `${WS_BASE}/logs`,
} as const;

/** REST endpoints */
export const ENDPOINTS = {
  LOGIN:          `${API_BASE}/login`,
  STATE:          `${API_BASE}/state`,
  TRADES:         `${API_BASE}/trades`,
  HEALTH:         `${API_BASE}/health`,
  PANIC:          `${API_BASE}/panic`,
  PANIC_RESET:    `${API_BASE}/panic/reset`,
  RISK_SETTINGS:  `${API_BASE}/settings/risk`,
  AI_MODE:        `${API_BASE}/settings/ai-mode`,
  VENUE:          `${API_BASE}/settings/venue`,
  ACCOUNT_TYPE:   `${API_BASE}/settings/account-type`,
} as const;

/** Design system colors (matching CSS variables) */
export const COLORS = {
  BG:          '#0B0E14',
  SURFACE:     '#111827',
  SURFACE_2:   '#1a2332',
  BORDER:      '#1F2937',
  BORDER_LT:   '#374151',
  TEXT:         '#F9FAFB',
  TEXT_DIM:     '#9CA3AF',
  TEXT_MUTED:   '#6B7280',
  CYAN:        '#06B6D4',
  PURPLE:      '#8B5CF6',
  RED:         '#EF4444',
  GREEN:       '#10B981',
  AMBER:       '#F59E0B',
} as const;

/** Regime ↔ Color mapping */
export const REGIME_COLORS: Record<string, string> = {
  GREEN:  COLORS.GREEN,
  YELLOW: COLORS.AMBER,
  RED:    COLORS.RED,
};

/** Regime labels */
export const REGIME_LABELS: Record<string, string> = {
  GREEN:  'BULLISH',
  YELLOW: 'CAUTION',
  RED:    'BEARISH',
};
