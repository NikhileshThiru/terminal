import {
  createChart,
  type IChartApi,
  type ISeriesApi,
  type MouseEventParams,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts';
import { useEffect, useMemo, useRef, useState } from 'react';

import { Pane } from '../chrome/Pane';
import { useSelection } from '../chrome/SelectionContext';
import { getBars, type BarPoint } from '../lib/api';
import { palette, withAlpha } from '../lib/palette';

const API_URL = import.meta.env.VITE_API_URL ?? 'http://localhost:8000';

interface Timeframe {
  label: string;
  days: number;
  /** Alpaca bar timeframe param. 1Min/5Min/15Min/1Hour are intraday; 1Day is EOD. */
  timeframe: string;
  /** When intraday, the chart subscribes to /bars/{symbol}/stream to tick live. */
  intraday: boolean;
}

// Intraday `days` is a lookback window: the backend filters bars whose
// timestamp is within `days` of now. 3 days for 1Min so weekends still
// show Friday's session; 10 days for 5Min so a long holiday weekend
// still shows last week's session.
const TIMEFRAMES: Timeframe[] = [
  { label: '1D', days: 3, timeframe: '1Min', intraday: true },
  { label: '1W', days: 10, timeframe: '5Min', intraday: true },
  { label: '1M', days: 30, timeframe: '1Day', intraday: false },
  { label: '3M', days: 90, timeframe: '1Day', intraday: false },
  { label: '6M', days: 180, timeframe: '1Day', intraday: false },
  { label: '1Y', days: 365, timeframe: '1Day', intraday: false },
];

/**
 * Bloomberg-style chart: area price + volume histogram below + crosshair
 * OHLCV overlay + extended-hours shading on intraday timeframes.
 *
 * Intraday timeframes also open an SSE connection to /bars/{symbol}/stream
 * so the chart ticks live as new IEX bars close (free tier — IEX only, ~2%
 * of consolidated volume, DESIGN.md §5).
 */
export function ChartPane() {
  const { selectedSymbol } = useSelection();
  const [timeframe, setTimeframe] = useState<Timeframe>(TIMEFRAMES[3]);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const priceSeriesRef = useRef<ISeriesApi<'Area'> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const [bars, setBars] = useState<BarPoint[]>([]);
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [liveConnected, setLiveConnected] = useState(false);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const [bandPx, setBandPx] = useState<Array<{ left: number; width: number }>>([]);

  // Initialise the chart once.
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout: {
        background: { color: palette.bgElevated },
        textColor: palette.textMuted,
        fontSize: 10,
      },
      grid: {
        vertLines: { color: palette.border },
        horzLines: { color: palette.border },
      },
      rightPriceScale: {
        borderColor: palette.borderStrong,
        // Leave the bottom 30% for the volume histogram.
        scaleMargins: { top: 0.05, bottom: 0.3 },
      },
      timeScale: { borderColor: palette.borderStrong, timeVisible: false },
      crosshair: { mode: 1 },
      handleScroll: false,
      handleScale: false,
      autoSize: true,
    });
    const price = chart.addAreaSeries({
      lineColor: palette.up,
      topColor: withAlpha(palette.up, 0.22),
      bottomColor: withAlpha(palette.up, 0),
      lineWidth: 2,
      priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    });
    const volume = chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
      color: withAlpha(palette.up, 0.4),
    });
    chart.priceScale('volume').applyOptions({
      scaleMargins: { top: 0.75, bottom: 0 },
    });
    chartRef.current = chart;
    priceSeriesRef.current = price;
    volumeSeriesRef.current = volume;
    return () => {
      chart.remove();
      chartRef.current = null;
      priceSeriesRef.current = null;
      volumeSeriesRef.current = null;
    };
  }, []);

  // Load data whenever the symbol or timeframe changes.
  useEffect(() => {
    let cancelled = false;
    setStatus('loading');
    setErrorMsg(null);
    setHoverIdx(null);
    getBars(selectedSymbol, timeframe.timeframe, timeframe.days)
      .then((r) => {
        if (cancelled) return;
        setBars(r.bars);
        applyBarsToChart(r.bars);
        setStatus(r.bars.length === 0 ? 'error' : 'ready');
        if (r.bars.length === 0) setErrorMsg('no bars returned');
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setStatus('error');
        setBars([]);
        setErrorMsg(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [selectedSymbol, timeframe]);

  // Live ticks: subscribe to /bars/{symbol}/stream while an intraday
  // timeframe is selected.
  useEffect(() => {
    if (!timeframe.intraday) {
      setLiveConnected(false);
      return;
    }
    const es = new EventSource(`${API_URL}/bars/${encodeURIComponent(selectedSymbol)}/stream`);
    setLiveConnected(false);
    es.addEventListener('hello', () => setLiveConnected(true));
    es.addEventListener('tick', () => {
      /* keep-alive only */
    });
    es.addEventListener('bar', (e) => {
      try {
        const data = JSON.parse((e as MessageEvent<string>).data) as BarPoint & { symbol: string };
        if (data.symbol.toUpperCase() !== selectedSymbol.toUpperCase()) return;
        const point: BarPoint = {
          t: data.t,
          o: data.o,
          h: data.h,
          low: data.low,
          c: data.c,
          v: data.v,
        };
        setBars((prev) => {
          const next = [...prev];
          const epoch = Math.floor(new Date(point.t).getTime() / 1000);
          const last = next[next.length - 1];
          if (last && Math.floor(new Date(last.t).getTime() / 1000) === epoch) {
            next[next.length - 1] = point;
          } else {
            next.push(point);
          }
          const ps = priceSeriesRef.current;
          const vs = volumeSeriesRef.current;
          if (ps) ps.update({ time: epoch as UTCTimestamp, value: Number(point.c) });
          if (vs) {
            const up = Number(point.c) >= Number(point.o);
            vs.update({
              time: epoch as UTCTimestamp,
              value: point.v,
              color: barColor(up, isExtendedHours(point.t)),
            });
          }
          return next;
        });
      } catch {
        /* skip malformed */
      }
    });
    es.onerror = () => setLiveConnected(false);
    return () => {
      es.close();
      setLiveConnected(false);
    };
  }, [selectedSymbol, timeframe]);

  // Crosshair hover — show OHLCV at the hovered bar.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const handler = (param: MouseEventParams) => {
      if (!param.time || !containerRef.current) {
        setHoverIdx(null);
        return;
      }
      // param.point is in chart coords; only show hover when the cursor is
      // actually over the plot area (not off-edge).
      const rect = containerRef.current.getBoundingClientRect();
      if (param.point && (param.point.x < 0 || param.point.x > rect.width)) {
        setHoverIdx(null);
        return;
      }
      const epoch = timeToEpoch(param.time);
      if (epoch === null) {
        setHoverIdx(null);
        return;
      }
      const idx = bars.findIndex((b) => Math.floor(new Date(b.t).getTime() / 1000) === epoch);
      setHoverIdx(idx >= 0 ? idx : null);
    };
    chart.subscribeCrosshairMove(handler);
    return () => chart.unsubscribeCrosshairMove(handler);
  }, [bars]);

  // Extended-hours shading. Identify runs of consecutive bars whose
  // timestamp falls outside RTH (9:30-16:00 ET, weekdays only) and render
  // a translucent band over each run. Pixel positions are re-derived on
  // every visible-range change so the bands scroll/zoom with the chart.
  const extendedRanges = useMemo(
    () => (timeframe.intraday ? findExtendedRanges(bars) : []),
    [bars, timeframe.intraday],
  );

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    if (extendedRanges.length === 0) {
      setBandPx([]);
      return;
    }
    const update = () => {
      const ts = chart.timeScale();
      const next: Array<{ left: number; width: number }> = [];
      for (const r of extendedRanges) {
        const x1 = ts.timeToCoordinate(r.start as UTCTimestamp);
        const x2 = ts.timeToCoordinate(r.end as UTCTimestamp);
        if (x1 === null || x2 === null) continue;
        const left = Math.min(x1, x2);
        const width = Math.abs(x2 - x1) + 4; // pad by ~half bar width
        if (width > 0) next.push({ left, width });
      }
      setBandPx(next);
    };
    update();
    chart.timeScale().subscribeVisibleLogicalRangeChange(update);
    return () => chart.timeScale().unsubscribeVisibleLogicalRangeChange(update);
  }, [extendedRanges]);

  const periodStats = useMemo(() => computePeriodStats(bars), [bars]);
  const hoveredBar = hoverIdx !== null ? bars[hoverIdx] : null;

  return (
    <Pane
      title="Chart"
      subtitle={
        <ChartSubtitle
          symbol={selectedSymbol}
          period={periodStats}
          hovered={hoveredBar}
          prev={hoverIdx !== null && hoverIdx > 0 ? bars[hoverIdx - 1] : null}
        />
      }
      bodyClassName="p-0 relative"
    >
      <div className="border-border bg-bg-elevated-2 absolute top-0 right-0 left-0 z-10 flex shrink-0 items-center justify-between border-b px-2 py-1">
        <span className="text-text-dim flex items-center gap-1.5 text-[9px] tracking-wider uppercase">
          {timeframe.label} · {timeframe.timeframe.toLowerCase()}
          {timeframe.intraday && (
            <span
              className={`inline-block h-1.5 w-1.5 rounded-full ${
                liveConnected ? 'bg-accent animate-pulse' : 'bg-text-dim'
              }`}
              title={liveConnected ? 'Live IEX ticks streaming' : 'Connecting to live feed…'}
            />
          )}
          {timeframe.intraday && extendedRanges.length > 0 && (
            <span className="text-text-dim" title="Shaded bands = pre-market / after-hours">
              · ext hrs shaded
            </span>
          )}
        </span>
        <div className="flex items-center gap-0.5">
          {TIMEFRAMES.map((t) => (
            <button
              key={t.label}
              type="button"
              onClick={() => setTimeframe(t)}
              className={`rounded px-1.5 py-0.5 font-mono text-[9px] tracking-wider uppercase ${
                t.label === timeframe.label
                  ? 'bg-accent/15 text-accent border-accent/40 border'
                  : 'text-text-dim hover:text-text border border-transparent'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>
      {/* Extended-hours shading sits between the toolbar and the chart so it
          stacks behind the lightweight-charts canvas (z-0). */}
      <div className="pointer-events-none absolute inset-x-0 top-6 bottom-0 z-0 overflow-hidden">
        {bandPx.map((b, i) => (
          <div
            key={i}
            className="bg-warning/8 border-warning/15 absolute top-0 bottom-0 border-x"
            style={{ left: `${b.left}px`, width: `${b.width}px` }}
          />
        ))}
      </div>
      <div ref={containerRef} className="absolute inset-x-0 bottom-0 top-6 z-10" />
      {status === 'loading' && (
        <div className="text-text-dim absolute inset-0 z-20 flex items-center justify-center text-xs">
          loading bars…
        </div>
      )}
      {status === 'error' && (
        <div className="text-text-dim absolute inset-0 z-20 flex items-center justify-center px-3 text-center text-xs">
          chart unavailable{errorMsg ? ` · ${errorMsg}` : ''}
        </div>
      )}
    </Pane>
  );

  function applyBarsToChart(barsIn: BarPoint[]) {
    const ps = priceSeriesRef.current;
    const vs = volumeSeriesRef.current;
    if (!ps || !vs) return;
    // Colour the price area by the visible period's direction: green when
    // the range is up, red when it's down. A falling stock drawn in green
    // would lie about the data.
    if (barsIn.length >= 2) {
      const up = Number(barsIn[barsIn.length - 1].c) >= Number(barsIn[0].c);
      const lineColor = up ? palette.up : palette.down;
      ps.applyOptions({
        lineColor,
        topColor: withAlpha(lineColor, 0.22),
        bottomColor: withAlpha(lineColor, 0),
      });
    }
    const priceData = barsIn.map((b) => ({
      time: Math.floor(new Date(b.t).getTime() / 1000) as UTCTimestamp,
      value: Number(b.c),
    }));
    const volumeData = barsIn.map((b) => {
      const up = Number(b.c) >= Number(b.o);
      return {
        time: Math.floor(new Date(b.t).getTime() / 1000) as UTCTimestamp,
        value: b.v,
        color: barColor(up, isExtendedHours(b.t)),
      };
    });
    ps.setData(priceData);
    vs.setData(volumeData);
    chartRef.current?.timeScale().fitContent();
  }
}

function ChartSubtitle({
  symbol,
  period,
  hovered,
  prev,
}: {
  symbol: string;
  period: PeriodStats | null;
  hovered: BarPoint | null;
  prev: BarPoint | null;
}) {
  if (hovered) {
    const o = Number(hovered.o);
    const h = Number(hovered.h);
    const l = Number(hovered.low);
    const c = Number(hovered.c);
    const change = prev ? c - Number(prev.c) : c - o;
    const changeBase = prev ? Number(prev.c) : o;
    const changePct = changeBase !== 0 ? (change / changeBase) * 100 : 0;
    const tone = change > 0 ? 'text-up' : change < 0 ? 'text-down' : 'text-text';
    return (
      <span className="flex items-baseline gap-2">
        <span className="text-text font-mono font-semibold tracking-wider">{symbol}</span>
        <span className="text-text-dim flex items-baseline gap-2 font-mono text-[10px] tabular-nums">
          <span>
            O <span className="text-text">${o.toFixed(2)}</span>
          </span>
          <span>
            H <span className="text-text">${h.toFixed(2)}</span>
          </span>
          <span>
            L <span className="text-text">${l.toFixed(2)}</span>
          </span>
          <span>
            C <span className="text-text">${c.toFixed(2)}</span>
          </span>
          <span className={tone}>
            {change >= 0 ? '+' : ''}
            {change.toFixed(2)} ({changePct >= 0 ? '+' : ''}
            {changePct.toFixed(2)}%)
          </span>
          <span>
            V <span className="text-text">{formatVolume(hovered.v)}</span>
          </span>
        </span>
      </span>
    );
  }
  return (
    <span className="flex items-baseline gap-2">
      <span className="text-text font-mono font-semibold tracking-wider">{symbol}</span>
      {period && (
        <span className="text-text-dim flex items-baseline gap-2 font-mono text-[10px] tabular-nums">
          <span>
            last <span className="text-text">${period.last.toFixed(2)}</span>
          </span>
          <span
            className={
              period.change > 0 ? 'text-up' : period.change < 0 ? 'text-down' : 'text-text'
            }
          >
            {period.change >= 0 ? '+' : ''}
            {period.change.toFixed(2)} ({period.changePct >= 0 ? '+' : ''}
            {period.changePct.toFixed(2)}%)
          </span>
          <span>
            hi <span className="text-text">${period.high.toFixed(2)}</span>
          </span>
          <span>
            lo <span className="text-text">${period.low.toFixed(2)}</span>
          </span>
        </span>
      )}
    </span>
  );
}

interface PeriodStats {
  last: number;
  first: number;
  high: number;
  low: number;
  change: number;
  changePct: number;
}

function computePeriodStats(bars: BarPoint[]): PeriodStats | null {
  if (bars.length === 0) return null;
  const closes = bars.map((b) => Number(b.c));
  const last = closes[closes.length - 1];
  const first = closes[0];
  const high = Math.max(...bars.map((b) => Number(b.h)));
  const low = Math.min(...bars.map((b) => Number(b.low)));
  const change = last - first;
  const changePct = first !== 0 ? (change / first) * 100 : 0;
  return { last, first, high, low, change, changePct };
}

function barColor(up: boolean, extended: boolean): string {
  // Extended-hours bars use a more muted colour so the volume histogram
  // still communicates direction without competing with the shaded bands.
  const base = up ? palette.up : palette.down;
  return withAlpha(base, extended ? 0.18 : 0.4);
}

function isExtendedHours(iso: string): boolean {
  try {
    const d = new Date(iso);
    const fmt = new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York',
      hour: 'numeric',
      minute: 'numeric',
      weekday: 'short',
      hour12: false,
    });
    const parts = fmt.formatToParts(d);
    const get = (t: string) => parts.find((p) => p.type === t)?.value ?? '';
    const weekday = get('weekday');
    if (['Sat', 'Sun'].includes(weekday)) return true;
    const hour = Number(get('hour'));
    const minute = Number(get('minute'));
    const totalMinutes = hour * 60 + minute;
    const rthStart = 9 * 60 + 30;
    const rthEnd = 16 * 60;
    return totalMinutes < rthStart || totalMinutes >= rthEnd;
  } catch {
    return false;
  }
}

function findExtendedRanges(bars: BarPoint[]): Array<{ start: number; end: number }> {
  const ranges: Array<{ start: number; end: number }> = [];
  let currentStart: number | null = null;
  let prevEpoch: number | null = null;
  for (const b of bars) {
    const epoch = Math.floor(new Date(b.t).getTime() / 1000);
    if (isExtendedHours(b.t)) {
      if (currentStart === null) currentStart = epoch;
      prevEpoch = epoch;
    } else if (currentStart !== null) {
      ranges.push({ start: currentStart, end: prevEpoch ?? currentStart });
      currentStart = null;
      prevEpoch = null;
    }
  }
  if (currentStart !== null) {
    ranges.push({ start: currentStart, end: prevEpoch ?? currentStart });
  }
  return ranges;
}

function timeToEpoch(t: Time): number | null {
  if (typeof t === 'number') return t;
  // Business day objects ({year, month, day}) for daily series; convert to UTC seconds.
  if (typeof t === 'object' && t !== null && 'year' in t && 'month' in t && 'day' in t) {
    return Math.floor(Date.UTC(t.year, t.month - 1, t.day) / 1000);
  }
  return null;
}

function formatVolume(v: number): string {
  if (v >= 1_000_000_000) return `${(v / 1_000_000_000).toFixed(1)}B`;
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}K`;
  return v.toLocaleString();
}
