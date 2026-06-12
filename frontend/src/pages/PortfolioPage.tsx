import { useCallback, useEffect, useState } from 'react';

import { EquityCurveChart } from '../EquityCurveChart';
import {
  getPaperAccounts,
  getShadowTrades,
  type AccountSummary,
  type ShadowTradeRow,
} from '../lib/api';
import { fmtDateTimeSmart, fmtPct, fmtUsd, relTimeIso } from '../lib/format';
import { Badge } from '../ui/Badge';
import { EmptyState } from '../ui/EmptyState';

/**
 * Portfolio — the two paper accounts side by side, the equity-curve A/B,
 * then the answer to "what is the agent holding right now, and how did its
 * past trades go": Open Positions (live marks) + Trade History (realized).
 * The conservative/aggressive contrast is the experiment: same theses,
 * different deterministic risk gates.
 */
export function PortfolioPage() {
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [trades, setTrades] = useState<ShadowTradeRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [a, t] = await Promise.all([getPaperAccounts(), getShadowTrades(undefined, 30)]);
      setAccounts(a);
      setTrades(t);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const interval = setInterval(() => void refresh(), 10000);
    return () => clearInterval(interval);
  }, [refresh]);

  return (
    <div className="flex h-full min-h-0 flex-col gap-2 overflow-auto p-2">
      <header className="border-border bg-bg-elevated flex shrink-0 flex-wrap items-baseline justify-between gap-2 rounded border px-3 py-2">
        <h2 className="text-text text-xs font-semibold tracking-[0.12em] uppercase">
          Paper portfolio · shadow mode
        </h2>
        <p className="text-text-dim text-[10px]">
          every persisted thesis runs both accounts&apos; risk gates — no real money, ever
        </p>
      </header>

      {error && (
        <div
          className="border-error/40 bg-error/10 text-error rounded border px-3 py-2 text-xs"
          role="alert"
        >
          {error}
        </div>
      )}

      <div className="grid shrink-0 gap-2 md:grid-cols-2">
        {accounts.map((a) => (
          <AccountCard key={a.id} account={a} />
        ))}
      </div>

      <EquityCurveChart />

      <OpenPositionsSection trades={trades.filter((t) => t.status === 'shadow_open')} />
      <TradeHistorySection trades={trades.filter((t) => t.status !== 'shadow_open')} />
    </div>
  );
}

