// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — PANIC Button (The Nuclear Button)
//  Optimistic UI · <1ms visual response · Double-confirm
// ═══════════════════════════════════════════════════════════════

import { useState, useCallback } from 'react';
import { useTelemetryStore } from '../../store/telemetry-store';
import { triggerPanic, resetPanic } from '../../lib/api';
import { AlertTriangle, ShieldOff, RotateCcw } from 'lucide-react';
import toast from 'react-hot-toast';

export default function PanicButton() {
  const panicMode = useTelemetryStore((s) => s.panicMode);
  const [isArmed, setIsArmed] = useState(false);
  const [isProcessing, setIsProcessing] = useState(false);

  const handleArm = useCallback(() => {
    if (panicMode) return;
    setIsArmed(true);
    // Auto-disarm after 5 seconds
    setTimeout(() => setIsArmed(false), 5000);
  }, [panicMode]);

  const handlePanic = useCallback(async () => {
    if (isProcessing) return;
    setIsProcessing(true);

    try {
      await triggerPanic();
      toast.error('🚨 PANIC HALT ACTIVATED — All trading stopped', { duration: 8000 });
    } catch (err) {
      toast.error(`Panic failed: ${err instanceof Error ? err.message : 'Unknown error'}`);
    } finally {
      setIsArmed(false);
      setIsProcessing(false);
    }
  }, [isProcessing]);

  const handleReset = useCallback(async () => {
    if (isProcessing) return;
    setIsProcessing(true);

    try {
      await resetPanic();
      toast.success('✅ Panic cleared — Trading resumed');
    } catch (err) {
      toast.error(`Reset failed: ${err instanceof Error ? err.message : 'Unknown error'}`);
    } finally {
      setIsProcessing(false);
    }
  }, [isProcessing]);

  // ── PANIC ACTIVE STATE ──
  if (panicMode) {
    return (
      <div className="animate-slide-in">
        <div className="glass-card p-5 border-nexus-red/50 bg-nexus-red/5 animate-panic-flash-slow relative overflow-hidden">
          {/* Alert overlay */}
          <div className="absolute inset-0 bg-gradient-to-r from-nexus-red/10 via-transparent to-nexus-red/10 animate-pulse" />

          <div className="relative z-10">
            <div className="flex items-center gap-3 mb-3">
              <div className="w-10 h-10 rounded-full bg-nexus-red/20 border border-nexus-red/40 flex items-center justify-center">
                <ShieldOff className="w-5 h-5 text-nexus-red animate-regime-pulse" />
              </div>
              <div>
                <h3 className="text-nexus-red font-bold text-sm uppercase tracking-wider">
                  ⚠️ SYSTEM HALTED
                </h3>
                <p className="text-nexus-red/70 text-xs">
                  All trading operations suspended
                </p>
              </div>
            </div>

            <button
              id="btn-panic-reset"
              onClick={handleReset}
              disabled={isProcessing}
              className="w-full flex items-center justify-center gap-2 py-2.5 rounded-lg
                         bg-nexus-surface border border-nexus-border text-nexus-text text-sm font-medium
                         hover:border-nexus-green/40 hover:text-nexus-green transition-all duration-200
                         disabled:opacity-50"
            >
              {isProcessing ? (
                <div className="w-4 h-4 border-2 border-nexus-text/30 border-t-nexus-text rounded-full animate-spin" />
              ) : (
                <>
                  <RotateCcw className="w-4 h-4" />
                  RESET & RESUME
                </>
              )}
            </button>
          </div>
        </div>
      </div>
    );
  }

  // ── ARMED STATE (Confirmation) ──
  if (isArmed) {
    return (
      <div className="animate-slide-in">
        <div className="glass-card p-5 border-nexus-amber/50 bg-nexus-amber/5">
          <div className="flex items-center gap-2 mb-3">
            <AlertTriangle className="w-5 h-5 text-nexus-amber animate-regime-pulse" />
            <span className="text-nexus-amber font-bold text-sm uppercase tracking-wider">
              CONFIRM EMERGENCY STOP
            </span>
          </div>
          <p className="text-nexus-text-dim text-xs mb-4">
            This will immediately halt ALL trading operations across all venues.
          </p>
          <div className="flex gap-2">
            <button
              id="btn-panic-confirm"
              onClick={handlePanic}
              disabled={isProcessing}
              className="flex-1 py-2.5 rounded-lg bg-nexus-red text-white text-sm font-bold uppercase tracking-wider
                         hover:bg-red-600 transition-all duration-200 disabled:opacity-50
                         shadow-lg shadow-nexus-red/20"
            >
              {isProcessing ? (
                <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin mx-auto" />
              ) : (
                '🔴 CONFIRM HALT'
              )}
            </button>
            <button
              id="btn-panic-cancel"
              onClick={() => setIsArmed(false)}
              className="px-4 py-2.5 rounded-lg border border-nexus-border text-nexus-text-dim text-sm
                         hover:text-nexus-text hover:border-nexus-border-light transition-all duration-200"
            >
              Cancel
            </button>
          </div>
        </div>
      </div>
    );
  }

  // ── NORMAL STATE ──
  return (
    <button
      id="btn-panic-arm"
      onClick={handleArm}
      className="w-full glass-card p-4 border-nexus-red/20 hover:border-nexus-red/50
                 transition-all duration-300 group cursor-pointer
                 hover:shadow-lg hover:shadow-nexus-red/10"
    >
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-full border-2 border-nexus-red/40 flex items-center justify-center
                        group-hover:border-nexus-red group-hover:bg-nexus-red/10 transition-all duration-300">
          <AlertTriangle className="w-5 h-5 text-nexus-red/60 group-hover:text-nexus-red transition-colors" />
        </div>
        <div className="text-left">
          <h3 className="text-sm font-bold text-nexus-red/80 group-hover:text-nexus-red uppercase tracking-wider transition-colors">
            ⚡ PANIC HALT
          </h3>
          <p className="text-xs text-nexus-text-muted">
            Emergency stop all operations
          </p>
        </div>
      </div>
    </button>
  );
}
