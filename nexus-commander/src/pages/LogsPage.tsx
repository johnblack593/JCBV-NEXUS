// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — Logs & Tear Sheet Page (Tab 5)
//  Live log stream + report generation
// ═══════════════════════════════════════════════════════════════

import { useEffect, useRef } from 'react';
import { useLogsStore } from '../store/telemetry-store';
import { ScrollText, Trash2, Download, Wifi, WifiOff } from 'lucide-react';

export default function LogsPage() {
  const { entries, wsStatus, connect, disconnect, clear } = useLogsStore();
  const scrollRef = useRef<HTMLDivElement>(null);
  const isConnected = wsStatus === 'CONNECTED';

  useEffect(() => {
    connect();
    return () => disconnect();
  }, [connect, disconnect]);

  // Auto-scroll to bottom
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [entries]);

  const getLineColor = (line: string): string => {
    if (line.includes('ERROR') || line.includes('CRITICAL') || line.includes('💀') || line.includes('🚨')) {
      return 'text-nexus-red';
    }
    if (line.includes('WARNING') || line.includes('⚠️')) {
      return 'text-nexus-amber';
    }
    if (line.includes('INFO') && (line.includes('✅') || line.includes('🚀') || line.includes('LIVE'))) {
      return 'text-nexus-green';
    }
    return 'text-nexus-text-dim';
  };

  return (
    <div className="p-6 h-full flex flex-col gap-4 animate-fade-in">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-nexus-text">Logs & Tear Sheet</h1>
          <p className="text-sm text-nexus-text-muted mt-0.5">
            Live pipeline log stream • Institutional report generation
          </p>
        </div>

        <div className="flex items-center gap-2">
          {/* Connection indicator */}
          <div className="flex items-center gap-1.5 text-xs mr-3">
            {isConnected ? (
              <Wifi className="w-3.5 h-3.5 text-nexus-green" />
            ) : (
              <WifiOff className="w-3.5 h-3.5 text-nexus-red" />
            )}
            <span className={isConnected ? 'text-nexus-green' : 'text-nexus-red'}>
              {wsStatus}
            </span>
          </div>

          <button
            onClick={clear}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-nexus-border
                       text-xs text-nexus-text-muted hover:text-nexus-text hover:border-nexus-border-light
                       transition-all duration-200"
          >
            <Trash2 className="w-3.5 h-3.5" />
            Clear
          </button>

          <button
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg
                       bg-nexus-cyan/10 border border-nexus-cyan/30 text-nexus-cyan
                       text-xs font-medium hover:bg-nexus-cyan/20 transition-all duration-200"
          >
            <Download className="w-3.5 h-3.5" />
            Generate Tear Sheet
          </button>
        </div>
      </div>

      {/* Log Console */}
      <div className="flex-1 glass-card flex flex-col overflow-hidden">
        <div className="flex items-center gap-2 px-4 py-2.5 border-b border-nexus-border bg-nexus-bg/50">
          <ScrollText className="w-4 h-4 text-nexus-text-muted" />
          <span className="text-xs font-medium text-nexus-text-dim uppercase tracking-wider">
            Pipeline Console
          </span>
          <span className="text-xs text-nexus-text-muted ml-auto">
            {entries.length} lines
          </span>
        </div>

        <div
          ref={scrollRef}
          className="flex-1 overflow-y-auto p-4 font-[family-name:var(--font-mono)] text-xs leading-relaxed"
        >
          {entries.length === 0 ? (
            <div className="flex items-center justify-center h-full text-nexus-text-muted">
              {isConnected ? 'Waiting for log output...' : 'Connect to backend to stream logs'}
            </div>
          ) : (
            entries.map((entry, i) => (
              <div
                key={i}
                className={`py-0.5 hover:bg-nexus-surface-2/30 px-2 -mx-2 rounded transition-colors ${getLineColor(entry.line)}`}
              >
                {entry.line}
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
