import { useCallback, useEffect, useState } from 'react';

import { AgentActivityIndicator } from '../panes/AgentActivityIndicator';
import { ChartPane } from '../panes/ChartPane';
import { LiveThesesPane } from '../panes/LiveThesesPane';
import { NewsTriagePane } from '../panes/NewsTriagePane';
import { TickerInfoPane } from '../panes/TickerInfoPane';
import {
  getAutonomousStatus,
  getEvalSummary,
  getPaperAccounts,
  startAutonomous,
  stopAutonomous,
  type AccountSummary,
  type EvalSummary,
  type WorkerStatus,
} from '../lib/api';

/**
 * Dashboard — symbol-centric + autonomous-centric in one screen.
 *
 *   ┌──────────────────────────────────────────────────────┐
 *   │  AUTONOMOUS STATUS STRIP                              │
 *   ├──────────────────────┬───────────────────────────────┤
 *   │ CHART (symbol)       │ TICKER INFO (symbol)          │
 *   │                      │ • profile + 52w + next earn   │
 *   │                      │ • recent news for THIS symbol │
 *   ├──────────────────────┼───────────────────────────────┤
 *   │ LIVE THESES          │ NEWS / TRIAGE (universe)      │
 *   │ (autonomous output)  │ click → focus symbol + expand │
 *   ├──────────────────────┴───────────────────────────────┤
 *   │ AGENT ACTIVITY INDICATOR (→ Manual Copilot page)     │
 *   └──────────────────────────────────────────────────────┘
 *
 * Click any watchlist symbol, news row, or triage decision → the right
 * column refocuses (chart + ticker info + symbol news all follow).
 * Click the agent indicator → opens the Manual Copilot page where the
 * full streaming-reasoning view lives.
 */
export function Dashboard() {
  const [worker, setWorker] = useState<WorkerStatus | null>(null);
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [evalSummary, setEvalSummary] = useState<EvalSummary | null>(null);
  const [toggling, setToggling] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [w, a, e] = await Promise.all([
        getAutonomousStatus(),
        getPaperAccounts(),
        getEvalSummary(),
      ]);
      setWorker(w);
      setAccounts(a);
      setEvalSummary(e);
    } catch {
      /* non-fatal */
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = setInterval(() => void refresh(), 8000);
    return () => clearInterval(t);
  }, [refresh]);

  async function toggleAutonomous() {
    if (toggling) return;
    setToggling(true);
    try {
      const next = worker?.state === 'running' ? await stopAutonomous() : await startAutonomous();
      setWorker(next);
    } finally {
      setToggling(false);
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col gap-2 p-2">
      <AutonomousStatusStrip
        worker={worker}
        accounts={accounts}
        evalSummary={evalSummary}
        toggling={toggling}
        onToggle={() => void toggleAutonomous()}
      />
      <div
        className="grid min-h-0 flex-1 gap-2"
        style={{
          gridTemplateColumns: 'minmax(0, 1.4fr) minmax(0, 1fr)',
          gridTemplateRows: 'minmax(0, 1fr) minmax(0, 1fr)',
        }}
      >
        <div className="min-h-0">
          <ChartPane />
        </div>
        <div className="min-h-0">
          <TickerInfoPane />
        </div>
        <div className="min-h-0">
          <LiveThesesPane />
        </div>
        <div className="min-h-0">
          <NewsTriagePane />
        </div>
      </div>
      <AgentActivityIndicator />
    </div>
  );
}

interface StatusStripProps {
  worker: WorkerStatus | null;
  accounts: AccountSummary[];
  evalSummary: EvalSummary | null;
  toggling: boolean;
  onToggle: () => void;
}

