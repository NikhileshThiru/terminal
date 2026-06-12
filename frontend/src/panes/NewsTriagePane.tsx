import { useCallback, useEffect, useState } from 'react';

import { useNavigation } from '../chrome/NavigationContext';
import { Pane } from '../chrome/Pane';
import { useSelection } from '../chrome/SelectionContext';
import {
  getAutonomousStatus,
  injectSyntheticEvent,
  type TriageDecisionRecord,
  type WorkerStatus,
} from '../lib/api';
import { Badge } from '../ui/Badge';

/**
 * Recent triage decisions from the autonomous worker, plus a tiny
 * injector form for firing synthetic events. The injector is what
 * lets the demo populate the eval harness without waiting for real
 * 8-Ks to roll in — and it's how I'll test the funnel locally.
 */
export function NewsTriagePane() {
  const { setSelectedSymbol, selectedSymbol } = useSelection();
  const { page, setPage } = useNavigation();
  const [status, setStatus] = useState<WorkerStatus | null>(null);
  const [injectOpen, setInjectOpen] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const s = await getAutonomousStatus();
      setStatus(s);
    } catch {
      /* worker may not be initialised; non-fatal */
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = setInterval(() => void refresh(), 10000);
    return () => clearInterval(t);
  }, [refresh]);

  const decisions: TriageDecisionRecord[] = [...(status?.recent_triage_decisions ?? [])].reverse();
  const isRunning = status?.state === 'running';

  return (
    <Pane title="News / Triage">
      <div className="flex h-full flex-col">
        <div className="border-border flex shrink-0 items-center justify-between gap-2 border-b px-2 py-1">
          <span className="text-text-dim text-[10px]">
            {decisions.length} recent decisions · {isRunning ? 'live' : 'paused'}
          </span>
          <button
            type="button"
            onClick={() => setInjectOpen((x) => !x)}
            disabled={!isRunning}
            className="border-border-strong text-text-muted hover:text-text hover:border-text-muted cursor-pointer rounded border px-1.5 py-0.5 font-mono text-[9px] tracking-wider uppercase disabled:cursor-not-allowed disabled:opacity-50"
            title={isRunning ? 'Inject a synthetic event' : 'Start autonomous mode first'}
          >
            {injectOpen ? '× close' : '+ inject'}
          </button>
        </div>

        {injectOpen && (
          <InjectForm
            initialSymbol={selectedSymbol}
            onDone={() => {
              void refresh();
            }}
          />
        )}

        <div className="min-h-0 flex-1 overflow-auto">
          {decisions.length === 0 ? (
            <p className="text-text-dim p-2 text-xs italic">
              {isRunning
                ? 'Watching for material events…'
                : 'Autonomous mode is stopped. Start it from the bottom strip.'}
            </p>
          ) : (
            <ul className="divide-border divide-y text-[11px]">
              {decisions.slice(0, 25).map((d) => (
                <TriageRow
                  key={d.event_id}
                  decision={d}
                  expanded={expandedId === d.event_id}
                  onClick={() => {
                    // Two effects on click: focus the symbol (chart + ticker
                    // info follow) and toggle the row expansion (body + reason
                    // visible inline).
                    if (d.symbol) {
                      setSelectedSymbol(d.symbol);
                      // Jump to Dashboard if the user is somewhere the focus
                      // wouldn't be visible.
                      if (page !== 'dashboard') {
                        setPage('dashboard');
                      }
                    }
                    setExpandedId((cur) => (cur === d.event_id ? null : d.event_id));
                  }}
                />
              ))}
            </ul>
          )}
        </div>
      </div>
    </Pane>
  );
}

function TriageRow({
  decision,
  expanded,
  onClick,
}: {
  decision: TriageDecisionRecord;
  expanded: boolean;
  onClick: () => void;
}) {
  return (
    <li
      className={`cursor-pointer px-2 py-1.5 transition-colors ${
        expanded ? 'bg-bg-elevated-2' : 'hover:bg-bg-elevated-2'
      }`}
      onClick={onClick}
    >
      <div
        className="grid items-baseline gap-1.5"
        style={{ gridTemplateColumns: 'auto auto auto 1fr auto' }}
      >
        <span className="text-text-dim font-mono text-[9px] tabular-nums">
          {formatHM(decision.at)}
        </span>
        <Badge tone={decision.passed ? 'up' : 'neutral'}>{decision.passed ? 'PASS' : 'DROP'}</Badge>
        <SourceBadge source={decision.source} kind={decision.kind} />
        <span className="flex items-baseline gap-1.5 overflow-hidden">
          <span className="text-text shrink-0 font-mono font-semibold">
            {decision.symbol ?? '—'}
          </span>
          <span className="text-text-muted truncate">{decision.headline}</span>
        </span>
        {decision.url && (
          <a
            href={decision.url}
            target="_blank"
            rel="noreferrer noopener"
            onClick={(e) => e.stopPropagation()}
            className="text-text-dim hover:text-accent shrink-0 font-mono text-[10px]"
            title="Open original article"
          >
            ↗
          </a>
        )}
      </div>
      {expanded && (
        <div className="mt-1.5 space-y-1.5 pl-12 text-[10px]">
          {decision.body_excerpt && (
            <p className="text-text-muted leading-relaxed">{decision.body_excerpt}</p>
          )}
          <p className="text-text-dim italic">
            <span className="text-text-dim/80 mr-1 tracking-wider uppercase">triage:</span>
            {decision.reason}{' '}
            <span className="text-text-dim/70">
              · {(decision.confidence * 100).toFixed(0)}% conf
            </span>
          </p>
        </div>
      )}
    </li>
  );
}

