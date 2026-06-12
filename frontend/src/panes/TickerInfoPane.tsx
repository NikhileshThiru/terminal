import { useEffect, useState } from 'react';

import { ChainView } from '../ChainView';
import { useModal } from '../chrome/ModalContext';
import { Pane } from '../chrome/Pane';
import { useSelection } from '../chrome/SelectionContext';
import { getTickerInfo, getTickerNews, type TickerInfo, type TickerNewsRow } from '../lib/api';
import { Badge } from '../ui/Badge';

/**
 * Symbol-centric context for the Dashboard's right column. Shows what the
 * selected ticker IS — company / ETF, sector, market cap, business summary,
 * next earnings date — plus a tight stream of recent triage decisions
 * filtered to just this symbol. Refreshes when selectedSymbol changes.
 *
 * Backend: /tickers/{symbol}/info (yfinance, 1hr cached) + /tickers/{symbol}/news
 * (filtered triage_decisions rows, includes both PASS and DROP for context).
 */
export function TickerInfoPane() {
  const { selectedSymbol } = useSelection();
  const modal = useModal();
  const [info, setInfo] = useState<TickerInfo | null>(null);
  const [news, setNews] = useState<TickerNewsRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setInfo(null);
    setNews([]);
    Promise.allSettled([getTickerInfo(selectedSymbol), getTickerNews(selectedSymbol, 10)])
      .then(([infoRes, newsRes]) => {
        if (cancelled) return;
        if (infoRes.status === 'fulfilled') {
          setInfo(infoRes.value);
        } else {
          setError(
            infoRes.reason instanceof Error ? infoRes.reason.message : String(infoRes.reason),
          );
        }
        if (newsRes.status === 'fulfilled') {
          setNews(newsRes.value.rows);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedSymbol]);

  return (
    <Pane
      title="Ticker info"
      subtitle={
        <span className="flex items-baseline gap-2">
          <span className="text-text font-mono font-semibold tracking-wider">{selectedSymbol}</span>
          <button
            type="button"
            onClick={() =>
              modal.open({
                title: `Options chain · ${selectedSymbol}`,
                content: <ChainView symbol={selectedSymbol} />,
              })
            }
            className="text-accent border-accent/40 bg-accent/10 hover:bg-accent/20 cursor-pointer rounded border px-2 py-0.5 font-mono text-[10px] font-semibold tracking-wider uppercase transition-colors"
            title="Browse the options chain the agent picks contracts from"
          >
            ≡ Options chain
          </button>
        </span>
      }
      bodyClassName="p-0"
    >
      <div className="flex h-full min-h-0 flex-col">
        <div className="border-border shrink-0 border-b p-3">
          {loading && !info ? (
            <p className="text-text-dim text-xs italic">loading ticker profile…</p>
          ) : info ? (
            <InfoBlock info={info} />
          ) : (
            <p className="text-text-dim text-xs italic">
              ticker profile unavailable{error ? ` · ${error}` : ''}
            </p>
          )}
        </div>
        <div className="min-h-0 flex-1 overflow-auto">
          <header className="border-border bg-bg-elevated-2 sticky top-0 z-10 border-b px-3 py-1.5">
            <h3 className="text-text-dim text-[10px] tracking-wider uppercase">
              Recent news · {selectedSymbol} {news.length > 0 && `(${news.length})`}
            </h3>
          </header>
          {news.length === 0 ? (
            <p className="text-text-dim p-3 text-xs italic">
              No recent triage decisions for this symbol. The autonomous worker watches every
              ticker; decisions accumulate as news flows.
            </p>
          ) : (
            <ul className="divide-border divide-y text-[11px]">
              {news.map((row) => (
                <SymbolNewsRow key={row.event_id} row={row} />
              ))}
            </ul>
          )}
        </div>
      </div>
    </Pane>
  );
}

function InfoBlock({ info }: { info: TickerInfo }) {
  const name = info.long_name ?? info.short_name ?? info.symbol;
  const summary = info.long_business_summary;
  const summaryShort = summary ? truncate(summary, 320) : null;
  const isEtf = info.quote_type === 'ETF';
  return (
    <div className="space-y-2 text-[11px]">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="text-text font-semibold tracking-wide">{name}</h3>
        <span className="text-text-dim shrink-0 text-[9px] tracking-wider uppercase">
          {info.quote_type ?? '—'}
        </span>
      </div>
      <div className="text-text-muted flex flex-wrap items-baseline gap-x-3 gap-y-1 font-mono text-[10px]">
        {info.sector && (
          <span>
            <Label>sector</Label> {info.sector}
          </span>
        )}
        {info.industry && (
          <span>
            <Label>{isEtf ? 'category' : 'industry'}</Label> {info.industry}
          </span>
        )}
        {info.market_cap_usd !== null && (
          <span>
            <Label>{isEtf ? 'aum' : 'mkt cap'}</Label> {formatMarketCap(info.market_cap_usd)}
          </span>
        )}
        {info.employees !== null && info.employees > 0 && (
          <span>
            <Label>employees</Label> {info.employees.toLocaleString()}
          </span>
        )}
      </div>
      <div className="text-text-muted flex flex-wrap items-baseline gap-x-3 gap-y-1 font-mono text-[10px] tabular-nums">
        {info.fifty_two_week_high && (
          <span>
            <Label>52w hi</Label>{' '}
            <span className="text-text">${Number(info.fifty_two_week_high).toFixed(2)}</span>
          </span>
        )}
        {info.fifty_two_week_low && (
          <span>
            <Label>52w lo</Label>{' '}
            <span className="text-text">${Number(info.fifty_two_week_low).toFixed(2)}</span>
          </span>
        )}
        {info.next_earnings_date && (
          <span>
            <Label>next earn</Label>{' '}
            <span className="text-warning">
              {info.next_earnings_date}
              {info.next_earnings_state === 'triggered' ? ' ✓' : ''}
            </span>
          </span>
        )}
      </div>
      {summaryShort && (
        <p className="text-text-muted text-[11px] leading-relaxed">{summaryShort}</p>
      )}
    </div>
  );
}

function SymbolNewsRow({ row }: { row: TickerNewsRow }) {
  const time = formatHM(row.decided_at);
  return (
    <li className="px-3 py-1.5">
      <div className="flex items-baseline gap-1.5">
        <span className="text-text-dim font-mono text-[9px] tabular-nums">{time}</span>
        <Badge tone={row.passed ? 'up' : 'neutral'}>{row.passed ? 'PASS' : 'DROP'}</Badge>
        <span className="border-border text-text-dim rounded border px-1 py-0 font-mono text-[9px] tracking-wider uppercase">
          {row.source === 'edgar'
            ? 'EDGR'
            : row.source === 'alpaca-news'
              ? 'NEWS'
              : row.source.slice(0, 4).toUpperCase()}
        </span>
        <span className="text-text flex-1 truncate font-mono text-[11px]">{row.headline}</span>
        {row.url && (
          <a
            href={row.url}
            target="_blank"
            rel="noreferrer noopener"
            onClick={(e) => e.stopPropagation()}
            className="text-text-dim hover:text-accent shrink-0 text-[10px]"
            title="Open original article"
          >
            ↗
          </a>
        )}
      </div>
      {row.reason && (
        <p className="text-text-dim mt-0.5 pl-12 text-[10px] italic">{truncate(row.reason, 140)}</p>
      )}
    </li>
  );
}

function Label({ children }: { children: string }) {
  return <span className="text-text-dim tracking-wider uppercase">{children}</span>;
}

function formatMarketCap(usd: number): string {
  if (usd >= 1e12) return `$${(usd / 1e12).toFixed(2)}T`;
  if (usd >= 1e9) return `$${(usd / 1e9).toFixed(2)}B`;
  if (usd >= 1e6) return `$${(usd / 1e6).toFixed(0)}M`;
  return `$${usd.toLocaleString()}`;
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

function truncate(s: string, max: number): string {
  if (s.length <= max) return s;
  return s.slice(0, max - 1).trimEnd() + '…';
}
