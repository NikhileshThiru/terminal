import { PayoffDiagram } from './PayoffDiagram';
import type { Thesis } from './lib/api';
import { Badge, directionTone } from './ui/Badge';

interface Props {
  thesis: Thesis;
  /** Optional current underlying price (drawn on the payoff diagram). */
  currentUnderlying?: number | null;
}

export function ThesisDisplay({ thesis, currentUnderlying = null }: Props) {
  const c = thesis.suggested_contract;
  const confPct = Math.round(thesis.confidence * 100);
  const dirColor = thesis.direction === 'long' ? 'text-up' : 'text-down';

  return (
    <article className="animate-fade-in space-y-3 font-mono">
      {/* Hero: symbol + direction + confidence | trade line */}
      <header className="border-border bg-bg-elevated-2 flex flex-wrap items-center justify-between gap-3 rounded border px-4 py-3">
        <div className="flex items-center gap-3">
          <span className="text-text text-2xl font-bold tracking-wider">{thesis.symbol}</span>
          <span className={`text-lg font-bold tracking-wider ${dirColor}`}>
            {thesis.direction.toUpperCase()}
          </span>
          <div className="flex flex-col gap-0.5">
            <span className="text-text text-sm font-semibold tabular-nums">
              {confPct}% <span className="text-text-dim text-[9px] uppercase">confidence</span>
            </span>
            <ConfidenceBar value={thesis.confidence} />
          </div>
        </div>
        <div className="text-right">
          <div className="text-text-dim text-[9px] tracking-wider uppercase">Trade</div>
          <code className="text-accent text-sm font-semibold">{c.occ_symbol}</code>
          <div className="text-text-muted text-[11px] tabular-nums">
            ${c.estimated_premium_per_contract} × {c.contracts} ={' '}
            <span className="text-down">${c.max_risk_usd} at risk</span>
          </div>
        </div>
      </header>

      {/* Payoff diagram front and center */}
      <section className="border-border bg-bg-elevated rounded border p-4">
        <PayoffDiagram contract={c} currentUnderlying={currentUnderlying} />
      </section>

      <section className="grid gap-3 sm:grid-cols-2">
        <Card title="Contract">
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-[11px] tabular-nums">
            <Dt>Type / Strike</Dt>
            <Dd>
              {c.option_type} · ${c.strike}
            </Dd>
            <Dt>Expiration</Dt>
            <Dd>{c.expiration}</Dd>
            <Dt>Premium / contract</Dt>
            <Dd>${c.estimated_premium_per_contract}</Dd>
            <Dt>Contracts</Dt>
            <Dd>{c.contracts}</Dd>
            <Dt>Max risk (USD)</Dt>
            <Dd>
              <span className="text-down font-semibold">${c.max_risk_usd}</span>
            </Dd>
          </dl>
        </Card>
        <Card title="What must happen">
          <p className="text-text text-[11px] leading-relaxed">{thesis.what_must_happen}</p>
          <p className="text-text-dim mt-2 text-[10px]">
            Evaluation window · {thesis.prediction_window_days} days
          </p>
        </Card>
      </section>

      <Card title="Reasoning">
        <p className="text-text-muted text-[11px] leading-relaxed">{thesis.reasoning}</p>
      </Card>

      <footer className="flex flex-wrap items-center gap-2 text-[10px]">
        <Badge tone={thesis.grounding_check_passed ? 'up' : 'warn'}>
          {thesis.grounding_check_passed ? 'grounded · verified' : 'has unverified figures'}
        </Badge>
        <Badge tone={directionTone(thesis.direction)}>{thesis.direction}</Badge>
        {thesis.grounding_notes && <span className="text-text-dim">{thesis.grounding_notes}</span>}
        <span className="text-text-dim ml-auto tabular-nums">
          {thesis.llm_provider}/{thesis.llm_model} · {thesis.funnel_latency_ms} ms · corr{' '}
          <code>{thesis.correlation_id.slice(0, 8)}</code>
        </span>
      </footer>
    </article>
  );
}

function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.max(0, Math.min(1, value)) * 100;
  return (
    <span className="bg-bg block h-1 w-24 overflow-hidden rounded">
      <span className="bg-accent block h-full rounded" style={{ width: `${pct}%` }} />
    </span>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border-border bg-bg-elevated rounded border p-3">
      <h3 className="text-text-dim mb-2 text-[9px] font-semibold tracking-wider uppercase">
        {title}
      </h3>
      {children}
    </div>
  );
}

function Dt({ children }: { children: React.ReactNode }) {
  return <dt className="text-text-dim">{children}</dt>;
}

function Dd({ children }: { children: React.ReactNode }) {
  return <dd className="text-text m-0">{children}</dd>;
}
