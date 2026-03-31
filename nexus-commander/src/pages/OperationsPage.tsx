// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — Operations Page (Tab 2)
//  Live Chart · Trade History · Order Book Imbalance
// ═══════════════════════════════════════════════════════════════

import { useEffect } from 'react';
import PriceChart from '../components/charts/PriceChart';
import { usePriceStore } from '../store/telemetry-store';
import { useTradesStore } from '../store/trades-store';
import { ArrowUpRight, ArrowDownRight, Clock, Activity } from 'lucide-react';

export default function OperationsPage() {
  const { connect: connectPrices, disconnect: disconnectPrices } = usePriceStore();
  const { trades, fetch: fetchTrades } = useTradesStore();

  useEffect(() => {
    connectPrices();
    fetchTrades(20);
    return () => disconnectPrices();
  }, [connectPrices, disconnectPrices, fetchTrades]);

  return (
    <div className="h-full w-full overflow-y-auto">
      <div className="p-6 md:p-8 lg:p-10 flex flex-col min-h-[850px] lg:min-h-full gap-6 animate-fade-in w-full">
        <div className="border-b border-nexus-border pb-4 flex-shrink-0">
          <h1 className="text-2xl font-bold text-nexus-text tracking-tight">Market Operations</h1>
          <p className="text-nexus-text-muted mt-1 text-sm md:text-base">
            Live execution chart and order book telemetry
          </p>
        </div>

        {/* Top: Chart (takes main vertical space) */}
        <div className="flex-[3] min-h-[400px] xl:min-h-[500px]">
          <PriceChart />
        </div>

        {/* Bottom: Trade history + Order Book */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Recent Trades (2/3 width) */}
          <div className="lg:col-span-2 glass-card p-5 flex flex-col min-h-[300px]">
            <div className="flex items-center justify-between mb-4 border-b border-nexus-border/50 pb-3">
              <h3 className="text-sm font-bold text-nexus-text-dim uppercase tracking-wider">Recent Trades</h3>
              <span className="text-xs font-semibold px-2 py-1 rounded bg-nexus-surface-2 text-nexus-text-muted">{trades.length} EXECUTIONS</span>
            </div>

            <div className="flex-1 overflow-y-auto pr-2">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-nexus-surface z-10">
                  <tr className="text-nexus-text-muted uppercase tracking-wider border-b border-nexus-border">
                    <th className="text-left py-3 font-semibold">Time</th>
                    <th className="text-left py-3 font-semibold">Asset</th>
                    <th className="text-left py-3 font-semibold">Direction</th>
                    <th className="text-right py-3 font-semibold">Size</th>
                    <th className="text-right py-3 font-semibold">Conf.</th>
                    <th className="text-right py-3 font-semibold">P&L</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="text-center py-10 text-nexus-text-muted">
                        <div className="flex flex-col items-center gap-2">
                          <Activity className="w-6 h-6 text-nexus-border-light" />
                          <span>No trades yet — awaiting execution signals</span>
                        </div>
                      </td>
                    </tr>
                  ) : (
                    trades.slice(0, 15).map((trade) => {
                      const isUp = trade.direction === 'CALL' || trade.direction === 'BUY';
                      const time = trade.timestamp ? new Date(trade.timestamp).toLocaleTimeString() : '—';
                      return (
                        <tr key={trade.order_id} className="border-b border-nexus-border/30 hover:bg-nexus-surface-2/50 transition-colors">
                          <td className="py-2.5 text-nexus-text-dim font-[family-name:var(--font-mono)]">
                            <div className="flex items-center gap-1.5">
                              <Clock className="w-3.5 h-3.5 text-nexus-border-light" />
                              {time}
                            </div>
                          </td>
                          <td className="py-2.5 text-nexus-text font-bold">{trade.asset}</td>
                          <td className="py-2.5">
                            <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider ${
                              isUp ? 'bg-nexus-green-glow text-nexus-green' : 'bg-nexus-red-glow text-nexus-red'
                            }`}>
                              {isUp ? <ArrowUpRight className="w-3 h-3" /> : <ArrowDownRight className="w-3 h-3" />}
                              {trade.direction}
                            </span>
                          </td>
                          <td className="py-2.5 text-right text-nexus-text font-[family-name:var(--font-mono)]">
                            ${trade.size.toFixed(2)}
                          </td>
                          <td className="py-2.5 text-right text-nexus-cyan font-[family-name:var(--font-mono)] font-bold">
                            {(trade.confidence * 100).toFixed(0)}%
                          </td>
                          <td className={`py-2.5 text-right font-bold text-sm font-[family-name:var(--font-mono)] ${
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
          <div className="glass-card p-5 flex flex-col min-h-[300px]">
            <div className="flex items-center justify-between mb-4 border-b border-nexus-border/50 pb-3">
              <h3 className="text-sm font-bold text-nexus-text-dim uppercase tracking-wider">Book Imbalance</h3>
            </div>

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
                <div key={i} className="relative flex items-center justify-between text-[11px] py-1.5 px-2 rounded">
                  <div
                    className="absolute inset-y-0 rounded opacity-60"
                    style={{
                      backgroundColor: level.side === 'bid' ? 'var(--color-nexus-green-glow)' : 'var(--color-nexus-red-glow)',
                      width: `${level.ratio * 100}%`,
                      [level.side === 'bid' ? 'left' : 'right']: 0,
                    }}
                  />
                  <span className={`relative z-10 font-[family-name:var(--font-mono)] font-bold tracking-tight ${
                    level.side === 'bid' ? 'text-nexus-green' : 'text-nexus-red'
                  }`}>
                    {level.price}
                  </span>
                  <span className="relative z-10 text-nexus-text-muted font-semibold font-[family-name:var(--font-mono)]">
                    {level.size}
                  </span>
                </div>
              ))}
            </div>

            <div className="mt-4 pt-4 border-t border-nexus-border flex items-center justify-between bg-nexus-surface-2/50 -mx-5 -mb-5 px-5 py-3 rounded-b-xl border-x-0 border-b-0">
              <span className="text-xs uppercase tracking-wider font-semibold text-nexus-text-muted">Net Imbalance</span>
              <span className="text-sm font-black text-nexus-green tracking-tight">+12.4% BID</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
