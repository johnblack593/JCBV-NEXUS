// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — App Shell
//  Layout · Tab Router · WS Lifecycle · Auth Gate
// ═══════════════════════════════════════════════════════════════

import { useEffect, useState } from 'react';
import { useAuthStore } from './store/auth-store';
import { useTelemetryStore } from './store/telemetry-store';
import LoginForm from './components/auth/LoginForm';
import Sidebar, { type TabId } from './components/layout/Sidebar';
import Header from './components/layout/Header';
import OverviewPage from './pages/OverviewPage';
import OperationsPage from './pages/OperationsPage';
import RiskTuningPage from './pages/RiskTuningPage';
import SimLabPage from './pages/SimLabPage';
import LogsPage from './pages/LogsPage';

const PAGE_MAP: Record<TabId, React.FC> = {
  overview:   OverviewPage,
  operations: OperationsPage,
  risk:       RiskTuningPage,
  simlab:     SimLabPage,
  logs:       LogsPage,
};

export default function App() {
  const { isAuthenticated, hydrate } = useAuthStore();
  const { connect: connectTelemetry, disconnect: disconnectTelemetry } = useTelemetryStore();
  const [activeTab, setActiveTab] = useState<TabId>('overview');

  // Hydrate auth from localStorage on mount
  useEffect(() => {
    hydrate();
  }, [hydrate]);

  // Connect telemetry WS when authenticated
  useEffect(() => {
    if (isAuthenticated) {
      connectTelemetry();
      return () => disconnectTelemetry();
    }
  }, [isAuthenticated, connectTelemetry, disconnectTelemetry]);

  // Auth gate
  if (!isAuthenticated) {
    return <LoginForm />;
  }

  const ActivePage = PAGE_MAP[activeTab];

  return (
    <div className="flex h-screen w-screen bg-nexus-bg overflow-hidden">
      {/* Sidebar Navigation */}
      <Sidebar activeTab={activeTab} onTabChange={setActiveTab} />

      {/* Main Content Area */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Header Bar */}
        <Header />

        {/* Page Content */}
        <main className="flex-1 min-h-0 overflow-hidden">
          <ActivePage />
        </main>

        {/* Status Bar */}
        <StatusBar />
      </div>
    </div>
  );
}

// ── Status Bar ──────────────────────────────────────────────────

function StatusBar() {
  const { wsStatus, latencyMs, lastTelemetryTimestamp } = useTelemetryStore();
  const [clock, setClock] = useState(new Date());

  useEffect(() => {
    const timer = setInterval(() => setClock(new Date()), 1000);
    return () => clearInterval(timer);
  }, []);

  return (
    <footer className="h-7 bg-nexus-surface/80 border-t border-nexus-border flex items-center justify-between px-4 text-[11px]">
      <div className="flex items-center gap-4">
        <span className="text-nexus-text-muted">
          NEXUS v4.0 OCP
        </span>
        <span className={`flex items-center gap-1 ${
          wsStatus === 'CONNECTED' ? 'text-nexus-green' : 'text-nexus-red'
        }`}>
          <span className={`w-1.5 h-1.5 rounded-full ${
            wsStatus === 'CONNECTED' ? 'bg-nexus-green' : 'bg-nexus-red animate-regime-pulse'
          }`} />
          WS: {wsStatus}
        </span>
        {latencyMs > 0 && (
          <span className={
            latencyMs < 100 ? 'text-nexus-green' : latencyMs < 500 ? 'text-nexus-amber' : 'text-nexus-red'
          }>
            Latency: {latencyMs}ms
          </span>
        )}
      </div>

      <div className="flex items-center gap-4 text-nexus-text-muted">
        {lastTelemetryTimestamp && (
          <span>
            Last frame: {new Date(lastTelemetryTimestamp).toLocaleTimeString()}
          </span>
        )}
        <span className="font-[family-name:var(--font-mono)]">
          {clock.toLocaleTimeString()} UTC{clock.getTimezoneOffset() > 0 ? '-' : '+'}{Math.abs(clock.getTimezoneOffset() / 60)}
        </span>
      </div>
    </footer>
  );
}
