import { useEffect, useState } from 'react';

import {
  getAutonomousStatus,
  getCostSummary,
  getHealth,
  type CostSummary,
  type WorkerStatus,
} from '../lib/api';
import { useModal } from './ModalContext';

type HealthState = 'loading' | 'ok' | 'error';

/**
 * Top status bar. Sells the "always-on terminal" feel: market open/closed,
 * NYSE clock, autonomous state, model in use, notional LLM cost (click for
 * the per-model breakdown), backend health, and the keyboard hint.
 */
export function Header() {
  const modal = useModal();
  const [now, setNow] = useState<Date>(new Date());
  const [health, setHealth] = useState<HealthState>('loading');
  const [version, setVersion] = useState<string | null>(null);
  const [worker, setWorker] = useState<WorkerStatus | null>(null);
  const [cost, setCost] = useState<CostSummary | null>(null);

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    let cancelled = false;
    getHealth()
      .then((h) => {
        if (cancelled) return;
        setHealth('ok');
        setVersion(h.version);
      })
      .catch(() => !cancelled && setHealth('error'));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const refresh = () => {
      getAutonomousStatus()
        .then((s) => !cancelled && setWorker(s))
        .catch(() => {
          /* worker may not be initialised yet; non-fatal */
        });
      getCostSummary()
        .then((c) => !cancelled && setCost(c))
        .catch(() => {
          /* non-fatal */
        });
    };
    refresh();
    const t = setInterval(refresh, 15000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  const marketState = computeMarketState(now);

  return (
    <header className="bg-bg border-border flex shrink-0 items-center justify-between border-b px-4 py-2">
      <div className="flex items-baseline gap-3">
        <h1 className="text-text text-sm font-bold tracking-[0.08em] uppercase">
          Terminal<span className="text-accent">_</span>
        </h1>
        {version && <span className="text-text-dim text-[10px]">v{version}</span>}
      </div>

      <div className="flex items-center gap-2.5 text-[11px] tabular-nums">
        <Chip label="market" value={marketState.label} tone={marketState.open ? 'up' : 'muted'} />
        <Chip label="nyse" value={formatNyseTime(now)} tone="muted" mono />
        <Chip
          label="auto"
          value={worker?.state ?? '—'}
          tone={worker?.state === 'running' ? 'accent' : 'muted'}
          dot
        />
        <Chip
          label="thesis"
          value={worker?.thesis_model ?? '—'}
          tone="muted"
          title={worker?.thesis_provider ?? undefined}
        />
        <Chip
          label="llm"
          value={cost ? `${cost.calls}/${cost.daily_request_budget}` : '—'}
          tone={cost && cost.calls > cost.daily_request_budget * 0.8 ? 'error' : 'muted'}
          title={
            cost
              ? `LLM calls this backend session vs the free-tier daily budget — click for the per-model breakdown`
              : undefined
          }
          onClick={
            cost
              ? () =>
                  modal.open({
                    title: 'LLM usage (session)',
                    content: <CostBreakdownTable cost={cost} />,
                  })
              : undefined
          }
        />
        <Chip
          label="backend"
          value={health === 'loading' ? 'check…' : health === 'ok' ? 'ok' : 'offline'}
          tone={health === 'ok' ? 'up' : health === 'error' ? 'error' : 'muted'}
          dot
        />
      </div>
    </header>
  );
}

interface ChipProps {
  label: string;
  value: string;
  tone: 'up' | 'accent' | 'error' | 'muted';
  dot?: boolean;
  mono?: boolean;
  title?: string;
  onClick?: () => void;
}

function Chip({ label, value, tone, dot, mono, title, onClick }: ChipProps) {
  const toneClass =
    tone === 'up'
      ? 'text-up'
      : tone === 'accent'
        ? 'text-accent'
        : tone === 'error'
          ? 'text-error'
          : 'text-text-muted';
  const inner = (
    <>
      <span className="text-text-dim text-[9px] tracking-wider uppercase">{label}</span>
      {dot && <span className={`h-1.5 w-1.5 rounded-full ${dotBg(tone)}`} />}
      <span className={`${toneClass} ${mono ? 'font-mono' : ''}`}>{value}</span>
    </>
  );
  const base = 'border-border bg-bg-elevated flex items-center gap-1.5 rounded border px-2 py-1';
  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        className={`${base} hover:border-border-strong cursor-pointer transition-colors`}
        title={title}
        aria-label={`${label} ${value}`}
      >
        {inner}
      </button>
    );
  }
  return (
    <span className={base} title={title} aria-label={`${label} ${value}`}>
      {inner}
    </span>
  );
}

