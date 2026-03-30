// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — Overview Page (Tab 1)
//  Auth & Global Overview · PnL · Venue/Account Controls
// ═══════════════════════════════════════════════════════════════

import { useEffect } from 'react';
import { useTelemetryStore } from '../store/telemetry-store';
import { useTradesStore } from '../store/trades-store';
import PanicButton from '../components/controls/PanicButton';
import VenueSwitch from '../components/controls/VenueSwitch';
import { REGIME_COLORS, REGIME_LABELS } from '../lib/constants';
import {
  TrendingUp,
  TrendingDown,
  BarChart3,
  Activity,
  Shield,
  Cpu,
  Timer,
} from 'lucide-react';

export default function OverviewPage() {
  const { macroRegime, panicMode, aiMode, circuitBreaker, executionVenue, accountType } = useTelemetryStore();
  const { dailyPnl, tradeCount, fetch: fetchTrades, source } = useTradesStore();

  const regimeColor = panicMode ? '#EF4444' : (REGIME_COLORS[macroRegime] ?? '#10B981');
  const regimeLabel = panicMode ? 'PANIC HALT' : (REGIME_LABELS[macroRegime] ?? macroRegime);

  useEffect(() => {
    fetchTrades(50);
    const interval = setInterval(() => fetchTrades(50), 30000);
    return () => clearInterval(interval);
  }, [fetchTrades]);

  const statCards = [
    {
      label: 'Daily P&L',
      value: `$${dailyPnl.toFixed(2)}`,
      icon: dailyPnl >= 0 ? TrendingUp : TrendingDown,
      color: dailyPnl >= 0 ? '#10B981' : '#EF4444',
    },
    {
      label: 'Trades Today',
      value: tradeCount.toString(),
      icon: BarChart3,
      color: '#06B6D4',
    },
    {
      label: 'Macro Regime',
      value: regimeLabel,
      icon: Activity,
      color: regimeColor,
    },
    {
      label: 'Circuit Breaker',
      value: circuitBreaker ? 'TRIPPED' : 'OK',
      icon: Shield,
      color: circuitBreaker ? '#EF4444' : '#10B981',
    },
    {
      label: 'AI Mode',
      value: aiMode ? 'ACTIVE' : 'OFF',
      icon: Cpu,
      color: aiMode ? '#8B5CF6' : '#6B7280',
    },
    {
      label: 'Data Source',
      value: source === 'questdb' ? 'QuestDB' : 'Offline',
      icon: Timer,
      color: source === 'questdb' ? '#10B981' : '#F59E0B',
    },
  ];

  return (
    <div className="p-6 space-y-6 overflow-y-auto h-full animate-fade-in">
      {/* Page title */}
      <div>
        <h1 className="text-xl font-bold text-nexus-text">Command Center</h1>
        <p className="text-sm text-nexus-text-muted mt-0.5">
          Real-time pipeline overview and global controls
        </p>
      </div>

      {/* Stat Cards Grid */}
      <div className="grid grid-cols-2 lg:grid-cols-3 gap-3">
        {statCards.map((card) => {
          const Icon = card.icon;
          return (
            <div key={card.label} className="glass-card p-4 animate-slide-in">
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs text-nexus-text-muted uppercase tracking-wider">
                  {card.label}
                </span>
                <div
                  className="w-8 h-8 rounded-lg flex items-center justify-center"
                  style={{ backgroundColor: `${card.color}15` }}
                >
                  <Icon className="w-4 h-4" style={{ color: card.color }} />
                </div>
              </div>
              <p
                className="text-lg font-bold font-[family-name:var(--font-mono)] tracking-tight"
                style={{ color: card.color }}
              >
                {card.value}
              </p>
            </div>
          );
        })}
      </div>

      {/* Controls Row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Left: Venue & Account */}
        <div>
          <h2 className="text-sm font-semibold text-nexus-text-dim uppercase tracking-wider mb-3">
            Execution Controls
          </h2>
          <VenueSwitch />
        </div>

        {/* Right: Panic */}
        <div>
          <h2 className="text-sm font-semibold text-nexus-text-dim uppercase tracking-wider mb-3">
            Emergency Controls
          </h2>
          <PanicButton />
        </div>
      </div>

      {/* System Status */}
      <div className="glass-card p-4">
        <h2 className="text-sm font-semibold text-nexus-text-dim uppercase tracking-wider mb-3">
          System Status
        </h2>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          {[
            { label: 'Pipeline', status: !panicMode, detail: panicMode ? 'Halted' : 'Running' },
            { label: 'Venue', status: true, detail: executionVenue === 'IQ_OPTION' ? 'IQ Option' : 'Binance' },
            { label: 'Account', status: accountType !== 'REAL', detail: accountType },
            { label: 'ML Engine', status: true, detail: 'LSTM Active' },
          ].map((s) => (
            <div key={s.label} className="flex items-center gap-2 py-2">
              <div className={`w-2 h-2 rounded-full ${s.status ? 'bg-nexus-green' : 'bg-nexus-red animate-regime-pulse'}`} />
              <div>
                <span className="text-xs text-nexus-text-muted block">{s.label}</span>
                <span className="text-xs font-medium text-nexus-text">{s.detail}</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
