// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — Header Bar
// ═══════════════════════════════════════════════════════════════

import { useTelemetryStore } from '../../store/telemetry-store';
import { useAuthStore } from '../../store/auth-store';
import { REGIME_COLORS, REGIME_LABELS } from '../../lib/constants';
import { Wifi, WifiOff, Clock, User, AlertTriangle } from 'lucide-react';

export default function Header() {
  const { macroRegime, panicMode, wsStatus, latencyMs, aiMode, executionVenue, accountType } = useTelemetryStore();
  const user = useAuthStore((s) => s.user);

  const regimeColor = panicMode ? '#EF4444' : (REGIME_COLORS[macroRegime] ?? '#10B981');
  const regimeLabel = panicMode ? 'PANIC HALT' : (REGIME_LABELS[macroRegime] ?? macroRegime);
  const isConnected = wsStatus === 'CONNECTED';

  return (
    <header className={`h-12 border-b flex items-center justify-between px-5 transition-colors duration-300
                        ${panicMode ? 'border-nexus-red/50 bg-nexus-red/5' : 'border-nexus-border bg-nexus-surface/50'}`}>
      {/* Left: Regime badge */}
      <div className="flex items-center gap-4">
        <div
          className="flex items-center gap-2 px-3 py-1 rounded-full border text-xs font-bold uppercase tracking-wider"
          style={{
            color: regimeColor,
            backgroundColor: `${regimeColor}15`,
            borderColor: `${regimeColor}40`,
          }}
        >
          {panicMode && <AlertTriangle className="w-3.5 h-3.5 animate-panic-flash" style={{ color: regimeColor }} />}
          <span className={panicMode ? 'animate-regime-pulse' : ''}>{regimeLabel}</span>
        </div>

        {aiMode && (
          <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-nexus-purple/10 border border-nexus-purple/30 text-nexus-purple text-xs font-medium">
            <span className="w-1.5 h-1.5 rounded-full bg-nexus-purple animate-regime-pulse" />
            AI MODE
          </div>
        )}
      </div>

      {/* Right: Status indicators */}
      <div className="flex items-center gap-5 text-xs">
        {/* Venue + Account */}
        <div className="flex items-center gap-2 text-nexus-text-dim">
          <span className="px-2 py-0.5 rounded bg-nexus-surface-2 border border-nexus-border text-nexus-text-dim font-medium">
            {executionVenue === 'IQ_OPTION' ? 'IQ Option' : 'Binance'}
          </span>
          <span className={`px-2 py-0.5 rounded border font-medium ${
            accountType === 'REAL'
              ? 'bg-nexus-red/10 border-nexus-red/30 text-nexus-red'
              : 'bg-nexus-green/10 border-nexus-green/30 text-nexus-green'
          }`}>
            {accountType === 'REAL' ? '💰 REAL' : '🧪 DEMO'}
          </span>
        </div>

        {/* Latency */}
        <div className="flex items-center gap-1.5 text-nexus-text-muted">
          <Clock className="w-3.5 h-3.5" />
          <span className={latencyMs < 100 ? 'text-nexus-green' : latencyMs < 500 ? 'text-nexus-amber' : 'text-nexus-red'}>
            {latencyMs}ms
          </span>
        </div>

        {/* WS Status */}
        <div className="flex items-center gap-1.5">
          {isConnected ? (
            <Wifi className="w-3.5 h-3.5 text-nexus-green" />
          ) : (
            <WifiOff className="w-3.5 h-3.5 text-nexus-red animate-regime-pulse" />
          )}
          <span className={isConnected ? 'text-nexus-green' : 'text-nexus-red'}>
            {wsStatus}
          </span>
        </div>

        {/* User */}
        <div className="flex items-center gap-1.5 text-nexus-text-dim">
          <User className="w-3.5 h-3.5" />
          {user}
        </div>
      </div>
    </header>
  );
}
