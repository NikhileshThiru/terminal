import { useCallback, useEffect, useState } from 'react';

import { useModal } from '../chrome/ModalContext';
import {
  getAutonomousStatus,
  getRecentReactiveTheses,
  getUpcomingCatalysts,
  startAutonomous,
  stopAutonomous,
  type RecentThesis,
  type UpcomingCatalyst,
  type WorkerStatus,
} from '../lib/api';
import { fmtHM, fmtHMS } from '../lib/format';
import { Badge, bucketTone } from '../ui/Badge';
import { EmptyState } from '../ui/EmptyState';
import { RecentThesisModal } from '../ui/RecentThesisModal';
import { StatCell } from '../ui/StatCell';

/**
 * Autonomous worker deep-dive: full status grid, reactive theses, the
 * catalyst calendar, and the triage feed. Watches the whole market —
 * Alpaca's Benzinga feed (universe-wide) + EDGAR's latest-filings feed.
 * Watchlist is prioritization only: rows sort watchlist symbols first,
 * nothing is filtered.
 */
export function AutonomousPage() {
  const [status, setStatus] = useState<WorkerStatus | null>(null);
  const [theses, setTheses] = useState<RecentThesis[]>([]);
  const [catalysts, setCatalysts] = useState<UpcomingCatalyst[]>([]);
  const [toggling, setToggling] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [s, t, c] = await Promise.all([
        getAutonomousStatus(),
        getRecentReactiveTheses(10),
        getUpcomingCatalysts(14),
      ]);
      setStatus(s);
      setTheses(t);
      setCatalysts(c);
      setErrorMsg(null);
    } catch (e) {
      setErrorMsg(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = setInterval(() => void refresh(), 8000);
    return () => clearInterval(t);
  }, [refresh]);

  async function toggle() {
    if (toggling) return;
    setToggling(true);
    setErrorMsg(null);
    try {
      const next = status?.state === 'running' ? await stopAutonomous() : await startAutonomous();
      setStatus(next);
    } catch (e) {
      setErrorMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setToggling(false);
    }
  }

  const running = status?.state === 'running';
  const watchlist = status?.watchlist ?? [];

  return (
    <div className="flex h-full min-h-0 flex-col gap-2 overflow-auto p-2">
      <header className="border-border bg-bg-elevated flex shrink-0 flex-wrap items-center justify-between gap-3 rounded border px-3 py-2">
        <div className="flex items-center gap-3">
          <h2 className="text-text text-xs font-semibold tracking-[0.12em] uppercase">
            Autonomous worker
          </h2>
          <span className="text-text-dim hidden text-[10px] sm:inline">
            universe-wide ingest → dedup → LLM triage → thesis funnel · watchlist sorts, never
            filters
          </span>
        </div>
        <button
          type="button"
          onClick={() => void toggle()}
          disabled={toggling}
          className={`flex cursor-pointer items-center gap-2 rounded border px-3 py-1.5 font-mono text-[11px] tracking-wider uppercase transition-colors ${
            running
              ? 'border-accent text-accent bg-accent/10 hover:bg-accent/15'
              : 'border-border-strong text-text-muted hover:text-text hover:border-text-muted'
          } disabled:opacity-50`}
        >
          <span
            className={`h-2 w-2 rounded-full ${running ? 'bg-accent animate-pulse' : 'bg-text-dim'}`}
          />
          {toggling
            ? 'toggling…'
            : running
              ? 'running — click to stop'
              : 'stopped — click to start'}
        </button>
      </header>

      {errorMsg && (
        <div
          className="border-error/40 bg-error/10 text-error rounded border px-3 py-2 text-xs"
          role="alert"
        >
          {errorMsg}
        </div>
      )}

      {status && <StatusGrid status={status} />}

      <div className="grid min-h-0 shrink-0 gap-2 lg:grid-cols-2">
        <ReactiveThesesCard theses={theses} watchlist={watchlist} />
        <CatalystsCard catalysts={catalysts} watchlist={watchlist} />
      </div>

      <TriageFeedCard status={status} watchlist={watchlist} />
    </div>
  );
}

