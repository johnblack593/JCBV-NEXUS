// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — Risk Tuning Page (Tab 3)
// ═══════════════════════════════════════════════════════════════

import RiskSliders from '../components/controls/RiskSliders';
import PanicButton from '../components/controls/PanicButton';
import { useTelemetryStore } from '../store/telemetry-store';
import { REGIME_COLORS } from '../lib/constants';

export default function RiskTuningPage() {
  const { macroRegime, panicMode } = useTelemetryStore();
  const regimeColor = panicMode ? '#EF4444' : (REGIME_COLORS[macroRegime] ?? '#10B981');

  return (
    <div className="p-6 h-full overflow-y-auto animate-fade-in">
      <div className="max-w-2xl mx-auto space-y-6">
        <div>
          <h1 className="text-xl font-bold text-nexus-text">Risk & Parameter Tuning</h1>
          <p className="text-sm text-nexus-text-muted mt-0.5">
            Regime-shift dynamic tuning — changes hot-reload into the pipeline via Redis
          </p>
        </div>

        {/* Regime Visual Indicator */}
        <div
          className="glass-card p-5 border transition-all duration-500"
          style={{ borderColor: `${regimeColor}40` }}
        >
          <div className="flex items-center gap-4">
            <div
              className="w-16 h-16 rounded-xl flex items-center justify-center text-2xl font-black animate-regime-pulse"
              style={{ backgroundColor: `${regimeColor}15`, color: regimeColor }}
            >
              {panicMode ? '🛑' : macroRegime === 'RED' ? '🔴' : macroRegime === 'YELLOW' ? '🟡' : '🟢'}
            </div>
            <div>
              <h2 className="text-lg font-bold" style={{ color: regimeColor }}>
                {panicMode ? 'PANIC HALT ACTIVE' : `${macroRegime} REGIME`}
              </h2>
              <p className="text-sm text-nexus-text-muted">
                {panicMode
                  ? 'All parameters locked. Reset panic to resume.'
                  : macroRegime === 'RED'
                    ? 'Defensive mode — conservative parameters enforced'
                    : macroRegime === 'YELLOW'
                      ? 'Caution mode — moderate risk parameters'
                      : 'Normal operations — standard risk parameters'}
              </p>
            </div>
          </div>
        </div>

        {/* Risk Sliders */}
        <RiskSliders />

        {/* Emergency Controls */}
        <div>
          <h2 className="text-sm font-semibold text-nexus-text-dim uppercase tracking-wider mb-3">
            Emergency Controls
          </h2>
          <PanicButton />
        </div>
      </div>
    </div>
  );
}