function SourceBadge({
  source,
  kind,
}: {
  source: TriageDecisionRecord['source'];
  kind: TriageDecisionRecord['kind'];
}) {
  const label =
    source === 'edgar'
      ? 'EDGR'
      : source === 'alpaca-news'
        ? 'NEWS'
        : source === 'rss'
          ? 'RSS'
          : source === 'flag-scanner'
            ? 'FLAG'
            : kind
              ? kind.slice(0, 4).toUpperCase()
              : '—';
  return (
    <span
      className="border-border text-text-dim rounded border px-1 py-0 font-mono text-[9px] tracking-wider uppercase"
      title={`${source ?? 'unknown'} / ${kind ?? 'unknown'}`}
    >
      {label}
    </span>
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

function InjectForm({ initialSymbol, onDone }: { initialSymbol: string; onDone: () => void }) {
  const [symbol, setSymbol] = useState(initialSymbol);
  const [headline, setHeadline] = useState('');
  const [kind, setKind] = useState<'news' | 'filing'>('news');
  const [state, setState] = useState<'idle' | 'submitting' | 'ok' | 'error'>('idle');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  async function submit() {
    if (!symbol.trim() || headline.trim().length < 5) return;
    setState('submitting');
    setErrorMsg(null);
    try {
      await injectSyntheticEvent({
        symbol: symbol.trim().toUpperCase(),
        headline: headline.trim(),
        kind,
      });
      setState('ok');
      setHeadline('');
      onDone();
      setTimeout(() => setState('idle'), 2000);
    } catch (e) {
      setState('error');
      setErrorMsg(e instanceof Error ? e.message : String(e));
    }
  }

  const samples = [
    {
      label: 'earnings beat',
      headline: `${symbol} beats Q2 earnings, raises full-year guidance`,
    },
    {
      label: '8-K filing',
      headline: `${symbol} files 8-K disclosing CFO departure`,
    },
    {
      label: 'analyst upgrade',
      headline: `${symbol} upgraded to Buy at major bank, PT raised`,
    },
  ];

  return (
    <div className="border-border bg-bg-elevated-2 flex shrink-0 flex-col gap-1.5 border-b px-2 py-2 text-[11px]">
      <div className="flex items-center gap-1.5">
        <input
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          placeholder="SYM"
          maxLength={8}
          className="bg-bg border-border-strong text-text w-16 rounded border px-1.5 py-1 font-mono uppercase"
        />
        <select
          value={kind}
          onChange={(e) => setKind(e.target.value as 'news' | 'filing')}
          className="bg-bg border-border-strong text-text rounded border px-1 py-1 font-mono"
        >
          <option value="news">news</option>
          <option value="filing">filing</option>
        </select>
        <button
          type="button"
          onClick={() => void submit()}
          disabled={state === 'submitting' || headline.trim().length < 5}
          className="bg-accent ml-auto cursor-pointer rounded px-2 py-1 font-mono text-[10px] font-bold text-black disabled:cursor-not-allowed disabled:bg-zinc-800 disabled:text-zinc-500"
        >
          {state === 'submitting' ? '…' : state === 'ok' ? '✓ fired' : 'fire'}
        </button>
      </div>
      <input
        value={headline}
        onChange={(e) => setHeadline(e.target.value)}
        placeholder="headline (≥ 5 chars)"
        className="bg-bg border-border-strong text-text rounded border px-1.5 py-1 font-mono"
      />
      <div className="flex flex-wrap items-center gap-1">
        <span className="text-text-dim text-[9px] tracking-wider uppercase">presets:</span>
        {samples.map((s) => (
          <button
            key={s.label}
            type="button"
            onClick={() => setHeadline(s.headline)}
            className="text-text-muted hover:text-text border-border-strong cursor-pointer rounded border px-1 py-0.5 font-mono text-[9px]"
          >
            {s.label}
          </button>
        ))}
      </div>
      {state === 'error' && errorMsg && (
        <p className="text-error border-error/40 bg-error/10 rounded border px-1.5 py-1 font-mono text-[10px]">
          {errorMsg}
        </p>
      )}
    </div>
  );
}