/** What the agent is holding RIGHT NOW, marked to market every 5 minutes. */
function OpenPositionsSection({ trades }: { trades: ShadowTradeRow[] }) {
  return (
    <section className="border-border bg-bg-elevated shrink-0 overflow-hidden rounded border">
      <header className="border-border bg-bg-elevated-2 flex items-baseline justify-between border-b px-3 py-1.5">
        <h3 className="text-text-muted text-[10px] font-semibold tracking-[0.12em] uppercase">
          Open positions
        </h3>
        <span className="text-text-dim text-[10px] tabular-nums">
          {trades.length} open · marked to market every 5 min
        </span>
      </header>
      {trades.length === 0 ? (
        <EmptyState
          title="Nothing open right now."
          hint="A position opens when a thesis passes a risk gate — conservative needs confidence ≥ 0.70, aggressive ≥ 0.50."
        />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-[11px] tabular-nums">
            <thead>
              <tr className="text-text-dim border-border border-b text-left text-[9px] tracking-wider uppercase">
                <Th>Acct</Th>
                <Th>Opened</Th>
                <Th>Contract</Th>
                <Th>Side</Th>
                <Th>Expiry</Th>
                <Th right>Qty</Th>
                <Th right>Cost basis</Th>
                <Th right>Live P&L</Th>
                <Th>Marked</Th>
                <Th>Thesis</Th>
              </tr>
            </thead>
            <tbody>
              {trades.map((t) => (
                <OpenPositionRow key={t.id} trade={t} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function OpenPositionRow({ trade }: { trade: ShadowTradeRow }) {
  const cost = Number(trade.total_cost_usd);
  const unreal = trade.unrealized_pnl_usd !== null ? Number(trade.unrealized_pnl_usd) : null;
  const unrealPct = unreal !== null && cost > 0 ? (unreal / cost) * 100 : null;
  const tone =
    unreal === null
      ? 'text-text-dim'
      : unreal > 0
        ? 'text-up'
        : unreal < 0
          ? 'text-down'
          : 'text-text-muted';
  return (
    <tr
      className="border-border hover:bg-bg-elevated-2 border-b transition-colors last:border-b-0"
      title={trade.risk_reason}
    >
      <Td>
        <Badge tone={trade.account_kind === 'aggressive' ? 'accent' : 'info'}>
          {trade.account_kind.slice(0, 4)}
        </Badge>
      </Td>
      <Td title={trade.opened_at}>{fmtDateTimeSmart(trade.opened_at)}</Td>
      <Td>
        <span className="text-text font-semibold">{trade.underlying}</span>{' '}
        <code className="text-text-dim text-[9px]" title={trade.occ_symbol}>
          {trade.occ_symbol}
        </code>
      </Td>
      <Td>
        <Badge tone={trade.option_type === 'call' ? 'up' : 'down'}>long {trade.option_type}</Badge>
      </Td>
      <Td>{trade.expiration}</Td>
      <Td right>{trade.contracts}</Td>
      <Td right>${trade.total_cost_usd}</Td>
      <Td right>
        {unreal === null ? (
          <span className="text-text-dim" title="No mark yet — the MTM job runs every 5 minutes">
            awaiting mark
          </span>
        ) : (
          <span className={`font-semibold ${tone}`}>
            {unreal >= 0 ? '+' : ''}
            {fmtUsd(unreal)}
            {unrealPct !== null && <span className="text-[10px]"> ({fmtPct(unrealPct)})</span>}
          </span>
        )}
      </Td>
      <Td>{trade.marked_at ? relTimeIso(trade.marked_at) : '—'}</Td>
      <Td>
        <span className="text-text-dim">#{trade.thesis_id}</span>
      </Td>
    </tr>
  );
}

/** Closed trades — what actually happened, graded in dollars. */
function TradeHistorySection({ trades }: { trades: ShadowTradeRow[] }) {
  return (
    <section className="border-border bg-bg-elevated shrink-0 overflow-hidden rounded border">
      <header className="border-border bg-bg-elevated-2 flex items-baseline justify-between border-b px-3 py-1.5">
        <h3 className="text-text-muted text-[10px] font-semibold tracking-[0.12em] uppercase">
          Trade history
        </h3>
        <span className="text-text-dim text-[10px] tabular-nums">{trades.length} closed</span>
      </header>
      {trades.length === 0 ? (
        <EmptyState
          title="No closed trades yet."
          hint="Positions close at expiration or when an exit rule trips; realized P&L lands here."
        />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-[11px] tabular-nums">
            <thead>
              <tr className="text-text-dim border-border border-b text-left text-[9px] tracking-wider uppercase">
                <Th>Acct</Th>
                <Th>Opened</Th>
                <Th>Closed</Th>
                <Th>Contract</Th>
                <Th>Side</Th>
                <Th right>Cost basis</Th>
                <Th right>Realized P&L</Th>
                <Th>Why closed</Th>
                <Th>Thesis</Th>
              </tr>
            </thead>
            <tbody>
              {trades.map((t) => (
                <HistoryRow key={t.id} trade={t} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function HistoryRow({ trade }: { trade: ShadowTradeRow }) {
  const cost = Number(trade.total_cost_usd);
  const realized = trade.realized_pnl_usd !== null ? Number(trade.realized_pnl_usd) : null;
  const realizedPct = realized !== null && cost > 0 ? (realized / cost) * 100 : null;
  const tone =
    realized === null
      ? 'text-text-dim'
      : realized > 0
        ? 'text-up'
        : realized < 0
          ? 'text-down'
          : 'text-text-muted';
  return (
    <tr
      className="border-border hover:bg-bg-elevated-2 border-b transition-colors last:border-b-0"
      title={trade.risk_reason}
    >
      <Td>
        <Badge tone={trade.account_kind === 'aggressive' ? 'accent' : 'info'}>
          {trade.account_kind.slice(0, 4)}
        </Badge>
      </Td>
      <Td title={trade.opened_at}>{fmtDateTimeSmart(trade.opened_at)}</Td>
      <Td title={trade.closed_at ?? undefined}>
        {trade.closed_at ? fmtDateTimeSmart(trade.closed_at) : '—'}
      </Td>
      <Td>
        <span className="text-text font-semibold">{trade.underlying}</span>{' '}
        <code className="text-text-dim text-[9px]" title={trade.occ_symbol}>
          {trade.occ_symbol}
        </code>
      </Td>
      <Td>
        <Badge tone={trade.option_type === 'call' ? 'up' : 'down'}>long {trade.option_type}</Badge>
      </Td>
      <Td right>${trade.total_cost_usd}</Td>
      <Td right>
        {realized === null ? (
          '—'
        ) : (
          <span className={`font-semibold ${tone}`}>
            {realized >= 0 ? '+' : ''}
            {fmtUsd(realized)}
            {realizedPct !== null && <span className="text-[10px]"> ({fmtPct(realizedPct)})</span>}
          </span>
        )}
      </Td>
      <Td>
        <span className="text-text-dim">{trade.close_reason?.replace(/_/g, ' ') ?? '—'}</span>
      </Td>
      <Td>
        <span className="text-text-dim">#{trade.thesis_id}</span>
      </Td>
    </tr>
  );
}

function AccountCard({ account }: { account: AccountSummary }) {
  const equity = Number(account.equity_usd);
  const start = Number(account.starting_balance_usd);
  const pnl = equity - start;
  const pnlPct = start > 0 ? (pnl / start) * 100 : 0;
  const pnlTone = pnl > 0 ? 'text-up' : pnl < 0 ? 'text-down' : 'text-text-muted';
  const isAgg = account.kind === 'aggressive';

  return (
    <article
      className={`bg-bg-elevated rounded border p-3 ${isAgg ? 'border-accent/30' : 'border-info/30'}`}
    >
      <header className="mb-2 flex items-center justify-between">
        <h3 className="text-text text-xs font-semibold tracking-wide">{account.name}</h3>
        <Badge tone={isAgg ? 'accent' : 'info'}>{account.kind}</Badge>
      </header>

      <div className="mb-3 flex items-baseline gap-4">
        <div>
          <div className="text-text-dim text-[9px] tracking-wider uppercase">Equity</div>
          <div className="text-text font-mono text-lg font-semibold tabular-nums">
            {fmtUsd(equity)}
          </div>
        </div>
        <div>
          <div className="text-text-dim text-[9px] tracking-wider uppercase">P&L (shadow)</div>
          <div className={`font-mono text-lg font-semibold tabular-nums ${pnlTone}`}>
            {pnl >= 0 ? '+' : ''}
            {fmtUsd(pnl)} <span className="text-xs">({fmtPct(pnlPct)})</span>
          </div>
        </div>
        {account.kill_switch && (
          <span className="ml-auto">
            <Badge tone="error">kill switch ON</Badge>
          </span>
        )}
      </div>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-[10px] tabular-nums sm:grid-cols-3">
        <ConfigStat label="min conf" value={account.min_confidence.toFixed(2)} />
        <ConfigStat label="max / trade" value={`$${account.max_trade_cost_usd}`} />
        <ConfigStat
          label="trades / day"
          value={`${account.shadow_trades_today} / ${account.max_trades_per_day}`}
        />
        <ConfigStat
          label="open"
          value={`${account.open_shadow_positions} / ${account.max_concurrent_positions}`}
        />
        <ConfigStat label="total trades" value={String(account.shadow_trades_total)} />
        <ConfigStat label="cost open" value={`$${account.total_cost_open_usd}`} />
      </dl>
    </article>
  );
}

function ConfigStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <dt className="text-text-dim tracking-wider uppercase">{label}</dt>
      <dd className="text-text-muted m-0 font-mono">{value}</dd>
    </div>
  );
}

function Th({ children, right }: { children: React.ReactNode; right?: boolean }) {
  return <th className={`px-2 py-1.5 ${right ? 'text-right' : 'text-left'}`}>{children}</th>;
}

function Td({
  children,
  right,
  title,
}: {
  children: React.ReactNode;
  right?: boolean;
  title?: string;
}) {
  return (
    <td className={`text-text-muted px-2 py-1.5 ${right ? 'text-right' : ''}`} title={title}>
      {children}
    </td>
  );
}
