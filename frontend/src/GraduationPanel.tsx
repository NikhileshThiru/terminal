import { useEffect, useState } from 'react';

import { getGraduation, type Graduation, type GraduationCriterion } from './lib/api';

/**
 * Shadow -> paper go/no-go (CLAUDE.md §8). The autonomous agent only logs
 * shadow trades until all three criteria hold on its own (reactive +
 * catalyst) theses. Even then nothing flips automatically: graduating is a
 * human config change, so this panel reports — it never acts.
 */
export function GraduationPanel() {
  const [g, setG] = useState<Graduation | null>(null);

  useEffect(() => {
    let cancelled = false;
    getGraduation()
      .then((r) => !cancelled && setG(r))
      .catch(() => {
        /* non-fatal: the panel just doesn't render */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!g) return null;

  return (
    <section
      className="border-border bg-bg-elevated shrink-0 rounded border"
      aria-label="Shadow to paper graduation"
    >
      <header className="border-border bg-bg-elevated-2 flex flex-wrap items-baseline justify-between gap-2 border-b px-3 py-1.5">
        <h3 className="text-text text-[11px] font-semibold tracking-[0.12em] uppercase">
          Shadow → paper go/no-go
        </h3>
        <span
          className={`rounded border px-1.5 py-0.5 text-[10px] tracking-wider uppercase ${
            g.ready ? 'border-up/50 text-up' : 'border-border-strong text-text-muted'
          }`}
        >
          {g.ready ? 'criteria met · human decision' : 'shadow only'}
        </span>
      </header>
      <div className="grid gap-2 p-3 md:grid-cols-3">
        {g.criteria.map((c) => (
          <CriterionCard key={c.key} c={c} />
        ))}
      </div>
      {g.notes.length > 0 && (
        <ul className="text-text-dim space-y-0.5 px-3 pb-2 text-[10px] leading-relaxed">
          {g.notes.map((n) => (
            <li key={n}>· {n}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

function CriterionCard({ c }: { c: GraduationCriterion }) {
  return (
    <div className="border-border rounded border px-2.5 py-2">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-text-dim text-[9px] tracking-wider uppercase">{c.label}</span>
        <span className={`text-[10px] ${c.passed ? 'text-up' : 'text-text-muted'}`}>
          {c.passed ? '✓ met' : 'not yet'}
        </span>
      </div>
      <div className="mt-1 flex items-baseline gap-1.5 font-mono tabular-nums">
        <span className="text-text text-sm font-semibold">{formatValue(c)}</span>
        <span className="text-text-dim text-[10px]">
          need {c.comparator} {formatNumber(c, c.threshold)}
        </span>
      </div>
      <ProgressBar fraction={progress(c)} passed={c.passed} />
      <p className="text-text-dim mt-1 text-[10px]">{c.detail}</p>
    </div>
  );
}

function ProgressBar({ fraction, passed }: { fraction: number; passed: boolean }) {
  return (
    <div className="bg-bg-elevated-2 mt-1.5 h-1 overflow-hidden rounded" aria-hidden>
      <div
        className={`h-full ${passed ? 'bg-up' : 'bg-accent'}`}
        style={{ width: `${Math.round(Math.max(0, Math.min(1, fraction)) * 100)}%` }}
      />
    </div>
  );
}

/** How far toward the threshold, in [0, 1]. For "<=" metrics (Brier), a
 * value at or under the threshold is complete; above it shows how far off. */
export function progress(c: GraduationCriterion): number {
  if (c.value === null) return 0;
  if (c.comparator === '>=') return c.threshold > 0 ? c.value / c.threshold : 1;
  return c.value <= c.threshold ? 1 : c.threshold / c.value;
}

function formatValue(c: GraduationCriterion): string {
  return c.value === null ? '—' : formatNumber(c, c.value);
}

function formatNumber(c: GraduationCriterion, n: number): string {
  if (c.key === 'volume') return String(Math.round(n));
  if (c.key === 'grounding') return `${(n * 100).toFixed(0)}%`;
  return n.toFixed(3);
}
