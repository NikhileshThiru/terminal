import {
  createChart,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts';
import { useEffect, useMemo, useRef, useState } from 'react';

import { getEquityCurve, type EquityCurveResponse, type EquityPoint } from './lib/api';
import { palette } from './lib/palette';

/**
 * Two-line equity curve plotting conservative vs aggressive paper accounts.
 * Bloomberg-level treatment: starting-balance reference line, header stats
 * showing P&L and max drawdown per account, and the curves coloured by
 * account identity (blue = conservative, amber = aggressive) so green/red
 * stay reserved for direction. The conservative-vs-aggressive A/B IS the
 * experimental claim of the project (DESIGN.md §2.5, §8); a single chart
 * that lets you read both at once is the cleanest way to communicate it.
 */
export function EquityCurveChart() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const consSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const aggSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const refSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const [cons, setCons] = useState<EquityCurveResponse | null>(null);
  const [agg, setAgg] = useState<EquityCurveResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout: {
        background: { color: palette.bgElevated },
        textColor: palette.textMuted,
        fontSize: 11,
      },
      grid: {
        vertLines: { color: palette.border },
        horzLines: { color: palette.border },
      },
      rightPriceScale: { borderColor: palette.borderStrong },
      timeScale: { borderColor: palette.borderStrong, timeVisible: true },
      crosshair: { mode: 1 },
      autoSize: true,
    });
    const refSeries = chart.addLineSeries({
      color: palette.textDim,
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      title: 'starting',
      priceLineVisible: false,
      lastValueVisible: false,
    });
    const consSeries = chart.addLineSeries({
      color: palette.info,
      lineWidth: 2,
      title: 'conservative',
      priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    });
    const aggSeries = chart.addLineSeries({
      color: palette.accent,
      lineWidth: 2,
      title: 'aggressive',
      priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    });
    chartRef.current = chart;
    consSeriesRef.current = consSeries;
    aggSeriesRef.current = aggSeries;
    refSeriesRef.current = refSeries;
    return () => {
      chart.remove();
      chartRef.current = null;
      consSeriesRef.current = null;
      aggSeriesRef.current = null;
      refSeriesRef.current = null;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    Promise.all([
      getEquityCurve('conservative', 90).catch((e: unknown) => {
        throw e;
      }),
      getEquityCurve('aggressive', 90).catch((e: unknown) => {
        throw e;
      }),
    ])
      .then(([c, a]) => {
        if (cancelled) return;
        setCons(c);
        setAgg(a);
      })
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      cancelled = true;
    };
  }, []);

  // Push data into the chart when it arrives.
  useEffect(() => {
    const consSeries = consSeriesRef.current;
    const aggSeries = aggSeriesRef.current;
    const refSeries = refSeriesRef.current;
    if (!consSeries || !aggSeries || !refSeries) return;
    consSeries.setData(toLineData(cons));
    aggSeries.setData(toLineData(agg));
    refSeries.setData(buildReferenceLine(cons, agg));
    chartRef.current?.timeScale().fitContent();
  }, [cons, agg]);

  const consStats = useMemo(() => computeAccountStats(cons), [cons]);
  const aggStats = useMemo(() => computeAccountStats(agg), [agg]);
  const empty = (cons?.points?.length ?? 0) <= 1 && (agg?.points?.length ?? 0) <= 1;

  return (
    <section className="border-border-strong bg-bg-elevated relative w-full overflow-hidden rounded border">
      <header className="border-border bg-bg-elevated-2 flex flex-wrap items-center justify-between gap-2 border-b px-3 py-2">
        <h3 className="text-text-muted text-[11px] font-semibold tracking-wider uppercase">
          Equity curve · 90d
        </h3>
        <div className="flex flex-wrap items-center gap-3 font-mono text-[10px] tabular-nums">
          <AccountStatLine
            color={palette.info}
            label="cons"
            stats={consStats}
            starting={Number(cons?.starting_balance_usd ?? '0')}
          />
          <span className="text-text-dim">·</span>
          <AccountStatLine
            color={palette.accent}
            label="agg"
            stats={aggStats}
            starting={Number(agg?.starting_balance_usd ?? '0')}
          />
        </div>
      </header>
      <div ref={containerRef} className="relative h-[280px] w-full">
        {empty && (
          <div className="text-text-dim absolute inset-0 flex items-center justify-center px-4 text-center text-xs">
            No marks resolved yet — equity curves populate as MTM ticks land and trades close.
          </div>
        )}
        {error && (
          <div className="text-error absolute inset-x-0 bottom-4 px-4 text-center text-xs">
            equity curve unavailable · {error}
          </div>
        )}
      </div>
    </section>
  );
}

