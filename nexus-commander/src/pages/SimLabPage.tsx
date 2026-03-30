// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — Sim Lab Page (Tab 4) — PLACEHOLDER
//  Spectacular UI placeholder for Backtesting & WFO
// ═══════════════════════════════════════════════════════════════

import { FlaskConical, Microscope, BarChart3, TestTubes, Rocket, Lock } from 'lucide-react';

const FEATURES = [
  {
    icon: Microscope,
    title: 'Walk-Forward Optimization',
    description: 'Rolling window validation with non-overlapping out-of-sample periods',
    color: '#06B6D4',
  },
  {
    icon: BarChart3,
    title: 'Multi-Asset Backtesting',
    description: 'Parallel backtest across all supported instruments with tick-level precision',
    color: '#8B5CF6',
  },
  {
    icon: TestTubes,
    title: 'Parameter Sensitivity Analysis',
    description: 'Heatmap visualization of parameter combinations and their Sharpe ratios',
    color: '#10B981',
  },
  {
    icon: Rocket,
    title: 'Monte Carlo Simulation',
    description: '10,000 permutation equity curves for robust drawdown estimation',
    color: '#F59E0B',
  },
];

export default function SimLabPage() {
  return (
    <div className="p-6 h-full overflow-y-auto animate-fade-in">
      <div className="max-w-3xl mx-auto">
        {/* Hero Section */}
        <div className="text-center mb-10">
          <div className="inline-flex items-center justify-center w-20 h-20 rounded-2xl
                          bg-gradient-to-br from-nexus-cyan/20 to-nexus-purple/20
                          border border-nexus-cyan/20 mb-5 animate-pulse-glow">
            <FlaskConical className="w-10 h-10 text-nexus-cyan" />
          </div>
          <h1 className="text-2xl font-bold text-nexus-text mb-2">The Simulation Laboratory</h1>
          <p className="text-nexus-text-muted text-sm max-w-md mx-auto">
            Institutional-grade backtesting, walk-forward optimization, and Monte Carlo analysis
          </p>

          {/* Coming Soon Badge */}
          <div className="inline-flex items-center gap-2 mt-5 px-4 py-2 rounded-full
                          bg-nexus-purple/10 border border-nexus-purple/30 text-nexus-purple text-sm font-medium">
            <Lock className="w-4 h-4" />
            Phase 9 — Coming Soon
          </div>
        </div>

        {/* Feature Cards */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-8">
          {FEATURES.map((feature) => {
            const Icon = feature.icon;
            return (
              <div
                key={feature.title}
                className="glass-card p-5 transition-all duration-300 hover:scale-[1.02]
                           hover:shadow-lg group cursor-default"
              >
                <div
                  className="w-10 h-10 rounded-lg flex items-center justify-center mb-3 transition-colors"
                  style={{ backgroundColor: `${feature.color}15` }}
                >
                  <Icon className="w-5 h-5" style={{ color: feature.color }} />
                </div>
                <h3 className="text-sm font-semibold text-nexus-text mb-1">{feature.title}</h3>
                <p className="text-xs text-nexus-text-muted leading-relaxed">
                  {feature.description}
                </p>
              </div>
            );
          })}
        </div>

        {/* Mock Equity Curve Preview */}
        <div className="glass-card p-6">
          <h3 className="text-sm font-semibold text-nexus-text mb-4">Preview: Equity Curve Projection</h3>
          <div className="h-48 flex items-end justify-between gap-1 px-4">
            {Array.from({ length: 40 }, (_, i) => {
              const height = 20 + Math.sin(i * 0.3) * 15 + i * 1.5 + Math.random() * 10;
              const isPositive = height > (20 + (i > 0 ? (i - 1) * 1.5 : 0));
              return (
                <div
                  key={i}
                  className="flex-1 rounded-t-sm transition-all duration-300 opacity-40"
                  style={{
                    height: `${Math.min(height, 100)}%`,
                    backgroundColor: isPositive ? '#10B981' : '#EF4444',
                  }}
                />
              );
            })}
          </div>
          <div className="mt-3 flex items-center justify-between text-xs text-nexus-text-muted border-t border-nexus-border pt-3">
            <span>Jan 2025</span>
            <span className="text-nexus-green font-bold">+47.3% (simulated)</span>
            <span>Mar 2026</span>
          </div>
        </div>
      </div>
    </div>
  );
}
