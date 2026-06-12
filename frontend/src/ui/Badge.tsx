import type { ReactNode } from 'react';

export type BadgeTone = 'up' | 'down' | 'accent' | 'warn' | 'error' | 'info' | 'neutral';

const TONE: Record<BadgeTone, string> = {
  up: 'bg-up/15 text-up border-up/40',
  down: 'bg-down/15 text-down border-down/40',
  accent: 'bg-accent/15 text-accent border-accent/40',
  warn: 'bg-warning/15 text-warning border-warning/40',
  error: 'bg-error/15 text-error border-error/40',
  info: 'bg-info/15 text-info border-info/40',
  neutral: 'bg-bg-elevated-2 text-text-muted border-border',
};

/** Tiny uppercase chip — PASS/DROP, LONG/SHORT, bucket names, statuses. */
export function Badge({
  tone,
  children,
  title,
}: {
  tone: BadgeTone;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={`rounded border px-1 py-0 font-mono text-[9px] tracking-wider uppercase ${TONE[tone]}`}
    >
      {children}
    </span>
  );
}

/** Badge tone for a thesis source bucket. */
export function bucketTone(bucket: string): BadgeTone {
  if (bucket === 'catalyst') return 'warn';
  if (bucket === 'reactive') return 'accent';
  if (bucket === 'manual') return 'info';
  return 'neutral';
}

/** Badge tone for a long/short direction. */
export function directionTone(direction: string): BadgeTone {
  return direction === 'long' ? 'up' : 'down';
}
