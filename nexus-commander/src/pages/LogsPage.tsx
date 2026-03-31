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

  const handleDownload = () => {
    if (entries.length === 0) return;
    const content = entries.map(e => e.line).join('\n');
    const blob = new Blob([content], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `nexus_tearsheet_${new Date().toISOString().replace(/[:.]/g, '-')}.txt`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  return (
    <div className="p-6 h-full flex flex-col gap-4 animate-fade-in">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-nexus-text">Logs & Tear Sheet</h1>
          <p className="text-sm text-nexus-text-muted mt-1">
            Live pipeline log stream • Institutional report generation
          </p>
        </div>

        <div className="flex items-center gap-2">
          {/* Connection indicator */}
          <div className="flex items-center gap-1.5 text-xs mr-3 font-semibold tracking-wider uppercase">
            {isConnected ? (
              <Wifi className="w-4 h-4 text-nexus-green animate-regime-pulse" />
            ) : (
              <WifiOff className="w-4 h-4 text-nexus-red" />
            )}
            <span className={isConnected ? 'text-nexus-green' : 'text-nexus-red'}>
              {wsStatus}
            </span>
          </div>

          <button
            onClick={clear}
            className="flex items-center gap-2 px-4 py-2 rounded-lg border border-nexus-border
                       text-sm text-nexus-text-muted hover:text-nexus-text hover:bg-nexus-surface-2 
                       transition-colors font-medium cursor-pointer"
          >
            <Trash2 className="w-4 h-4" />
            Clear Buffer
          </button>

          <button
            onClick={handleDownload}
            disabled={entries.length === 0}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-bold
                       transition-all shadow-lg
                       ${entries.length > 0 
                         ? 'bg-nexus-cyan text-[#000000] hover:brightness-110 cursor-pointer' 
                         : 'bg-nexus-surface-2 text-nexus-text-muted select-none cursor-not-allowed border border-nexus-border'}`}
          >
            <Download className="w-4 h-4" />
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
