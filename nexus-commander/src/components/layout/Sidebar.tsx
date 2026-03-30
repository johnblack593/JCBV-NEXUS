// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — Sidebar Navigation
// ═══════════════════════════════════════════════════════════════

import {
  LayoutDashboard,
  Activity,
  SlidersHorizontal,
  FlaskConical,
  ScrollText,
  LogOut,
  Zap,
} from 'lucide-react';
import { useAuthStore } from '../../store/auth-store';
import { useTelemetryStore } from '../../store/telemetry-store';
import { REGIME_COLORS } from '../../lib/constants';

const NAV_ITEMS = [
  { id: 'overview',    label: 'Overview',     icon: LayoutDashboard },
  { id: 'operations',  label: 'Operations',   icon: Activity },
  { id: 'risk',        label: 'Risk Tuning',  icon: SlidersHorizontal },
  { id: 'simlab',      label: 'Sim Lab',      icon: FlaskConical },
  { id: 'logs',        label: 'Logs & Tear',  icon: ScrollText },
] as const;

export type TabId = (typeof NAV_ITEMS)[number]['id'];

interface SidebarProps {
  activeTab: TabId;
  onTabChange: (tab: TabId) => void;
}

export default function Sidebar({ activeTab, onTabChange }: SidebarProps) {
  const logout = useAuthStore((s) => s.logout);
  const macroRegime = useTelemetryStore((s) => s.macroRegime);
  const panicMode = useTelemetryStore((s) => s.panicMode);

  const regimeColor = panicMode ? '#EF4444' : (REGIME_COLORS[macroRegime] ?? '#10B981');

  return (
    <aside className="w-[220px] min-w-[220px] bg-nexus-surface border-r border-nexus-border flex flex-col h-full">
      {/* Brand */}
      <div className="px-5 py-5 border-b border-nexus-border">
        <div className="flex items-center gap-2.5">
          <div
            className="w-9 h-9 rounded-lg flex items-center justify-center transition-colors duration-300"
            style={{ backgroundColor: `${regimeColor}20`, border: `1px solid ${regimeColor}40` }}
          >
            <Zap className="w-5 h-5" style={{ color: regimeColor }} />
          </div>
          <div>
            <h2 className="text-sm font-bold text-nexus-text tracking-tight leading-none">
              NEXUS
            </h2>
            <span className="text-[10px] text-nexus-text-muted uppercase tracking-widest">
              Commander v4.0
            </span>
          </div>
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 py-3 px-3 space-y-0.5">
        {NAV_ITEMS.map((item) => {
          const isActive = activeTab === item.id;
          const Icon = item.icon;
          return (
            <button
              key={item.id}
              id={`nav-${item.id}`}
              onClick={() => onTabChange(item.id)}
              className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium
                         transition-all duration-200 group
                         ${isActive
                           ? 'bg-nexus-cyan/10 text-nexus-cyan border border-nexus-cyan/20'
                           : 'text-nexus-text-dim hover:text-nexus-text hover:bg-nexus-surface-2 border border-transparent'
                         }`}
            >
              <Icon className={`w-4 h-4 transition-colors ${isActive ? 'text-nexus-cyan' : 'text-nexus-text-muted group-hover:text-nexus-text-dim'}`} />
              {item.label}
            </button>
          );
        })}
      </nav>

      {/* Regime Indicator */}
      <div className="px-4 py-3 border-t border-nexus-border">
        <div className="flex items-center gap-2 mb-3">
          <div
            className="w-2.5 h-2.5 rounded-full animate-regime-pulse"
            style={{ backgroundColor: regimeColor }}
          />
          <span className="text-xs font-medium text-nexus-text-dim uppercase tracking-wider">
            {panicMode ? 'PANIC HALT' : `Regime: ${macroRegime}`}
          </span>
        </div>

        <button
          onClick={logout}
          className="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sm text-nexus-text-muted
                     hover:text-nexus-red hover:bg-nexus-red/5 transition-all duration-200 border border-transparent
                     hover:border-nexus-red/20"
        >
          <LogOut className="w-4 h-4" />
          Disconnect
        </button>
      </div>
    </aside>
  );
}
