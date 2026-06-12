import { useCallback, useEffect, useState } from 'react';

import { useModal } from '../chrome/ModalContext';
import { Pane } from '../chrome/Pane';
import { getRecentReactiveTheses, type RecentThesis } from '../lib/api';
import { Badge, bucketTone } from '../ui/Badge';
import { RecentThesisModal } from '../ui/RecentThesisModal';

/**
 * Live stream of recently-produced theses. Polls /autonomous/theses every
 * 6 seconds and renders each as a compact row; clicking expands the full
 * thesis (with payoff diagram) in a modal. This is the autonomous
 * product's "main page artifact" — every meaningful unit of work the
 * system does eventually lands here.
 */
export function LiveThesesPane() {
  const modal = useModal();
  const [theses, setTheses] = useState<RecentThesis[]>([]);
  const [lastRefresh, setLastRefresh] = useState<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const t = await getRecentReactiveTheses(15);
      setTheses(t);
      setLastRefresh(Date.now());
    } catch {
      /* non-fatal */
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = setInterval(() => void refresh(), 6000);
    return () => clearInterval(t);
  }, [refresh]);

  return (
    <Pane
      title="Live theses"
      subtitle={
        <span className="text-text-dim text-[10px]">
          {theses.length} recent · {lastRefresh ? formatRel(lastRefresh) : 'waiting'}
        </span>
      }
      bodyClassName="p-0"
    >
      {theses.length === 0 ? (
        <p className="text-text-dim p-3 text-xs italic">
          No theses yet. They land here as the autonomous worker produces them — usually a few per
          trading day, more during earnings season.
        </p>
      ) : (
        <ul className="divide-border divide-y">
          {theses.map((t) => (
            <ThesisRow
              key={t.id}
              thesis={t}
              onClick={() =>
                modal.open({
                  title: `Thesis · ${t.symbol}`,
                  content: <RecentThesisModal recent={t} />,
                })
              }
            />
          ))}
        </ul>
      )}
    </Pane>
  );
}

function ThesisRow({ thesis, onClick }: { thesis: RecentThesis; onClick: () => void }) {
  const time = formatHM(thesis.generated_at);
  const dirTone = thesis.direction === 'long' ? 'text-up' : 'text-down';
  const conf = (thesis.confidence * 100).toFixed(0);
  return (
    <li
      className="hover:bg-bg-elevated-2 animate-fade-in grid cursor-pointer items-baseline gap-1.5 px-2.5 py-2 text-[11px]"
      onClick={onClick}
      style={{ gridTemplateColumns: 'auto auto auto auto 1fr' }}
    >
      <span className="text-text-dim font-mono text-[9px] tabular-nums">{time}</span>
      <Badge tone={bucketTone(thesis.source_bucket)}>{thesis.source_bucket}</Badge>
      <span className="text-text shrink-0 font-mono font-semibold">{thesis.symbol}</span>
      <span className={`font-mono font-semibold ${dirTone}`}>
        {thesis.direction.toUpperCase()} {conf}%
      </span>
      <span className="text-text-muted truncate font-mono text-[11px]">{thesis.reasoning}</span>
    </li>
  );
}

function formatHM(iso: string): string {
  try {
    const d = new Date(iso);
    const pad = (n: number) => n.toString().padStart(2, '0');
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  } catch {
    return '—';
  }
}

function formatRel(ms: number): string {
  const secs = Math.floor((Date.now() - ms) / 1000);
  if (secs < 2) return 'just now';
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  return `${Math.floor(mins / 60)}h ago`;
}