function StatusGrid({ status }: { status: WorkerStatus }) {
  const failures = status.triage_failures + status.thesis_failures + status.persistence_failures;
  return (
    <div className="grid shrink-0 grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-8">
      <StatCell
        label="state"
        value={status.state}
        tone={status.state === 'running' ? 'accent' : 'default'}
      />
      <StatCell label="events" value={status.events_consumed.toLocaleString()} />
      <StatCell label="triaged" value={status.events_passed_triage.toLocaleString()} />
      <StatCell
        label="theses"
        value={status.theses_produced.toLocaleString()}
        tone={status.theses_produced > 0 ? 'accent' : 'default'}
      />
      <StatCell label="polls" value={status.polls_completed.toLocaleString()} />
      <StatCell
        label="failures"
        value={failures}
        tone={failures > 0 ? 'error' : 'default'}
        hint={`triage ${status.triage_failures} · thesis ${status.thesis_failures} · persist ${status.persistence_failures}`}
      />
      <StatCell label="last poll" value={fmtHMS(status.last_poll_at)} />
      <StatCell
        label="models"
        value={`${status.triage_model.split('-')[0]} / ${status.thesis_model}`}
        hint={`triage: ${status.triage_provider}/${status.triage_model} · thesis: ${status.thesis_provider}/${status.thesis_model}`}
      />
      {status.last_error && (
        <div className="col-span-full">
          <StatCell label="last error" value={status.last_error} tone="error" />
        </div>
      )}
    </div>
  );
}

