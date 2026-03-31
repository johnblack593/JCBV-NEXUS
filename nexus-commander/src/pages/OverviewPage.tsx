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
    <div className="h-full overflow-y-auto w-full">
      <div className="p-6 md:p-8 lg:p-10 space-y-8 animate-fade-in">
        {/* Page title */}
        <div className="border-b border-nexus-border pb-4">
          <h1 className="text-2xl font-bold text-nexus-text tracking-tight">Command Center</h1>
          <p className="text-nexus-text-muted mt-1 text-sm md:text-base">
            Real-time pipeline overview and global controls
          </p>
        </div>

        {/* Stat Cards Grid */}
        <div className="grid grid-cols-2 md:grid-cols-3 gap-6">
          {statCards.map((card) => {
            const Icon = card.icon;
            return (
              <div key={card.label} className="glass-card p-5 animate-slide-in flex flex-col justify-between h-28">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-[10px] sm:text-xs font-semibold text-nexus-text-muted uppercase tracking-wider truncate">
                    {card.label}
                  </span>
                  <div
                    className="w-7 h-7 flex-shrink-0 rounded-lg flex items-center justify-center p-1.5"
                    style={{ backgroundColor: `${card.color}15` }}
                  >
                    <Icon className="w-full h-full" style={{ color: card.color }} />
                  </div>
                </div>
                <div className="mt-auto">
                  <p
                    className="text-lg md:text-xl font-bold font-[family-name:var(--font-mono)] tracking-tight truncate"
                    style={{ color: card.color }}
                  >
                    {card.value}
                  </p>
                </div>
              </div>
            );
          })}
        </div>

        {/* Controls Row */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Left: Venue & Account */}
          <div className="glass-card p-6">
            <div className="flex items-center justify-between mb-5 border-b border-nexus-border/50 pb-3">
              <h2 className="text-sm font-bold text-nexus-text-dim uppercase tracking-wider flex items-center gap-2">
                <Activity className="w-4 h-4 text-nexus-cyan" />
                Execution Provider
              </h2>
            </div>
            <VenueSwitch />
          </div>

          {/* Right: Panic */}
          <div className="glass-card p-6 border-nexus-red/30">
            <div className="flex items-center justify-between mb-5 border-b border-nexus-border/50 pb-3">
              <h2 className="text-sm font-bold text-nexus-text-dim uppercase tracking-wider flex items-center gap-2">
                <Shield className="w-4 h-4 text-nexus-red" />
                Emergency Operations
              </h2>
            </div>
            <PanicButton />
          </div>
        </div>

        {/* System Status Table Row */}
        <div className="glass-card p-6">
          <h2 className="text-sm font-bold text-nexus-text-dim uppercase tracking-wider mb-5 border-b border-nexus-border/50 pb-3">
            Core Daemon Subsystems
          </h2>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-6">
            {[
              { label: 'Pipeline Engine', status: !panicMode, detail: panicMode ? 'HALTED' : 'RUNNING' },
              { label: 'Market API', status: true, detail: executionVenue === 'IQ_OPTION' ? 'IQ Option WS' : 'Binance FIX' },
              { label: 'Order Execution', status: accountType !== 'REAL', detail: accountType },
              { label: 'Inference Graph', status: aiMode, detail: aiMode ? 'LSTM Online' : 'Static Logic' },
            ].map((s) => (
              <div key={s.label} className="flex items-start gap-3 p-3 rounded-lg bg-nexus-surface-2/30 border border-nexus-border/30 hover:border-nexus-border transition-colors">
                <div className={`mt-1.5 w-2 h-2 rounded-full flex-shrink-0 ${s.status ? 'bg-nexus-green animate-pulse-glow' : 'bg-nexus-red animate-regime-pulse'}`} />
                <div>
                  <span className="text-[11px] uppercase tracking-wider font-semibold text-nexus-text-muted block">{s.label}</span>
                  <span className={`text-sm font-bold mt-0.5 block ${s.status ? 'text-nexus-text' : 'text-nexus-red'}`}>
                    {s.detail}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
