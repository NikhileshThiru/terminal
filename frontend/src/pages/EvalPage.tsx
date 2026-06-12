import { useEffect, useState } from 'react';

import {
  getCalibration,
  getEvalOutcomes,
  getEvalSummary,
  type BucketSummary,
  type CalibrationPoint,
  type CalibrationResponse,
  type EvalSummary,
  type OutcomeRow,
  type SourceBucket,
} from '../lib/api';
import { fmtDateTimeSmart, fmtPct } from '../lib/format';
import { palette, withAlpha } from '../lib/palette';
import { Badge, bucketTone } from '../ui/Badge';
import { EmptyState } from '../ui/EmptyState';

const BUCKET_BLURB: Record<SourceBucket, string> = {
  manual: 'user-prompted copilot runs',
  reactive: 'news-driven, latency-disadvantaged',
  catalyst: 'scheduled events, pre-positioned — where edge is plausible',
};

/**
 * Evaluation harness — the project's crown jewel (DESIGN.md §2.5/§2.6).
 * Per-bucket Brier + hit rate, and the calibration plot for the selected
 * bucket. Honest about sample size: no fake zeros, outcomes populate at
 * expiration.
 */
export function EvalPage() {
  const [summary, setSummary] = useState<EvalSummary | null>(null);
  const [activeBucket, setActiveBucket] = useState<SourceBucket>('manual');
  const [calibration, setCalibration] = useState<CalibrationResponse | null>(null);
  const [outcomes, setOutcomes] = useState<OutcomeRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getEvalSummary()
      .then((s) => !cancelled && setSummary(s))
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : String(e)));
    getEvalOutcomes(undefined, 25)
      .then((o) => !cancelled && setOutcomes(o))
      .catch(() => {
        /* non-fatal */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    getCalibration(activeBucket)
      .then((c) => !cancelled && setCalibration(c))
      .catch(() => {
        /* non-fatal */
      });
    return () => {
      cancelled = true;
    };
  }, [activeBucket]);

  return (
    <div className="flex h-full min-h-0 flex-col gap-2 overflow-auto p-2">
      <header className="border-border bg-bg-elevated flex shrink-0 flex-wrap items-baseline justify-between gap-2 rounded border px-3 py-2">
        <h2 className="text-text text-xs font-semibold tracking-[0.12em] uppercase">
          Evaluation harness
        </h2>
        <p className="text-text-dim text-[10px]">
          forward-tested only · scored per source bucket · Brier 0.25 = coin-flip baseline
        </p>
      </header>

      {error && (
        <p className="border-error/40 bg-error/10 text-error rounded border px-3 py-2 text-xs">
          Failed to load eval summary: {error}
        </p>
      )}

      <div className="grid shrink-0 gap-2 md:grid-cols-3">
        {(summary?.buckets ?? []).map((b) => (
          <BucketCard
            key={b.bucket}
            bucket={b}
            active={b.bucket === activeBucket}
            onSelect={() => setActiveBucket(b.bucket)}
          />
        ))}
      </div>

      <div className="grid min-h-0 flex-1 gap-2 lg:grid-cols-[1.4fr_1fr]">
        <section className="border-border bg-bg-elevated flex min-h-[420px] flex-col overflow-hidden rounded border">
          <header className="border-border bg-bg-elevated-2 flex flex-wrap items-baseline justify-between gap-2 border-b px-3 py-1.5">
            <h3 className="text-text-muted flex items-baseline gap-2 text-[10px] font-semibold tracking-[0.12em] uppercase">
              Calibration
              <Badge tone={bucketTone(activeBucket)}>{activeBucket}</Badge>
            </h3>
            <span className="text-text-dim text-[10px]">
              stated confidence vs realized hit rate — perfect calibration lies on the diagonal
            </span>
          </header>
          <div className="flex min-h-0 flex-1 items-center justify-center p-3">
            <CalibrationPlot calibration={calibration} bucketLabel={activeBucket} />
          </div>
        </section>

        <section className="border-border bg-bg-elevated flex min-h-[420px] flex-col overflow-hidden rounded border">
          <header className="border-border bg-bg-elevated-2 flex items-baseline justify-between border-b px-3 py-1.5">
            <h3 className="text-text-muted text-[10px] font-semibold tracking-[0.12em] uppercase">
              Resolved outcomes
            </h3>
            <span className="text-text-dim text-[10px] tabular-nums">
              {outcomes.length} graded predictions
            </span>
          </header>
          {outcomes.length === 0 ? (
            <EmptyState
              title="No graded predictions yet."
              hint="Each thesis is scored against what the underlying actually did, at the end of its prediction window."
            />
          ) : (
            <ul className="divide-border min-h-0 flex-1 divide-y overflow-auto">
              {outcomes.map((o) => (
                <OutcomeListRow key={o.thesis_id} o={o} />
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}

function OutcomeListRow({ o }: { o: OutcomeRow }) {
  return (
    <li className="px-3 py-2 text-[11px]">
      <div className="flex items-baseline gap-1.5">
        <Badge tone={o.hit ? 'up' : 'down'}>{o.hit ? 'HIT' : 'MISS'}</Badge>
        <Badge tone={bucketTone(o.source_bucket)}>{o.source_bucket}</Badge>
        <span className="text-text font-mono font-semibold">{o.symbol}</span>
        <span
          className={`font-mono font-semibold ${o.direction === 'long' ? 'text-up' : 'text-down'}`}
        >
          {o.direction.toUpperCase()} {(o.confidence * 100).toFixed(0)}%
        </span>
        <span className="text-text-dim ml-auto font-mono text-[9px]" title={o.evaluated_at}>
          {fmtDateTimeSmart(o.evaluated_at)}
        </span>
      </div>
      <p className="text-text-muted mt-1 font-mono text-[10px] tabular-nums">
        predicted {o.direction} · underlying went {o.realized_direction}{' '}
        <span className={o.pct_move >= 0 ? 'text-up' : 'text-down'}>({fmtPct(o.pct_move)})</span>
        {' · '}${o.underlying_price_at_thesis.toFixed(2)} → ${o.underlying_price_at_eval.toFixed(2)}
      </p>
      {o.notes && <p className="text-text-dim mt-0.5 text-[10px] italic">{o.notes}</p>}
    </li>
  );
}

function BucketCard({
  bucket,
  active,
  onSelect,
}: {
  bucket: BucketSummary;
  active: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={`cursor-pointer rounded border p-3 text-left transition-colors ${
        active
          ? 'border-accent/50 bg-accent/5'
          : 'border-border bg-bg-elevated hover:border-border-strong'
      }`}
    >
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <Badge tone={bucketTone(bucket.bucket)}>{bucket.bucket}</Badge>
        <span className="text-text-dim truncate text-[9px]">{BUCKET_BLURB[bucket.bucket]}</span>
      </div>
      <div className="grid grid-cols-4 gap-2 font-mono text-[11px] tabular-nums">
        <Metric label="theses" value={String(bucket.count_theses)} />
        <Metric label="resolved" value={String(bucket.count_resolved)} />
        <Metric
          label="brier"
          value={bucket.brier === null ? '—' : bucket.brier.toFixed(3)}
          tone={bucket.brier === null ? undefined : bucket.brier < 0.25 ? 'text-up' : 'text-down'}
        />
        <Metric
          label="hit"
          value={bucket.hit_rate === null ? '—' : `${(bucket.hit_rate * 100).toFixed(0)}%`}
          tone={
            bucket.hit_rate === null ? undefined : bucket.hit_rate >= 0.5 ? 'text-up' : 'text-down'
          }
        />
      </div>
    </button>
  );
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div>
      <div className="text-text-dim text-[9px] tracking-wider uppercase">{label}</div>
      <div className={tone ?? 'text-text'}>{value}</div>
    </div>
  );
}

/** Hand-rolled SVG calibration plot — gridlines, diagonal, count-scaled points. */
function CalibrationPlot({
  calibration,
  bucketLabel,
}: {
  calibration: CalibrationResponse | null;
  bucketLabel: SourceBucket;
}) {
  const width = 640;
  const height = 520;
  const pad = 48;
  const innerW = width - 2 * pad;
  const innerH = height - 2 * pad;

  const points = calibration?.points ?? [];
  const hasData = points.length > 0;
  const gridSteps = [0.25, 0.5, 0.75];

  const x = (v: number) => pad + v * innerW;
  const y = (v: number) => pad + innerH - v * innerH;

  return (
    <div className="flex h-full w-full flex-col items-center gap-2">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="min-h-0 w-full flex-1 font-mono"
        role="img"
        aria-label={`Calibration plot for ${bucketLabel}`}
      >
        {/* gridlines */}
        {gridSteps.map((s) => (
          <g key={s}>
            <line x1={x(s)} y1={pad} x2={x(s)} y2={height - pad} stroke={palette.border} />
            <line x1={pad} y1={y(s)} x2={width - pad} y2={y(s)} stroke={palette.border} />
            <text
              x={x(s)}
              y={height - pad + 14}
              fill={palette.textDim}
              fontSize={9}
              textAnchor="middle"
            >
              {s}
            </text>
            <text x={pad - 8} y={y(s) + 3} fill={palette.textDim} fontSize={9} textAnchor="end">
              {s}
            </text>
          </g>
        ))}

        {/* axes */}
        <line
          x1={pad}
          y1={height - pad}
          x2={width - pad}
          y2={height - pad}
          stroke={palette.borderStrong}
        />
        <line x1={pad} y1={pad} x2={pad} y2={height - pad} stroke={palette.borderStrong} />

        {/* perfect-calibration diagonal */}
        <line
          x1={x(0)}
          y1={y(0)}
          x2={x(1)}
          y2={y(1)}
          stroke={palette.textDim}
          strokeDasharray="4 4"
        />
        <text
          x={x(0.78)}
          y={y(0.83)}
          fill={palette.textDim}
          fontSize={9}
          transform={`rotate(-34 ${x(0.78)} ${y(0.83)})`}
        >
          perfect calibration
        </text>

        {/* 0/1 corner ticks */}
        <text x={pad} y={height - pad + 14} fill={palette.textDim} fontSize={9} textAnchor="middle">
          0
        </text>
        <text
          x={width - pad}
          y={height - pad + 14}
          fill={palette.textDim}
          fontSize={9}
          textAnchor="middle"
        >
          1
        </text>
        <text x={pad - 8} y={pad + 3} fill={palette.textDim} fontSize={9} textAnchor="end">
          1
        </text>

        {/* axis labels */}
        <text
          x={width / 2}
          y={height - 8}
          fill={palette.textMuted}
          fontSize={10}
          textAnchor="middle"
        >
          stated confidence
        </text>
        <text
          x={-height / 2}
          y={14}
          fill={palette.textMuted}
          fontSize={10}
          transform="rotate(-90)"
          textAnchor="middle"
        >
          realized hit rate
        </text>

        {/* data points */}
        {points.map((p) => (
          <PlotPoint key={p.bucket_lower} p={p} x={x} y={y} />
        ))}
      </svg>

      {!hasData && (
        <p className="text-text-dim max-w-md text-center text-[11px] leading-relaxed">
          No resolved outcomes in the <span className="text-text-muted">{bucketLabel}</span> bucket
          yet. Outcomes populate at expiration (or when a stop-loss / theta-exit rule trips). Until
          then the plot has nothing to draw — that&apos;s the honest empty state, not a bug.
        </p>
      )}
    </div>
  );
}

function PlotPoint({
  p,
  x,
  y,
}: {
  p: CalibrationPoint;
  x: (v: number) => number;
  y: (v: number) => number;
}) {
  // Bigger circle for buckets with more theses, capped so it stays readable.
  const r = Math.min(12, 4 + Math.sqrt(p.count) * 1.5);
  return (
    <g>
      <circle
        cx={x(p.mean_confidence)}
        cy={y(p.realized_hit_rate)}
        r={r}
        fill={withAlpha(palette.accent, 0.35)}
        stroke={palette.accent}
        strokeWidth={1.5}
      />
      <title>{`conf ${p.mean_confidence.toFixed(2)} · hit ${(p.realized_hit_rate * 100).toFixed(0)}% · n=${p.count}`}</title>
    </g>
  );
}
