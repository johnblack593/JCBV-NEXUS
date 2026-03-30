// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — API Response Types (Strict TypeScript)
// ═══════════════════════════════════════════════════════════════

export interface TokenResponse {
  access_token: string;
  token_type: 'bearer';
  expires_in_hours: number;
}

export interface StateResponse {
  macro_regime: MacroRegime;
  circuit_breaker_active: boolean;
  panic_mode: boolean;
  ai_mode: boolean;
  execution_venue: ExecutionVenue;
  account_type: AccountType;
  timestamp: string;
}

export interface TradeRecord {
  order_id: string;
  venue: string;
  asset: string;
  direction: 'CALL' | 'PUT' | 'BUY' | 'SELL';
  size: number;
  price: number;
  payout: number;
  status: string;
  confidence: number;
  regime: string;
  timestamp: string;
}

export interface TradesResponse {
  trades: TradeRecord[];
  daily_pnl: number;
  trade_count: number;
  source: string;
}

export interface RiskSettingsRequest {
  base_size?: number;
  max_daily_trades?: number;
  min_confidence?: number;
  cooldown_between_trades_s?: number;
  min_payout?: number;
}

export interface VenueRequest {
  venue: ExecutionVenue;
}

export interface AccountTypeRequest {
  account_type: AccountType;
  confirm: boolean;
}

export interface AIModeRequest {
  enabled: boolean;
}

export interface HealthResponse {
  status: string;
  redis: 'connected' | 'offline';
  version: string;
  uptime_s: number;
}

export type MacroRegime = 'GREEN' | 'YELLOW' | 'RED';
export type ExecutionVenue = 'IQ_OPTION' | 'BINANCE';
export type AccountType = 'PRACTICE' | 'REAL';
