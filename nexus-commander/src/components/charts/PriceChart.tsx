// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — TradingView Lightweight Chart
// ═══════════════════════════════════════════════════════════════

import { useEffect, useRef } from 'react';
import { createChart, type IChartApi, type ISeriesApi, type CandlestickData, ColorType, CandlestickSeries } from 'lightweight-charts';
import { usePriceStore } from '../../store/telemetry-store';
import { COLORS } from '../../lib/constants';

export default function PriceChart() {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const { ticks, asset, currentPrice } = usePriceStore();

  // Initialize chart
  useEffect(() => {
    if (!chartContainerRef.current) return;

    const chart = createChart(chartContainerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: COLORS.TEXT_DIM,
        fontFamily: 'Inter, system-ui, sans-serif',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: `${COLORS.BORDER}80` },
        horzLines: { color: `${COLORS.BORDER}80` },
      },
      crosshair: {
        vertLine: { color: COLORS.CYAN, width: 1, style: 2, labelBackgroundColor: COLORS.SURFACE },
        horzLine: { color: COLORS.CYAN, width: 1, style: 2, labelBackgroundColor: COLORS.SURFACE },
      },
      rightPriceScale: {
        borderColor: COLORS.BORDER,
      },
      timeScale: {
        borderColor: COLORS.BORDER,
        timeVisible: true,
        secondsVisible: true,
      },
      handleScroll: { vertTouchDrag: false },
    });

    const series = chart.addSeries(CandlestickSeries, {
      upColor: COLORS.GREEN,
      downColor: COLORS.RED,
      borderUpColor: COLORS.GREEN,
      borderDownColor: COLORS.RED,
      wickUpColor: `${COLORS.GREEN}99`,
      wickDownColor: `${COLORS.RED}99`,
    });

    chartRef.current = chart;
    seriesRef.current = series;

    // Resize observer
    const observer = new ResizeObserver((entries) => {
      const { width, height } = entries[0].contentRect;
      chart.applyOptions({ width, height });
    });
    observer.observe(chartContainerRef.current);

    return () => {
      observer.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  // Update data on new ticks
  useEffect(() => {
    if (!seriesRef.current || ticks.length === 0) return;

    const last = ticks[ticks.length - 1];
    seriesRef.current.update(last as CandlestickData<any>);
  }, [ticks]);

  return (
    <div className="glass-card p-4 flex flex-col h-full">
      {/* Chart Header */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-3">
          <h3 className="text-sm font-semibold text-nexus-text">{asset || 'EURUSD'}</h3>
          <span className={`text-lg font-bold font-[family-name:var(--font-mono)] ${
            currentPrice > 0 ? 'text-nexus-cyan' : 'text-nexus-text-muted'
          }`}>
            {currentPrice > 0 ? currentPrice.toFixed(5) : '—'}
          </span>
        </div>
        <div className="flex items-center gap-2 text-xs text-nexus-text-muted">
          <span className="w-2 h-2 rounded-full bg-nexus-green animate-regime-pulse" />
          LIVE
        </div>
      </div>

      {/* Chart Container */}
      <div ref={chartContainerRef} className="flex-1 min-h-0" />
    </div>
  );
}