interface AccountStats {
  current: number;
  pnl: number;
  pnlPct: number;
  maxDrawdown: number;
  maxDrawdownPct: number;
}

function AccountStatLine({
  color,
  label,
  stats,
  starting,
}: {
  color: string;
  label: string;
  stats: AccountStats | null;
  starting: number;
}) {
  return (
    <span className="flex items-baseline gap-1">
      <span className="inline-block h-0.5 w-3" style={{ backgroundColor: color }} />
      <span className="text-text-dim tracking-wider uppercase">{label}</span>
      <span className="text-text">${(stats?.current ?? starting).toLocaleString()}</span>
      <span
        className={
          stats === null
            ? 'text-text-dim'
            : stats.pnl > 0
              ? 'text-up'
              : stats.pnl < 0
                ? 'text-down'
                : 'text-text'
        }
      >
        {stats === null ? '—' : `${stats.pnl >= 0 ? '+' : ''}${stats.pnlPct.toFixed(2)}%`}
      </span>
      <span className="text-text-dim">
        dd{' '}
        <span className={stats && stats.maxDrawdown < 0 ? 'text-error' : 'text-text-dim'}>
          {stats === null ? '—' : `${stats.maxDrawdownPct.toFixed(2)}%`}
        </span>
      </span>
    </span>
  );
}

function toLineData(c: EquityCurveResponse | null): { time: UTCTimestamp; value: number }[] {
  const points = c?.points;
  if (!points || points.length === 0) return [];
  // Dedupe by epoch-second — lightweight-charts requires strictly increasing
  // time keys, and multiple marks can land within the same second in dev.
  const byT = new Map<number, number>();
  for (const p of points) {
    const epoch = Math.floor(new Date(p.t).getTime() / 1000);
    byT.set(epoch, Number(p.equity));
  }
  return Array.from(byT.entries())
    .sort((a, b) => a[0] - b[0])
    .map(([t, v]) => ({ time: t as UTCTimestamp, value: v }));
}

function buildReferenceLine(
  cons: EquityCurveResponse | null,
  agg: EquityCurveResponse | null,
): { time: UTCTimestamp; value: number }[] {
  // Use whichever account has more data to span the reference line across
  // the chart's full x-range. The starting balance is the same for both
  // accounts by design (DESIGN.md §8); use the cons balance as the
  // canonical "even" baseline.
  const baseline = Number(cons?.starting_balance_usd ?? agg?.starting_balance_usd ?? 0);
  if (!baseline) return [];
  const candidate = (cons?.points?.length ?? 0) >= (agg?.points?.length ?? 0) ? cons : agg;
  const candidatePoints = candidate?.points;
  if (!candidate || !candidatePoints || candidatePoints.length === 0) return [];
  // Dedupe by epoch-second — multiple closes can land in the same second
  // in dev, and lightweight-charts requires strictly-ascending time keys.
  const byT = new Map<number, number>();
  for (const p of candidatePoints) {
    byT.set(Math.floor(new Date(p.t).getTime() / 1000), baseline);
  }
  return Array.from(byT.entries())
    .sort((a, b) => a[0] - b[0])
    .map(([t, v]) => ({ time: t as UTCTimestamp, value: v }));
}

function computeAccountStats(c: EquityCurveResponse | null): AccountStats | null {
  const points = c?.points;
  if (!c || !points || points.length === 0) return null;
  const starting = Number(c.starting_balance_usd);
  const equities = points.map((p: EquityPoint) => Number(p.equity));
  const current = equities[equities.length - 1];
  const pnl = current - starting;
  const pnlPct = starting > 0 ? (pnl / starting) * 100 : 0;
  // Max drawdown: largest (current - running_max) seen.
  let runningMax = -Infinity;
  let maxDrawdown = 0;
  for (const v of equities) {
    if (v > runningMax) runningMax = v;
    const dd = v - runningMax;
    if (dd < maxDrawdown) maxDrawdown = dd;
  }
  const maxDrawdownPct = runningMax > 0 ? (maxDrawdown / runningMax) * 100 : 0;
  return { current, pnl, pnlPct, maxDrawdown, maxDrawdownPct };
}
