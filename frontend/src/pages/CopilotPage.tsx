import { useCallback, useEffect, useState } from 'react';

import { useModal } from '../chrome/ModalContext';
import { getRecentReactiveTheses, type RecentThesis } from '../lib/api';
import { fmtDateTimeSmart } from '../lib/format';
import { AgentReasoningPane } from '../panes/AgentReasoningPane';
import { CopilotInputPane } from '../panes/CopilotInputPane';
import { Badge } from '../ui/Badge';
import { RecentThesisModal } from '../ui/RecentThesisModal';

/**
 * Manual copilot — the ad-hoc thesis explorer. Type an idea in plain
 * English; the agent researches it with real data and emits a structured,
 * grounding-checked options thesis. Manual runs flow into the eval harness
 * alongside the autonomous ones.
 */
export function CopilotPage() {
  return (
    <div className="flex h-full min-h-0 flex-col gap-2 p-2">
      <header className="border-border bg-bg-elevated shrink-0 rounded border px-3 py-2">
        <p className="text-text-muted text-[11px] leading-relaxed">
          <span className="text-text font-semibold">
            This is the same agent that trades autonomously — pointed at your idea instead of the
            news feed.
          </span>{' '}
          Type a hunch in plain English (or click a starter below). It researches with real data,
          grounding-checks every number it cites, and returns a concrete options trade with max
          risk. Every answer is graded later against what actually happened — that&apos;s the{' '}
          <span className="text-info">manual</span> line on the Evaluation page, the control group
          for the autonomous buckets.
        </p>
      </header>
      <div
        className="grid min-h-0 flex-1 gap-2"
        style={{ gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1.4fr)' }}
      >
        <div className="flex min-h-0 flex-col gap-2">
          <div className="min-h-0 flex-1">
            <CopilotInputPane />
          </div>
          <RecentManualTheses />
        </div>
        <div className="min-h-0">
          <AgentReasoningPane />
        </div>
      </div>
    </div>
  );
}

/** Past manual runs — proof the copilot accumulates an eval track record. */
function RecentManualTheses() {
  const modal = useModal();
  const [theses, setTheses] = useState<RecentThesis[]>([]);

  const refresh = useCallback(async () => {
    try {
      const all = await getRecentReactiveTheses(30);
      setTheses(all.filter((t) => t.source_bucket === 'manual').slice(0, 8));
    } catch {
      /* non-fatal */
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = setInterval(() => void refresh(), 15000);
    return () => clearInterval(t);
  }, [refresh]);

  if (theses.length === 0) return null;

  return (
    <section className="border-border bg-bg-elevated max-h-56 shrink-0 overflow-hidden rounded border">
      <header className="border-border bg-bg-elevated-2 flex items-baseline justify-between border-b px-3 py-1.5">
        <h3 className="text-text-muted text-[10px] font-semibold tracking-[0.12em] uppercase">
          Recent manual runs
        </h3>
        <span className="text-text-dim text-[10px] tabular-nums">{theses.length}</span>
      </header>
      <ul className="divide-border max-h-44 divide-y overflow-auto">
        {theses.map((t) => (
          <li
            key={t.id}
            onClick={() =>
              modal.open({
                title: `Thesis · ${t.symbol}`,
                content: <RecentThesisModal recent={t} />,
              })
            }
            className="hover:bg-bg-elevated-2 flex cursor-pointer items-baseline gap-1.5 px-3 py-1.5 text-[11px]"
          >
            <span className="text-text-dim shrink-0 font-mono text-[9px] tabular-nums">
              {fmtDateTimeSmart(t.generated_at)}
            </span>
            <span className="text-text shrink-0 font-mono font-semibold">{t.symbol}</span>
            <span
              className={`shrink-0 font-mono font-semibold ${
                t.direction === 'long' ? 'text-up' : 'text-down'
              }`}
            >
              {t.direction.toUpperCase()} {(t.confidence * 100).toFixed(0)}%
            </span>
            <Badge tone={t.grounding_check_passed ? 'up' : 'warn'}>
              {t.grounding_check_passed ? 'grounded' : 'ungrounded'}
            </Badge>
            <span className="text-text-muted truncate">{t.reasoning}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
