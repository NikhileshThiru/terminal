import { type SuggestedContract } from './lib/api';
import { palette, withAlpha } from './lib/palette';

/**
 * Payoff diagram — P&L at expiration as a function of the underlying price.
 *
 * Long call:  P&L(S) = max(0, S - K) * 100 * n  -  premium * 100 * n
 * Long put:   P&L(S) = max(0, K - S) * 100 * n  -  premium * 100 * n
 *
 * That's piecewise linear, with one kink at the strike. Hand-rolled SVG —
 * the chart is too simple to justify a charting dependency.
 */

interface Props {
  contract: SuggestedContract;
  /** Current underlying price, if known (drawn as a vertical reference line). */
  currentUnderlying?: number | null;
  width?: number;
  height?: number;
}

interface PayoffMath {
  strike: number;
  premium: number;
  contracts: number;
  breakEven: number;
  maxLoss: number; // negative number
  /** Where to anchor the x-range. */
  xMin: number;
  xMax: number;
  isCall: boolean;
}

function payoffAt(s: number, m: PayoffMath): number {
  const intrinsic = m.isCall ? Math.max(0, s - m.strike) : Math.max(0, m.strike - s);
  return (intrinsic - m.premium) * 100 * m.contracts;
}

function buildMath(c: SuggestedContract, currentUnderlying: number | null): PayoffMath {
  const strike = Number(c.strike);
  const premium = Number(c.estimated_premium_per_contract);
  const contracts = c.contracts;
  const isCall = c.option_type === 'call';
  const breakEven = isCall ? strike + premium : strike - premium;
  const maxLoss = -premium * 100 * contracts;

  // x-range: centered on strike, with ±30% by default. Widen if currentUnderlying
  // is outside that window so it's visible.
  const halfRange = strike * 0.3;
  let xMin = Math.max(0, strike - halfRange);
  let xMax = strike + halfRange;
  if (currentUnderlying !== null && currentUnderlying !== undefined) {
    xMin = Math.min(xMin, currentUnderlying - 5);
    xMax = Math.max(xMax, currentUnderlying + 5);
  }
  return { strike, premium, contracts, breakEven, maxLoss, xMin, xMax, isCall };
}

