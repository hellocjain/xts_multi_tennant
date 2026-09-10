import React, { useEffect, useRef, useState } from 'react';
import { 
  createChart, 
  IChartApi, 
  CandlestickSeries, 
  LineSeries, 
  createSeriesMarkers,
  LineStyle
} from 'lightweight-charts';
import { CandleData, ChartMarker } from '../../types/telemetry';
import { RotateCw } from 'lucide-react';

interface TradingViewChartProps {
  candles: CandleData[];
  supertrendLine?: Array<{ time: number; value: number; color?: string }>;
  upperBand?: Array<{ time: number; value: number }>;
  lowerBand?: Array<{ time: number; value: number }>;
  markers?: ChartMarker[];
  symbol: string;
  timeframe: string;
  onChangeTimeframe?: (tf: string) => void;
  onRefresh?: () => void;
  isLoading?: boolean;
}

export const TradingViewChart: React.FC<TradingViewChartProps> = ({
  candles,
  supertrendLine = [],
  upperBand = [],
  lowerBand = [],
  markers = [],
  symbol,
  timeframe,
  onChangeTimeframe,
  onRefresh,
  isLoading = false,
}) => {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<any>(null);
  const stSeriesRef = useRef<any>(null);
  const upperBandSeriesRef = useRef<any>(null);
  const lowerBandSeriesRef = useRef<any>(null);
  const markersPluginRef = useRef<any>(null);

  const [hoverData, setHoverData] = useState<{
    time?: string;
    open?: number;
    high?: number;
    low?: number;
    close?: number;
    supertrend?: number;
    distanceToFlip?: number;
  } | null>(null);

  const timeframes = ['1m', '3m', '5m', '15m', '30m', '1h'];

  useEffect(() => {
    if (!chartContainerRef.current) return;

    // Create Lightweight Chart instance (v5)
    const chart = createChart(chartContainerRef.current, {
      layout: {
        background: { color: '#080B11' },
        textColor: '#94A3B8',
        fontSize: 11,
        fontFamily: "'JetBrains Mono', monospace",
      },
      grid: {
        vertLines: { color: '#161E2E' },
        horzLines: { color: '#161E2E' },
      },
      crosshair: {
        vertLine: {
          color: '#475569',
          width: 1,
          style: LineStyle.Dashed,
          labelBackgroundColor: '#1E293B',
        },
        horzLine: {
          color: '#475569',
          width: 1,
          style: LineStyle.Dashed,
          labelBackgroundColor: '#1E293B',
        },
      },
      timeScale: {
        borderColor: '#1E293B',
        timeVisible: true,
        secondsVisible: false,
      },
      rightPriceScale: {
        borderColor: '#1E293B',
        autoScale: true,
      },
    });

    chartRef.current = chart;

    // 1. Candlestick Series (v5 addSeries API)
    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#10B981',
      downColor: '#F43F5E',
      borderUpColor: '#10B981',
      borderDownColor: '#F43F5E',
      wickUpColor: '#10B981',
      wickDownColor: '#F43F5E',
    });
    candleSeriesRef.current = candleSeries;

    // 2. SuperTrend Line Series
    const stSeries = chart.addSeries(LineSeries, {
      color: '#10B981',
      lineWidth: 2,
      priceLineVisible: false,
      crosshairMarkerVisible: true,
    });
    stSeriesRef.current = stSeries;

    // 3. Upper Band
    const upperBandSeries = chart.addSeries(LineSeries, {
      color: '#10B98133',
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      priceLineVisible: false,
      crosshairMarkerVisible: false,
    });
    upperBandSeriesRef.current = upperBandSeries;

    // 4. Lower Band
    const lowerBandSeries = chart.addSeries(LineSeries, {
      color: '#F43F5E33',
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      priceLineVisible: false,
      crosshairMarkerVisible: false,
    });
    lowerBandSeriesRef.current = lowerBandSeries;

    // Crosshair hover listener for HUD
    chart.subscribeCrosshairMove((param) => {
      if (!param.time || !param.seriesData) {
        setHoverData(null);
        return;
      }

      const cData = param.seriesData.get(candleSeries) as any;
      const sData = param.seriesData.get(stSeries) as any;

      if (cData) {
        const dTime = typeof param.time === 'number' 
          ? new Date(param.time * 1000).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false })
          : String(param.time);

        const stVal = sData ? sData.value : undefined;
        const dist = (stVal !== undefined && cData.close) 
          ? Math.round((cData.close - stVal) * 100) / 100 
          : undefined;

        setHoverData({
          time: dTime,
          open: cData.open,
          high: cData.high,
          low: cData.low,
          close: cData.close,
          supertrend: stVal,
          distanceToFlip: dist,
        });
      }
    });

    // Responsive Resize Observer
    const resizeObserver = new ResizeObserver((entries) => {
      if (entries.length === 0 || !entries[0].contentRect) return;
      const { width, height } = entries[0].contentRect;
      chart.applyOptions({ width, height });
    });

    resizeObserver.observe(chartContainerRef.current);

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  // Update Data in Series
  useEffect(() => {
    if (!chartRef.current || !candleSeriesRef.current) return;

    if (candles && candles.length > 0) {
      const sortedCandles = [...candles]
        .sort((a, b) => a.time - b.time)
        .filter((c, i, arr) => i === 0 || c.time > arr[i - 1].time);

      const formattedCandles = sortedCandles.map((c) => ({
        time: c.time as any,
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      }));

      candleSeriesRef.current.setData(formattedCandles);

      // Markers (v5 createSeriesMarkers plugin)
      if (markers && markers.length > 0) {
        const chartMarkers = markers.map((m) => ({
          time: m.time as any,
          position: m.position,
          color: m.color,
          shape: m.shape,
          text: m.text,
        }));
        try {
          if (!markersPluginRef.current) {
            markersPluginRef.current = createSeriesMarkers(candleSeriesRef.current, chartMarkers);
          } else {
            markersPluginRef.current.setMarkers(chartMarkers);
          }
        } catch {
          // Non-blocking markers fallback
        }
      }

      // SuperTrend Line
      if (stSeriesRef.current && supertrendLine && supertrendLine.length > 0) {
        const sortedST = [...supertrendLine]
          .sort((a, b) => a.time - b.time)
          .filter((c, i, arr) => i === 0 || c.time > arr[i - 1].time);

        const stData = sortedST.map((s) => ({
          time: s.time as any,
          value: s.value,
        }));
        stSeriesRef.current.setData(stData);
      }

      // Upper & Lower Bands
      if (upperBandSeriesRef.current && upperBand && upperBand.length > 0) {
        const sortedUpper = [...upperBand]
          .sort((a, b) => a.time - b.time)
          .filter((c, i, arr) => i === 0 || c.time > arr[i - 1].time);
        upperBandSeriesRef.current.setData(sortedUpper.map((u) => ({ time: u.time as any, value: u.value })));
      }

      if (lowerBandSeriesRef.current && lowerBand && lowerBand.length > 0) {
        const sortedLower = [...lowerBand]
          .sort((a, b) => a.time - b.time)
          .filter((c, i, arr) => i === 0 || c.time > arr[i - 1].time);
        lowerBandSeriesRef.current.setData(sortedLower.map((l) => ({ time: l.time as any, value: l.value })));
      }

      chartRef.current.timeScale().fitContent();
    }
  }, [candles, supertrendLine, upperBand, lowerBand, markers]);

  return (
    <div className="flex flex-col h-full bg-cardbg rounded-xl border border-bordercolor overflow-hidden">
      {/* Chart Toolbar */}
      <div className="h-10 bg-slate-950/80 border-b border-bordercolor px-3 flex items-center justify-between shrink-0 text-xs">
        <div className="flex items-center space-x-3">
          <div className="flex items-center space-x-1.5 font-bold font-mono text-slate-100">
            <span className="text-brand-400">{symbol || 'NO SYMBOL'}</span>
          </div>

          <div className="h-4 w-[1px] bg-bordercolor" />

          <div className="flex items-center space-x-1">
            {timeframes.map((tf) => (
              <button
                key={tf}
                type="button"
                onClick={() => onChangeTimeframe && onChangeTimeframe(tf)}
                className={`px-2 py-0.5 rounded font-mono text-[11px] transition ${
                  timeframe.toLowerCase() === tf.toLowerCase()
                    ? 'bg-brand-600 text-white font-bold'
                    : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'
                }`}
              >
                {tf.toUpperCase()}
              </button>
            ))}
          </div>
        </div>

        <div className="flex items-center space-x-2">
          {onRefresh && (
            <button
              type="button"
              onClick={onRefresh}
              title="Reload Chart Data"
              disabled={isLoading}
              className="p-1 text-slate-400 hover:text-slate-100 hover:bg-slate-800 rounded transition"
            >
              <RotateCw className={`w-3.5 h-3.5 ${isLoading ? 'animate-spin text-brand-400' : ''}`} />
            </button>
          )}
        </div>
      </div>

      {/* Crosshair HUD Bar */}
      <div className="h-7 bg-obsidian/90 border-b border-bordercolor/50 px-3 flex items-center justify-between text-[11px] font-mono shrink-0">
        {hoverData ? (
          <div className="flex items-center space-x-3 overflow-x-auto no-scrollbar">
            <span className="text-slate-400">{hoverData.time}</span>
            <span>
              <span className="text-slate-500">O:</span> <strong className="text-slate-200">{hoverData.open}</strong>
            </span>
            <span>
              <span className="text-slate-500">H:</span> <strong className="text-emerald-400">{hoverData.high}</strong>
            </span>
            <span>
              <span className="text-slate-500">L:</span> <strong className="text-rose-400">{hoverData.low}</strong>
            </span>
            <span>
              <span className="text-slate-500">C:</span>{' '}
              <strong className={hoverData.close! >= hoverData.open! ? 'text-emerald-400' : 'text-rose-400'}>
                {hoverData.close}
              </strong>
            </span>
            {hoverData.supertrend !== undefined && (
              <span>
                <span className="text-slate-500">SuperTrend:</span>{' '}
                <strong className="text-amber-400">{hoverData.supertrend}</strong>
              </span>
            )}
            {hoverData.distanceToFlip !== undefined && (
              <span>
                <span className="text-slate-500">Dist:</span>{' '}
                <strong className={hoverData.distanceToFlip >= 0 ? 'text-emerald-400' : 'text-rose-400'}>
                  {hoverData.distanceToFlip > 0 ? '+' : ''}
                  {hoverData.distanceToFlip}
                </strong>
              </span>
            )}
          </div>
        ) : (
          <div className="text-slate-500 italic text-[10px]">
            Hover over chart candles for OHLC + SuperTrend inspection
          </div>
        )}
      </div>

      {/* Canvas Area */}
      <div className="relative flex-1 w-full min-h-[360px]">
        <div ref={chartContainerRef} className="absolute inset-0 w-full h-full" />
        {isLoading && (
          <div className="absolute inset-0 bg-slate-950/60 backdrop-blur-[1px] flex items-center justify-center z-20">
            <div className="flex items-center space-x-2 text-xs font-mono text-brand-400">
              <RotateCw className="w-4 h-4 animate-spin" />
              <span>Loading Candlesticks...</span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};
