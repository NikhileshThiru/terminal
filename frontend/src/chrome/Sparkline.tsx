import { palette, withAlpha } from '../lib/palette';

interface SparklineProps {
  /** Numeric series; rendered as a polyline scaled to the box. */
  values: number[];
  width?: number;
  height?: number;
  /** When true, the line is drawn red; otherwise the project's accent green. */
  negative?: boolean;
}

/**
 * Tiny inline SVG sparkline. Hand-rolled because the chart libraries are
 * overkill at 80×20px and this stays static — perfect for a watchlist
 * row where it ships once and never updates.
 */
export function Sparkline({ values, width = 80, height = 20, negative = false }: SparklineProps) {
  if (values.length < 2) {
    return (
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden>
        <line x1="0" y1={height / 2} x2={width} y2={height / 2} className="stroke-text-dim/40" />
      </svg>
    );
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const stepX = width / (values.length - 1);
  const points = values
    .map((v, i) => {
      const x = i * stepX;
      const y = height - ((v - min) / range) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');
  const stroke = negative ? palette.down : palette.up;
  const fill = withAlpha(stroke, 0.1);
  const areaPoints = `0,${height} ${points} ${width},${height}`;
  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      aria-hidden
      preserveAspectRatio="none"
    >
      <polygon points={areaPoints} fill={fill} />
      <polyline points={points} fill="none" stroke={stroke} strokeWidth={1.25} />
    </svg>
  );
}