function dotBg(tone: 'up' | 'accent' | 'error' | 'muted'): string {
  if (tone === 'up') return 'bg-up';
  if (tone === 'accent') return 'bg-accent animate-pulse';
  if (tone === 'error') return 'bg-error';
  return 'bg-text-dim';
}

function CostBreakdownTable({ cost }: { cost: CostSummary }) {
  return (
    <div className="space-y-3 text-xs">
      <p className="text-text-muted leading-relaxed">
        Every key here is free-tier, so the scarce resource is <em>requests per day</em> (budget:{' '}
        {cost.daily_request_budget}, config-driven), not dollars. The cost column is notional — what
        these calls <em>would</em> bill at list price. Counters are per backend session and reset on
        restart.
      </p>
      <table className="w-full text-[11px] tabular-nums">
        <thead>
          <tr className="text-text-dim border-border border-b text-left text-[9px] tracking-wider uppercase">
            <th className="py-1.5 pr-3">Provider / model</th>
            <th className="py-1.5 pr-3 text-right">Calls</th>
            <th className="py-1.5 pr-3 text-right">Tokens in</th>
            <th className="py-1.5 pr-3 text-right">Tokens out</th>
            <th className="py-1.5 text-right">Cost</th>
          </tr>
        </thead>
        <tbody>
          {cost.by_model.map((m) => (
            <tr key={`${m.provider}/${m.model}`} className="border-border border-b last:border-b-0">
              <td className="text-text py-1.5 pr-3 font-mono">
                {m.provider}/{m.model}
              </td>
              <td className="text-text-muted py-1.5 pr-3 text-right">{m.calls}</td>
              <td className="text-text-muted py-1.5 pr-3 text-right">
                {m.input_tokens.toLocaleString()}
              </td>
              <td className="text-text-muted py-1.5 pr-3 text-right">
                {m.output_tokens.toLocaleString()}
              </td>
              <td className="text-accent py-1.5 text-right">${m.cost_usd.toFixed(4)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <td className="text-text py-1.5 pr-3 font-semibold">total</td>
            <td className="text-text py-1.5 pr-3 text-right">{cost.calls}</td>
            <td className="text-text py-1.5 pr-3 text-right">
              {cost.input_tokens.toLocaleString()}
            </td>
            <td className="text-text py-1.5 pr-3 text-right">
              {cost.output_tokens.toLocaleString()}
            </td>
            <td className="text-accent py-1.5 text-right font-semibold">
              ${cost.cost_usd.toFixed(4)}
            </td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

interface MarketState {
  open: boolean;
  label: string;
}

function computeMarketState(now: Date): MarketState {
  // NYSE hours 9:30–16:00 ET, Mon–Fri, via a proper America/New_York wall-clock.
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    hour: '2-digit',
    minute: '2-digit',
    weekday: 'short',
    hour12: false,
  });
  const parts = fmt.formatToParts(now);
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? '';
  const weekday = get('weekday');
  const minutes = Number(get('hour')) * 60 + Number(get('minute'));
  const isWeekday = !['Sat', 'Sun'].includes(weekday);
  const open = isWeekday && minutes >= 9 * 60 + 30 && minutes < 16 * 60;
  if (!isWeekday) return { open: false, label: 'closed (weekend)' };
  if (minutes < 4 * 60) return { open: false, label: 'closed' };
  if (minutes < 9 * 60 + 30) return { open: false, label: 'pre-market' };
  if (open) return { open: true, label: 'open' };
  if (minutes < 20 * 60) return { open: false, label: 'after-hours' };
  return { open: false, label: 'closed' };
}

function formatNyseTime(now: Date): string {
  return new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(now);
}
