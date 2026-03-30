// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — Trades Store (Zustand)
// ═══════════════════════════════════════════════════════════════

import { create } from 'zustand';
import { fetchTrades } from '../lib/api';
import type { TradeRecord } from '../types/api';

interface TradesState {
  trades: TradeRecord[];
  dailyPnl: number;
  tradeCount: number;
  source: string;
  isLoading: boolean;
  error: string | null;
  fetch: (limit?: number) => Promise<void>;
}

export const useTradesStore = create<TradesState>((set) => ({
  trades: [],
  dailyPnl: 0,
  tradeCount: 0,
  source: 'unavailable',
  isLoading: false,
  error: null,

  fetch: async (limit = 20) => {
    set({ isLoading: true, error: null });
    try {
      const data = await fetchTrades(limit);
      set({
        trades: data.trades,
        dailyPnl: data.daily_pnl,
        tradeCount: data.trade_count,
        source: data.source,
        isLoading: false,
      });
    } catch (err) {
      set({
        isLoading: false,
        error: err instanceof Error ? err.message : 'Failed to fetch trades',
      });
    }
  },
}));