function ReactiveThesesCard({
  theses,
  watchlist,
}: {
  theses: RecentThesis[];
  watchlist: string[];
}) {
  const modal = useModal();
  return (
    <section className="border-border bg-bg-elevated flex min-h-48 flex-col overflow-hidden rounded border">
      <header className="border-border bg-bg-elevated-2 flex items-baseline justify-between border-b px-3 py-1.5">
        <h3 className="text-text-muted text-[10px] font-semibold tracking-[0.12em] uppercase">
          Autonomous theses
        </h3>
        <span className="text-text-dim text-[10px] tabular-nums">{theses.length} recent</span>
      </header>
      {theses.length === 0 ? (
        <EmptyState
          title="None yet."
          hint="Theses land as material news arrives — usually a few per trading day, more in earnings season."
        />
      ) : (
        <ul className="divide-border min-h-0 flex-1 divide-y overflow-auto">
          {sortByWatchlist(theses, (t) => t.symbol, watchlist).map((t) => (
            <li
              key={t.id}
              onClick={() =>
                modal.open({
                  title: `Thesis · ${t.symbol}`,
                  content: <RecentThesisModal recent={t} />,
                })
              }
              className="hover:bg-bg-elevated-2 cursor-pointer px-3 py-2 text-[11px]"
            >
              <div className="flex items-baseline gap-1.5">
                {isOnWatchlist(t.symbol, watchlist) && <Star />}
                <span className="text-text-dim font-mono text-[9px] tabular-nums">
                  {fmtHM(t.generated_at)}
                </span>
                <Badge tone={bucketTone(t.source_bucket)}>{t.source_bucket}</Badge>
                <span className="text-text font-mono font-semibold">{t.symbol}</span>
                <span
                  className={`font-mono font-semibold ${t.direction === 'long' ? 'text-up' : 'text-down'}`}
                >
                  {t.direction.toUpperCase()} {(t.confidence * 100).toFixed(0)}%
                </span>
                <Badge tone={t.grounding_check_passed ? 'up' : 'warn'}>
                  {t.grounding_check_passed ? 'grounded' : 'ungrounded'}
                </Badge>
              </div>
              <p className="text-text-muted mt-1 line-clamp-2 text-[10px] leading-relaxed">
                {t.reasoning}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function CatalystsCard({
  catalysts,
  watchlist,
}: {
  catalysts: UpcomingCatalyst[];
  watchlist: string[];
}) {
  return (
    <section className="border-border bg-bg-elevated flex min-h-48 flex-col overflow-hidden rounded border">
      <header className="border-border bg-bg-elevated-2 flex items-baseline justify-between border-b px-3 py-1.5">
        <h3 className="text-text-muted text-[10px] font-semibold tracking-[0.12em] uppercase">
          Upcoming catalysts · 14d
        </h3>
        <span className="text-text-dim text-[10px]">
          scheduled events the agent watches — not trades; a thesis fires 2 days before each
        </span>
      </header>
      {catalysts.length === 0 ? (
        <EmptyState
          title="No catalysts in the next 14 days."
          hint="Earnings dates refresh from Finnhub every 6 hours."
        />
      ) : (
        <div className="min-h-0 flex-1 overflow-auto">
          <table className="w-full text-[11px] tabular-nums">
            <thead className="bg-bg-elevated sticky top-0">
              <tr className="text-text-dim border-border border-b text-left text-[9px] tracking-wider uppercase">
                <th className="px-3 py-1.5">Symbol</th>
                <th className="px-2 py-1.5">Type</th>
                <th className="px-2 py-1.5">When</th>
                <th className="px-2 py-1.5 text-right">Est EPS</th>
                <th className="px-2 py-1.5">State</th>
                <th className="px-2 py-1.5">Thesis</th>
              </tr>
            </thead>
            <tbody>
              {sortByWatchlist(catalysts, (c) => c.symbol, watchlist).map((c) => (
                <tr
                  key={c.id}
                  className="border-border hover:bg-bg-elevated-2 border-b last:border-b-0"
                >
                  <td className="px-3 py-1.5">
                    <span className="flex items-baseline gap-1">
                      {isOnWatchlist(c.symbol, watchlist) && <Star />}
                      <span className="text-text font-mono font-semibold">{c.symbol}</span>
                    </span>
                  </td>
                  <td className="text-text-muted px-2 py-1.5">{c.event_type}</td>
                  <td className="text-text-muted px-2 py-1.5" title={c.event_date}>
                    {fmtWhen(c.days_until, c.event_hour)}
                  </td>
                  <td className="text-text-muted px-2 py-1.5 text-right">
                    {c.estimated_eps ?? '—'}
                  </td>
                  <td className="px-2 py-1.5">
                    <Badge
                      tone={
                        c.state === 'triggered'
                          ? 'accent'
                          : c.state === 'expired'
                            ? 'neutral'
                            : 'warn'
                      }
                    >
                      {c.state}
                    </Badge>
                  </td>
                  <td className="text-text-dim px-2 py-1.5">
                    {c.thesis_id !== null ? `#${c.thesis_id}` : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function TriageFeedCard({
  status,
  watchlist,
}: {
  status: WorkerStatus | null;
  watchlist: string[];
}) {
  const decisions = [...(status?.recent_triage_decisions ?? [])].reverse();
  return (
    <section className="border-border bg-bg-elevated flex min-h-40 shrink-0 flex-col overflow-hidden rounded border">
      <header className="border-border bg-bg-elevated-2 flex items-baseline justify-between border-b px-3 py-1.5">
        <h3 className="text-text-muted text-[10px] font-semibold tracking-[0.12em] uppercase">
          Triage feed
        </h3>
        <span className="text-text-dim text-[10px] tabular-nums">
          {decisions.length} recent decisions
        </span>
      </header>
      {decisions.length === 0 ? (
        <EmptyState title="Watching for material events…" />
      ) : (
        <ul className="divide-border min-h-0 flex-1 divide-y overflow-auto">
          {sortByWatchlist(decisions, (d) => d.symbol, watchlist)
            .slice(0, 20)
            .map((d) => (
              <li key={d.event_id} className="flex items-baseline gap-1.5 px-3 py-1.5 text-[11px]">
                {isOnWatchlist(d.symbol, watchlist) && <Star />}
                <span className="text-text-dim font-mono text-[9px] tabular-nums">
                  {fmtHM(d.at)}
                </span>
                <Badge tone={d.passed ? 'up' : 'neutral'}>{d.passed ? 'PASS' : 'DROP'}</Badge>
                <span className="text-text shrink-0 font-mono font-semibold">
                  {d.symbol ?? '—'}
                </span>
                <span className="text-text-muted truncate">{d.headline}</span>
                <span className="text-text-dim hidden truncate text-[10px] italic lg:inline">
                  — {d.reason}
                </span>
              </li>
            ))}
        </ul>
      )}
    </section>
  );
}

function Star() {
  return (
    <span className="text-accent text-[10px]" title="On your watchlist">
      ★
    </span>
  );
}

function fmtWhen(daysUntil: number, hour: string | null): string {
  const hourLabel = hour === 'bmo' ? ' BMO' : hour === 'amc' ? ' AMC' : hour === 'dmh' ? ' IH' : '';
  if (daysUntil < 0) return `${Math.abs(daysUntil)}d ago${hourLabel}`;
  if (daysUntil === 0) return `today${hourLabel}`;
  if (daysUntil === 1) return `tomorrow${hourLabel}`;
  return `in ${daysUntil}d${hourLabel}`;
}

function isOnWatchlist(symbol: string | null | undefined, watchlist: string[]): boolean {
  if (!symbol) return false;
  return watchlist.includes(symbol.toUpperCase());
}

function sortByWatchlist<T>(
  items: T[],
  getSymbol: (item: T) => string | null | undefined,
  watchlist: string[],
): T[] {
  // Stable sort: watchlist items first, preserving relative order within each bucket.
  const watch: T[] = [];
  const other: T[] = [];
  for (const item of items) {
    if (isOnWatchlist(getSymbol(item), watchlist)) {
      watch.push(item);
    } else {
      other.push(item);
    }
  }
  return [...watch, ...other];
}