function AutonomousStatusStrip({
  worker,
  accounts,
  evalSummary,
  toggling,
  onToggle,
}: StatusStripProps) {
  const running = worker?.state === 'running';
  const cons = accounts.find((a) => a.kind === 'conservative');
  const agg = accounts.find((a) => a.kind === 'aggressive');
  const totalResolved = evalSummary?.buckets.reduce((acc, b) => acc + b.count_resolved, 0) ?? 0;
  const totalTheses = evalSummary?.buckets.reduce((acc, b) => acc + b.count_theses, 0) ?? 0;
  const weightedHit = computeWeightedHit(evalSummary);
  const totalOpen = (cons?.open_shadow_positions ?? 0) + (agg?.open_shadow_positions ?? 0);

  return (
    <section className="border-border-strong bg-bg-elevated relative flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2 overflow-hidden rounded border px-3 py-2.5 text-[11px] tabular-nums">
      <div className="flex items-baseline gap-2">
        <button
          type="button"
          onClick={onToggle}
          disabled={toggling}
          className={`flex cursor-pointer items-center gap-2 rounded border px-2.5 py-1 font-mono text-[11px] uppercase ${
            running
              ? 'border-accent text-accent bg-accent/10 hover:bg-accent/15'
              : 'border-border-strong text-text-muted hover:text-text hover:border-text-muted'
          } disabled:opacity-50`}
        >
          <span
            className={`h-2 w-2 rounded-full ${
              running ? 'bg-accent animate-pulse' : 'bg-text-dim'
            }`}
          />
          {toggling ? 'toggling…' : running ? 'Autonomous live' : 'Autonomous paused'}
        </button>
      </div>

      <Sep />
      <StatGroup label="Funnel · session">
        <Stat label="evt" value={fmt(worker?.events_consumed)} />
        <Stat label="triaged" value={fmt(worker?.events_passed_triage)} />
        <Stat label="theses" value={fmt(worker?.theses_produced)} tone="accent" />
      </StatGroup>

      <Sep />
      <StatGroup label="Eval">
        <Stat label="logged" value={String(totalTheses)} />
        <Stat label="resolved" value={String(totalResolved)} />
        <Stat
          label="hit"
          value={weightedHit === null ? '—' : `${(weightedHit * 100).toFixed(0)}%`}
          tone={weightedHit === null ? undefined : weightedHit >= 0.5 ? 'up' : 'down'}
        />
      </StatGroup>

      <Sep />
      <StatGroup label="Accounts">
        <AccountStat label="cons" account={cons} />
        <AccountStat label="agg" account={agg} />
        <Stat label="open" value={String(totalOpen)} />
      </StatGroup>

      <Sep />
      <StatGroup label="Activity">
        <Stat
          label="last thesis"
          value={worker?.last_thesis_at ? formatRel(worker.last_thesis_at) : '—'}
        />
        <Stat
          label="last news"
          value={worker?.last_event_at ? formatRel(worker.last_event_at) : '—'}
        />
      </StatGroup>
    </section>
  );
}

function StatGroup({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="text-text-dim text-[9px] tracking-wider uppercase">{label}</span>
      <div className="flex items-baseline gap-1.5">{children}</div>
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: 'accent' | 'error' | 'up' | 'down';
}) {
  const toneClass =
    tone === 'accent'
      ? 'text-accent'
      : tone === 'error'
        ? 'text-error'
        : tone === 'up'
          ? 'text-up'
          : tone === 'down'
            ? 'text-down'
            : 'text-text';
  return (
    <span className="flex items-baseline gap-1 font-mono">
      <span className="text-text-dim text-[9px] tracking-wider uppercase">{label}</span>
      <span className={toneClass}>{value}</span>
    </span>
  );
}

function AccountStat({ label, account }: { label: string; account: AccountSummary | undefined }) {
  if (!account) {
    return <Stat label={label} value="—" />;
  }
  const equity = Number(account.equity_usd);
  const start = Number(account.starting_balance_usd);
  const pnl = equity - start;
  const pnlPct = start > 0 ? (pnl / start) * 100 : 0;
  const tone = pnl > 0 ? 'up' : pnl < 0 ? 'down' : undefined;
  const arrow = pnl > 0 ? '▲' : pnl < 0 ? '▼' : '·';
  return (
    <Stat
      label={label}
      value={`${arrow} ${pnl >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%`}
      tone={tone}
    />
  );
}

function Sep() {
  return <span className="bg-border h-6 w-px shrink-0" />;
}

function fmt(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—';
  return n.toLocaleString();
}

function formatRel(iso: string): string {
  const ms = Date.now() - Date.parse(iso);
  if (!Number.isFinite(ms) || ms < 0) return '—';
  const secs = Math.floor(ms / 1000);
  if (secs < 2) return 'now';
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  return `${Math.floor(hrs / 24)}d`;
}

function computeWeightedHit(summary: EvalSummary | null): number | null {
  if (!summary) return null;
  let n = 0;
  let hits = 0;
  for (const b of summary.buckets) {
    if (b.count_resolved > 0 && b.hit_rate !== null) {
      n += b.count_resolved;
      hits += b.hit_rate * b.count_resolved;
    }
  }
  if (n === 0) return null;
  return hits / n;
}
