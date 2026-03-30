// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — Operations Page (Tab 2)
//  Live Chart · Trade History · Order Book Imbalance
// ═══════════════════════════════════════════════════════════════

import { useEffect } from 'react';
import PriceChart from '../components/charts/PriceChart';
import { usePriceStore } from '../store/telemetry-store';
import { useTradesStore } from '../store/trades-store';
import { ArrowUpRight, ArrowDownRight, Clock } from 'lucide-react';

export default function OperationsPage() {
  const { connect: connectPrices, disconnect: disconnectPrices } = usePriceStore();
  const { trades, fetch: fetchTrades } = useTradesStore();

  useEffect(() => {
    connectPrices();
    fetchTrades(20);
    return () => disconnectPrices();
  }, [connectPrices, disconnectPrices, fetchTrades]);

  return (
    <div className="p-6 flex flex-col h-full gap-4 animate-fade-in">
      {/* Top: Chart (takes 60% of the vertical space) */}
      <div className="flex-[3] min-h-0">
        <PriceChart />
      </div>

      {/* Bottom: Trade history + Order Book */}
      <div className="flex-[2] min-h-0 grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Recent Trades (2/3 width) */}
        <div className="lg:col-span-2 glass-card p-4 flex flex-col">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold text-nexus-text">Recent Trades</h3>
            <span className="text-xs text-nexus-text-muted">{trades.length} trades</span>
          </div>

          <div className="flex-1 overflow-y-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-nexus-text-muted uppercase tracking-wider border-b border-nexus-border">
                  <th className="text-left py-2 font-medium">Time</th>
                  <th className="text-left py-2 font-medium">Asset</th>
                  <th className="text-left py-2 font-medium">Direction</th>
                  <th className="text-right py-2 font-medium">Size</th>
                  <th className="text-right py-2 font-medium">Conf.</th>
                  <th className="text-right py-2 font-medium">P&L</th>
                </tr>
              </thead>
              <tbody>
                {trades.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="text-center py-8 text-nexus-text-muted">
                      No trades yet — waiting for signals
                    </td>
                  </tr>
                ) : (
                  trades.slice(0, 15).map((trade) => {
                    const isUp = trade.direction === 'CALL' || trade.direction === 'BUY';
                    const time = trade.timestamp ? new Date(trade.timestamp).toLocaleTimeString() : '—';
                    return (
                      <tr key={trade.order_id} className="border-b border-nexus-border/50 hover:bg-nexus-surface-2/50 transition-colors">
                        <td className="py-2 text-nexus-text-dim font-[family-name:var(--font-mono)]">
                          <div className="flex items-center gap-1">
                            <Clock className="w-3 h-3" />
                            {time}
                          </div>
                        </td>
                        <td className="py-2 text-nexus-text font-medium">{trade.asset}</td>
                        <td className="py-2">
                          <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold uppercase ${
                            isUp ? 'bg-nexus-green/10 text-nexus-green' : 'bg-nexus-red/10 text-nexus-red'
                          }`}>
                            {isUp ? <ArrowUpRight className="w-3 h-3" /> : <ArrowDownRight className="w-3 h-3" />}
                            {trade.direction}
                          </span>
                        </td>
                        <td className="py-2 text-right text-nexus-text font-[family-name:var(--font-mono)]">
                          ${trade.size.toFixed(2)}
                        </td>
                        <td className="py-2 text-right text-nexus-cyan font-[family-name:var(--font-mono)]">
                          {(trade.confidence * 100).toFixed(0)}%
                        </td>
                        <td className={`py-2 text-right font-bold font-[family-name:var(--font-mono)] ${
                          trade.payout >= 0 ? 'text-nexus-green' : 'text-nexus-red'
                        }`}>
                          {trade.payout >= 0 ? '+' : ''}${trade.payout.toFixed(2)}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>

        {/* Order Book Placeholder (1/3 width) */}
        <div className="glass-card p-4 flex flex-col">
          <h3 className="text-sm font-semibold text-nexus-text mb-3">Order Book Imbalance</h3>

          <div className="flex-1 flex flex-col justify-center gap-2">
            {/* Mock order book visualization */}
            {[
              { side: 'ask', ratio: 0.35, price: '1.08520', size: '1.2M' },
              { side: 'ask', ratio: 0.55, price: '1.08515', size: '2.1M' },
              { side: 'ask', ratio: 0.45, price: '1.08510', size: '1.8M' },
              { side: 'bid', ratio: 0.65, price: '1.08505', size: '2.5M' },
              { side: 'bid', ratio: 0.40, price: '1.08500', size: '1.5M' },
              { side: 'bid', ratio: 0.30, price: '1.08495', size: '1.1M' },
            ].map((level, i) => (
              <div key={i} className="relative flex items-center justify-between text-[11px] py-1 px-2 rounded">
                <div
                  className="absolute inset-y-0 rounded"
                  style={{
                    backgroundColor: level.side === 'bid' ? '#10B98115' : '#EF444415',
                    width: `${level.ratio * 100}%`,
                    [level.side === 'bid' ? 'left' : 'right']: 0,
                  }}
                />
                <span className={`relative z-10 font-[family-name:var(--font-mono)] ${
                  level.side === 'bid' ? 'text-nexus-green' : 'text-nexus-red'
                }`}>
                  {level.price}
                </span>
                <span className="relative z-10 text-nexus-text-muted font-[family-name:var(--font-mono)]">
                  {level.size}
                </span>
              </div>
            ))}
          </div>

          <div className="mt-3 pt-3 border-t border-nexus-border flex items-center justify-between">
            <span className="text-xs text-nexus-text-muted">Imbalance</span>
            <span className="text-xs font-bold text-nexus-green">+12.4% BID</span>
          </div>
        </div>
      </div>
    </div>
  );
}
