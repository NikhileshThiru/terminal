import type { ReactNode } from 'react';

export type StatTone = 'up' | 'down' | 'accent' | 'warn' | 'error' | 'info' | 'default';

const VALUE_TONE: Record<StatTone, string> = {
  up: 'text-up',
  down: 'text-down',
  accent: 'text-accent',
  warn: 'text-warning',
  error: 'text-error',
  info: 'text-info',
  default: 'text-text',
};

/** Label-over-value cell for dense status grids. */
export function StatCell({
  label,
  value,
  tone = 'default',
  hint,
}: {
  label: string;
  value: ReactNode;
  tone?: StatTone;
  hint?: string;
}) {
  return (
    <div className="border-border bg-bg-elevated min-w-0 rounded border px-2.5 py-1.5" title={hint}>
      <div className="text-text-dim text-[9px] tracking-wider uppercase">{label}</div>
      <div className={`truncate font-mono text-xs tabular-nums ${VALUE_TONE[tone]}`}>{value}</div>
    </div>
  );
}

/** Inline label+value pair for single-row stat strips. */
export function StatInline({
  label,
  value,
  tone = 'default',
}: {
  label: string;
  value: ReactNode;
  tone?: StatTone;
}) {
  return (
    <span className="flex items-baseline gap-1 font-mono">
      <span className="text-text-dim text-[9px] tracking-wider uppercase">{label}</span>
      <span className={`text-[11px] tabular-nums ${VALUE_TONE[tone]}`}>{value}</span>
    </span>
  );
}