export function PayoffDiagram({
  contract,
  currentUnderlying = null,
  width = 520,
  height = 240,
}: Props) {
  const pad = { top: 16, right: 24, bottom: 32, left: 56 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;

  const m = buildMath(contract, currentUnderlying ?? null);

  // Sample at strike + endpoints (piecewise-linear, so 3 points is enough).
  const samplePrices = [m.xMin, m.strike, m.xMax];
  const samplePayoffs = samplePrices.map((p) => payoffAt(p, m));
  // Add the break-even so the zero-crossing is visually exact.
  if (m.breakEven > m.xMin && m.breakEven < m.xMax) {
    samplePrices.splice(m.isCall ? 1 : 2, 0, m.breakEven);
    samplePayoffs.splice(m.isCall ? 1 : 2, 0, 0);
  }

  // y-range: ±max(|maxLoss|, maxIntrinsicProfit). We want the zero line in view.
  const yMaxRaw = Math.max(...samplePayoffs);
  const yMinRaw = Math.min(...samplePayoffs);
  const yMax = Math.max(Math.abs(yMaxRaw), Math.abs(yMinRaw)) * 1.1;
  const yMin = -yMax;

  const xToPx = (x: number): number => pad.left + ((x - m.xMin) / (m.xMax - m.xMin)) * innerW;
  const yToPx = (y: number): number => pad.top + ((yMax - y) / (yMax - yMin)) * innerH;

  // Build the payoff polyline.
  const points = samplePrices.map((x, i) => `${xToPx(x)},${yToPx(samplePayoffs[i])}`).join(' ');

  // Profit zone: from the break-even to the chart edge (in the direction of profit).
  // Loss zone: from the chart edge (opposite side) to the break-even.
  let profitPolygon = '';
  let lossPolygon = '';
  if (m.isCall) {
    // Profit: x >= breakEven, y between 0 and payoff line.
    const beX = xToPx(m.breakEven);
    const xMaxPx = xToPx(m.xMax);
    const yMaxPx = yToPx(payoffAt(m.xMax, m));
    const y0 = yToPx(0);
    profitPolygon = `${beX},${y0} ${xMaxPx},${y0} ${xMaxPx},${yMaxPx}`;
    // Loss: x <= strike, payoff is constant at maxLoss.
    const xMinPx = xToPx(m.xMin);
    const kX = xToPx(m.strike);
    const beX2 = xToPx(m.breakEven);
    const yLossPx = yToPx(m.maxLoss);
    lossPolygon = `${xMinPx},${y0} ${beX2},${y0} ${kX},${yLossPx} ${xMinPx},${yLossPx}`;
  } else {
    // Long put: profit when S < breakEven.
    const beX = xToPx(m.breakEven);
    const xMinPx = xToPx(m.xMin);
    const yLeftPx = yToPx(payoffAt(m.xMin, m));
    const y0 = yToPx(0);
    profitPolygon = `${xMinPx},${y0} ${beX},${y0} ${xMinPx},${yLeftPx}`;
    const xMaxPx = xToPx(m.xMax);
    const kX = xToPx(m.strike);
    const yLossPx = yToPx(m.maxLoss);
    lossPolygon = `${beX},${y0} ${xMaxPx},${y0} ${xMaxPx},${yLossPx} ${kX},${yLossPx}`;
  }

  const fmtUsd = (n: number) => {
    const sign = n < 0 ? '-' : '';
    const abs = Math.abs(n);
    return `${sign}$${abs.toFixed(0)}`;
  };

  const tickStyle = { fill: palette.textDim, fontSize: 9 } as const;
  const labelStyle = { fontSize: 9 } as const;

  return (
    <figure className="m-0 space-y-2">
      <figcaption className="text-text-dim text-[10px] tracking-wider uppercase">
        Payoff at expiration · {m.isCall ? 'Long call' : 'Long put'} · {contract.contracts}{' '}
        {contract.contracts === 1 ? 'contract' : 'contracts'}
      </figcaption>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        style={{ maxWidth: width }}
        className="font-mono"
        role="img"
        aria-label={`Payoff diagram for ${contract.occ_symbol}`}
      >
        {/* shaded zones */}
        <polygon points={lossPolygon} fill={withAlpha(palette.down, 0.12)} />
        <polygon points={profitPolygon} fill={withAlpha(palette.up, 0.12)} />

        {/* axes */}
        <line
          x1={pad.left}
          y1={yToPx(0)}
          x2={width - pad.right}
          y2={yToPx(0)}
          stroke={palette.textDim}
          strokeWidth={1}
        />
        <line
          x1={pad.left}
          y1={pad.top}
          x2={pad.left}
          y2={height - pad.bottom}
          stroke={palette.borderStrong}
          strokeWidth={1}
        />

        {/* strike marker */}
        <line
          x1={xToPx(m.strike)}
          y1={pad.top}
          x2={xToPx(m.strike)}
          y2={height - pad.bottom}
          stroke={palette.textMuted}
          strokeWidth={1}
          strokeDasharray="3 3"
        />
        <text
          x={xToPx(m.strike)}
          y={pad.top - 4}
          style={{ ...labelStyle, fill: palette.textMuted }}
          textAnchor="middle"
        >
          K=${m.strike.toFixed(0)}
        </text>

        {/* break-even marker */}
        {m.breakEven > m.xMin && m.breakEven < m.xMax && (
          <>
            <line
              x1={xToPx(m.breakEven)}
              y1={pad.top}
              x2={xToPx(m.breakEven)}
              y2={height - pad.bottom}
              stroke={palette.accent}
              strokeWidth={1}
              strokeDasharray="4 3"
            />
            <text
              x={xToPx(m.breakEven)}
              y={height - pad.bottom + 14}
              style={{ ...labelStyle, fill: palette.accent }}
              textAnchor="middle"
            >
              BE=${m.breakEven.toFixed(2)}
            </text>
          </>
        )}

        {/* current underlying marker */}
        {currentUnderlying !== null &&
          currentUnderlying !== undefined &&
          currentUnderlying >= m.xMin &&
          currentUnderlying <= m.xMax && (
            <>
              <line
                x1={xToPx(currentUnderlying)}
                y1={pad.top}
                x2={xToPx(currentUnderlying)}
                y2={height - pad.bottom}
                stroke={palette.info}
                strokeWidth={1}
                strokeDasharray="2 3"
              />
              <text
                x={xToPx(currentUnderlying)}
                y={pad.top + 12}
                style={{ ...labelStyle, fill: palette.info }}
                textAnchor="middle"
              >
                S₀=${currentUnderlying.toFixed(2)}
              </text>
            </>
          )}

        {/* payoff line */}
        <polyline points={points} fill="none" stroke={palette.text} strokeWidth={2} />

        {/* y-axis labels */}
        <text x={pad.left - 6} y={yToPx(0) + 3} style={tickStyle} textAnchor="end">
          $0
        </text>
        <text x={pad.left - 6} y={yToPx(yMax) + 8} style={tickStyle} textAnchor="end">
          {fmtUsd(yMax)}
        </text>
        <text x={pad.left - 6} y={yToPx(yMin)} style={tickStyle} textAnchor="end">
          {fmtUsd(yMin)}
        </text>

        {/* x-axis labels */}
        <text x={pad.left} y={height - 6} style={tickStyle} textAnchor="start">
          ${m.xMin.toFixed(0)}
        </text>
        <text x={width - pad.right} y={height - 6} style={tickStyle} textAnchor="end">
          ${m.xMax.toFixed(0)}
        </text>

        {/* x-axis title */}
        <text
          x={(pad.left + (width - pad.right)) / 2}
          y={height - 6}
          style={{ ...tickStyle, fill: palette.textDim }}
          textAnchor="middle"
        >
          underlying at expiry
        </text>
      </svg>

      <div className="grid grid-cols-3 gap-2">
        <PayoffStat label="Max loss" value={fmtUsd(m.maxLoss)} variant="loss" />
        <PayoffStat
          label="Break-even"
          value={`$${m.breakEven.toFixed(2)}`}
          variant="neutral"
          hint={
            currentUnderlying !== null && currentUnderlying !== undefined
              ? `${(((m.breakEven - currentUnderlying) / currentUnderlying) * 100).toFixed(1)}% from spot`
              : null
          }
        />
        <PayoffStat
          label={m.isCall ? 'Profit above' : 'Profit below'}
          value={`$${m.breakEven.toFixed(2)}`}
          variant="profit"
        />
      </div>
    </figure>
  );
}

function PayoffStat({
  label,
  value,
  variant,
  hint,
}: {
  label: string;
  value: string;
  variant: 'profit' | 'loss' | 'neutral';
  hint?: string | null;
}) {
  const valueTone =
    variant === 'profit' ? 'text-up' : variant === 'loss' ? 'text-down' : 'text-accent';
  return (
    <div className="border-border bg-bg-elevated-2 rounded border px-2.5 py-2">
      <div className="text-text-dim text-[9px] tracking-wider uppercase">{label}</div>
      <div className={`font-mono text-sm font-semibold tabular-nums ${valueTone}`}>{value}</div>
      {hint && <div className="text-text-dim text-[9px]">{hint}</div>}
    </div>
  );
}
