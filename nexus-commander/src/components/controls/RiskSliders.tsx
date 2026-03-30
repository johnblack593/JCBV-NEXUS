// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — Risk Sliders (Hot-Reload)
// ═══════════════════════════════════════════════════════════════

import { useState } from 'react';
import { useTelemetryStore } from '../../store/telemetry-store';
import { updateRiskSettings, toggleAIMode } from '../../lib/api';
import { SlidersHorizontal, Brain, Save } from 'lucide-react';
import toast from 'react-hot-toast';

interface SliderConfig {
  key: string;
  label: string;
  min: number;
  max: number;
  step: number;
  defaultVal: number;
  unit: string;
}

const SLIDERS: SliderConfig[] = [
  { key: 'base_size',               label: 'Position Size',        min: 1,    max: 100,  step: 1,    defaultVal: 10,   unit: '$' },
  { key: 'max_daily_trades',        label: 'Max Daily Trades',     min: 1,    max: 50,   step: 1,    defaultVal: 15,   unit: '' },
  { key: 'min_confidence',          label: 'Min Confidence',       min: 0.5,  max: 0.99, step: 0.01, defaultVal: 0.75, unit: '' },
  { key: 'cooldown_between_trades_s', label: 'Trade Cooldown',     min: 10,   max: 600,  step: 10,   defaultVal: 60,   unit: 's' },
  { key: 'min_payout',              label: 'Min Payout',           min: 50,   max: 95,   step: 1,    defaultVal: 80,   unit: '%' },
];

export default function RiskSliders() {
  const { macroRegime, panicMode, aiMode } = useTelemetryStore();
  const [values, setValues] = useState<Record<string, number>>(
    Object.fromEntries(SLIDERS.map((s) => [s.key, s.defaultVal]))
  );
  const [isSaving, setIsSaving] = useState(false);

  const isDefensive = macroRegime === 'RED' || panicMode;

  const handleSave = async () => {
    setIsSaving(true);
    try {
      await updateRiskSettings(values as any);
      toast.success('Risk parameters updated (hot-reload)');
    } catch (err) {
      toast.error(`Save failed: ${err instanceof Error ? err.message : 'Error'}`);
    } finally {
      setIsSaving(false);
    }
  };

  const handleAIToggle = async () => {
    try {
      await toggleAIMode({ enabled: !aiMode });
      toast.success(`AI Mode ${!aiMode ? 'enabled' : 'disabled'}`);
    } catch (err) {
      toast.error(`Toggle failed: ${err instanceof Error ? err.message : 'Error'}`);
    }
  };

  return (
    <div className="space-y-4">
      {/* AI Mode Toggle */}
      <div className={`glass-card p-4 transition-all duration-300
                       ${aiMode ? 'border-nexus-purple/40 bg-nexus-purple/5' : ''}`}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className={`w-9 h-9 rounded-lg flex items-center justify-center transition-colors
                            ${aiMode ? 'bg-nexus-purple/20' : 'bg-nexus-surface-2'}`}>
              <Brain className={`w-5 h-5 ${aiMode ? 'text-nexus-purple' : 'text-nexus-text-muted'}`} />
            </div>
            <div>
              <h3 className="text-sm font-medium text-nexus-text">AI Dynamic Tuning</h3>
              <p className="text-xs text-nexus-text-muted">Auto-optimize params per regime</p>
            </div>
          </div>
          <button
            onClick={handleAIToggle}
            className={`relative w-12 h-6 rounded-full transition-colors duration-300 ${
              aiMode ? 'bg-nexus-purple' : 'bg-nexus-border-light'
            }`}
          >
            <div className={`absolute top-0.5 w-5 h-5 rounded-full bg-white shadow-md transition-transform duration-300 ${
              aiMode ? 'translate-x-6.5' : 'translate-x-0.5'
            }`} />
          </button>
        </div>
      </div>

      {/* Regime Alert */}
      {isDefensive && (
        <div className="glass-card p-3 border-nexus-red/40 bg-nexus-red/5 animate-slide-in">
          <p className="text-xs text-nexus-red font-medium">
            ⚠️ {panicMode ? 'PANIC MODE — Trading halted, parameters locked' : 'RED REGIME — Defensive parameters active'}
          </p>
        </div>
      )}

      {/* Sliders */}
      <div className="glass-card p-5">
        <div className="flex items-center gap-2 mb-4">
          <SlidersHorizontal className="w-4 h-4 text-nexus-cyan" />
          <h3 className="text-sm font-medium text-nexus-text">Risk Parameters</h3>
        </div>

        <div className="space-y-5">
          {SLIDERS.map((slider) => (
            <div key={slider.key}>
              <div className="flex items-center justify-between mb-1.5">
                <span className="text-xs text-nexus-text-dim">{slider.label}</span>
                <span className={`text-xs font-bold font-[family-name:var(--font-mono)]
                                  ${isDefensive ? 'text-nexus-red' : 'text-nexus-cyan'}`}>
                  {values[slider.key]}{slider.unit}
                </span>
              </div>
              <input
                type="range"
                min={slider.min}
                max={slider.max}
                step={slider.step}
                value={values[slider.key]}
                onChange={(e) => setValues((v) => ({ ...v, [slider.key]: Number(e.target.value) }))}
                disabled={panicMode}
                className="w-full h-1.5 rounded-full appearance-none cursor-pointer
                           bg-nexus-border disabled:opacity-40 disabled:cursor-not-allowed
                           [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-4
                           [&::-webkit-slider-thumb]:h-4 [&::-webkit-slider-thumb]:rounded-full
                           [&::-webkit-slider-thumb]:bg-nexus-cyan [&::-webkit-slider-thumb]:shadow-md
                           [&::-webkit-slider-thumb]:shadow-nexus-cyan/30 [&::-webkit-slider-thumb]:cursor-pointer
                           [&::-webkit-slider-thumb]:transition-all [&::-webkit-slider-thumb]:hover:scale-125"
              />
            </div>
          ))}
        </div>

        <button
          onClick={handleSave}
          disabled={isSaving || panicMode}
          className="w-full mt-5 flex items-center justify-center gap-2 py-2.5 rounded-lg
                     bg-nexus-cyan/10 border border-nexus-cyan/30 text-nexus-cyan text-sm font-medium
                     hover:bg-nexus-cyan/20 transition-all duration-200 disabled:opacity-40"
        >
          {isSaving ? (
            <div className="w-4 h-4 border-2 border-nexus-cyan/30 border-t-nexus-cyan rounded-full animate-spin" />
          ) : (
            <>
              <Save className="w-4 h-4" />
              Apply & Hot-Reload
            </>
          )}
        </button>
      </div>
    </div>
  );
}
