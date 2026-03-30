// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — Venue & Account Switch Controls
// ═══════════════════════════════════════════════════════════════

import { useState } from 'react';
import { useTelemetryStore } from '../../store/telemetry-store';
import { switchVenue, switchAccountType } from '../../lib/api';
import { ArrowLeftRight, ShieldAlert } from 'lucide-react';
import toast from 'react-hot-toast';

export default function VenueSwitch() {
  const { executionVenue, accountType } = useTelemetryStore();
  const [isVenueLoading, setIsVenueLoading] = useState(false);
  const [isAccountLoading, setIsAccountLoading] = useState(false);
  const [showRealConfirm, setShowRealConfirm] = useState(false);

  const handleVenueToggle = async () => {
    const newVenue = executionVenue === 'IQ_OPTION' ? 'BINANCE' : 'IQ_OPTION';
    setIsVenueLoading(true);
    try {
      await switchVenue({ venue: newVenue });
      toast.success(`Venue switched to ${newVenue === 'IQ_OPTION' ? 'IQ Option' : 'Binance'}`);
    } catch (err) {
      toast.error(`Switch failed: ${err instanceof Error ? err.message : 'Error'}`);
    } finally {
      setIsVenueLoading(false);
    }
  };

  const handleAccountToggle = async () => {
    if (accountType === 'PRACTICE') {
      // Switching to REAL requires confirmation
      setShowRealConfirm(true);
      return;
    }
    // Switch back to PRACTICE (safe)
    setIsAccountLoading(true);
    try {
      await switchAccountType({ account_type: 'PRACTICE', confirm: false });
      toast.success('Switched to DEMO mode');
    } catch (err) {
      toast.error(`Switch failed: ${err instanceof Error ? err.message : 'Error'}`);
    } finally {
      setIsAccountLoading(false);
    }
  };

  const confirmRealSwitch = async () => {
    setIsAccountLoading(true);
    setShowRealConfirm(false);
    try {
      await switchAccountType({ account_type: 'REAL', confirm: true });
      toast.error('⚠️ REAL MONEY MODE ACTIVATED', { duration: 6000 });
    } catch (err) {
      toast.error(`Switch failed: ${err instanceof Error ? err.message : 'Error'}`);
    } finally {
      setIsAccountLoading(false);
    }
  };

  return (
    <div className="space-y-3">
      {/* Venue Switch */}
      <div className="glass-card p-4">
        <label className="text-xs text-nexus-text-muted uppercase tracking-wider font-medium mb-2 block">
          Execution Venue
        </label>
        <div className="flex items-center gap-2">
          <button
            onClick={handleVenueToggle}
            disabled={isVenueLoading}
            className="flex-1 flex items-center justify-between px-4 py-2.5 rounded-lg
                       bg-nexus-bg border border-nexus-border hover:border-nexus-cyan/40
                       transition-all duration-200 disabled:opacity-50"
          >
            <span className="text-sm font-medium text-nexus-text">
              {executionVenue === 'IQ_OPTION' ? '📊 IQ Option (Binary)' : '₿ Binance (Crypto)'}
            </span>
            <ArrowLeftRight className="w-4 h-4 text-nexus-text-muted" />
          </button>
        </div>
      </div>

      {/* Account Type Switch */}
      <div className={`glass-card p-4 transition-all duration-300 ${
        accountType === 'REAL' ? 'border-nexus-red/40 bg-nexus-red/5' : ''
      }`}>
        <label className="text-xs text-nexus-text-muted uppercase tracking-wider font-medium mb-2 block">
          Account Mode
        </label>

        {showRealConfirm ? (
          <div className="animate-slide-in">
            <div className="flex items-center gap-2 mb-3 text-nexus-amber">
              <ShieldAlert className="w-4 h-4" />
              <span className="text-xs font-bold uppercase">
                Confirm: Switch to REAL MONEY?
              </span>
            </div>
            <div className="flex gap-2">
              <button
                onClick={confirmRealSwitch}
                className="flex-1 py-2 rounded-lg bg-nexus-red text-white text-xs font-bold uppercase
                           hover:bg-red-600 transition-colors"
              >
                Yes, Use Real Money
              </button>
              <button
                onClick={() => setShowRealConfirm(false)}
                className="px-3 py-2 rounded-lg border border-nexus-border text-nexus-text-dim text-xs
                           hover:text-nexus-text transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <button
            onClick={handleAccountToggle}
            disabled={isAccountLoading}
            className={`w-full flex items-center justify-between px-4 py-2.5 rounded-lg
                       border transition-all duration-200 disabled:opacity-50 ${
                         accountType === 'REAL'
                           ? 'bg-nexus-red/10 border-nexus-red/30 hover:border-nexus-red/60'
                           : 'bg-nexus-bg border-nexus-border hover:border-nexus-green/40'
                       }`}
          >
            <span className={`text-sm font-medium ${accountType === 'REAL' ? 'text-nexus-red' : 'text-nexus-green'}`}>
              {accountType === 'REAL' ? '💰 REAL MONEY' : '🧪 DEMO (Practice)'}
            </span>
            <ArrowLeftRight className="w-4 h-4 text-nexus-text-muted" />
          </button>
        )}
      </div>
    </div>
  );
}
