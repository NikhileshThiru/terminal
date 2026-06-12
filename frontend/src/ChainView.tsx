import { useCallback, useEffect, useMemo, useState } from 'react';

import { PayoffDiagram } from './PayoffDiagram';
import {
  getChain,
  getExpirations,
  type ChainResponse,
  type OptionRow,
  type SuggestedContract,
} from './lib/api';
import { EmptyState } from './ui/EmptyState';

/**
 * Options chain deep-dive. Opened as a modal from the Ticker Info pane (or
 * the `g c` shortcut) — its job is transparency: this is the raw market the
 * agent picks contracts from. Calls left, puts right, strike center, ATM
 * highlighted; click a side to price its payoff at expiration. Prices are
 * 15-min delayed (free-tier Alpaca Indicative Pricing Feed).
 */
export function ChainView({ symbol: initialSymbol }: { symbol: string }) {
  const [input, setInput] = useState(initialSymbol);
  // The symbol whose chain is loaded. State (not a ref) on purpose: it's an
  // effect dependency — a new symbol must reload the chain even when both
  // symbols share the same first expiration date.
  const [loadedSymbol, setLoadedSymbol] = useState<string | null>(null);
  const [expirations, setExpirations] = useState<string[]>([]);
  const [selectedExpiration, setSelectedExpiration] = useState<string | null>(null);
  const [chain, setChain] = useState<ChainResponse | null>(null);
  const [selectedRow, setSelectedRow] = useState<OptionRow | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadExpirations = useCallback(async (sym: string) => {
    setLoading(true);
    setError(null);
    setSelectedRow(null);
    setChain(null);
    try {
      const r = await getExpirations(sym);
      setLoadedSymbol(sym);
      setExpirations(r.expirations);
      setSelectedExpiration(r.expirations[0] ?? null);
      if (r.expirations.length === 0) {
        setError('no expirations returned — chain unavailable for this symbol');
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setLoadedSymbol(null);
      setExpirations([]);
      setSelectedExpiration(null);
    } finally {
      setLoading(false);
    }
  }, []);

  // Load on mount for the symbol the modal was opened with.
  useEffect(() => {
    void loadExpirations(initialSymbol.toUpperCase());
  }, [initialSymbol, loadExpirations]);

  // Load the chain whenever the loaded symbol or expiration changes.
  useEffect(() => {
    if (!loadedSymbol || !selectedExpiration) return;
    let cancelled = false;
    setLoading(true);
    getChain(loadedSymbol, selectedExpiration)
      .then((c) => {
        if (cancelled) return;
        setChain(c);
        setSelectedRow(null);
        setError(null);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [loadedSymbol, selectedExpiration]);

  const underlyingPrice = chain?.underlying_price ? Number(chain.underlying_price) : null;
  const strikeRows = useMemo(() => buildStrikeRows(chain), [chain]);
  const atmStrike = useMemo(() => {
    if (underlyingPrice === null || strikeRows.length === 0) return null;
    return strikeRows.reduce((acc, r) =>
      Math.abs(Number(r.strike) - underlyingPrice) < Math.abs(Number(acc.strike) - underlyingPrice)
        ? r
        : acc,
    ).strike;
  }, [strikeRows, underlyingPrice]);

  const selectedContract: SuggestedContract | null = useMemo(() => {
    if (!selectedRow || !chain) return null;
    const premium = pickPremium(selectedRow);
    if (premium === null) return null;
    return {
      underlying: chain.symbol,
      occ_symbol: selectedRow.occ_symbol,
      option_type: selectedRow.option_type,
      strike: selectedRow.strike,
      expiration: selectedRow.expiration,
      estimated_premium_per_contract: premium.toFixed(2),
      contracts: 1,
      max_risk_usd: (premium * 100).toFixed(2),
    };
  }, [selectedRow, chain]);

  return (
    <div className="space-y-2 font-mono">
      <div className="flex flex-wrap items-center gap-3">
        <form
          className="flex items-center gap-1.5"
          onSubmit={(e) => {
            e.preventDefault();
            const sym = input.trim().toUpperCase();
            if (sym && sym !== loadedSymbol) void loadExpirations(sym);
          }}
        >
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            className="bg-bg border-border-strong text-text focus:border-accent w-24 rounded border px-2 py-1 font-mono text-xs uppercase outline-none"
            placeholder="ticker"
            maxLength={8}
            aria-label="Ticker"
          />
          <button
            type="submit"
            disabled={loading}
            className="bg-accent cursor-pointer rounded px-2.5 py-1 font-mono text-[11px] font-bold text-black disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? '…' : 'load'}
          </button>
        </form>
        {underlyingPrice !== null && (
          <span className="text-text-dim text-[11px] tabular-nums">
            spot <span className="text-text font-semibold">${underlyingPrice.toFixed(2)}</span>
          </span>
        )}
        <span className="text-text-dim ml-auto hidden text-[10px] sm:inline">
          15-min delayed · click a side for its payoff
        </span>
      </div>

      {expirations.length > 0 && (
        <div className="flex flex-wrap items-center gap-1">
          <span className="text-text-dim mr-1 text-[9px] tracking-wider uppercase">expiry</span>
          {expirations.slice(0, 12).map((exp) => (
            <button
              key={exp}
              type="button"
              onClick={() => setSelectedExpiration(exp)}
              className={`cursor-pointer rounded border px-1.5 py-0.5 font-mono text-[10px] tabular-nums transition-colors ${
                exp === selectedExpiration
                  ? 'bg-accent/15 text-accent border-accent/40'
                  : 'text-text-dim hover:text-text border-border'
              }`}
            >
              {exp}
            </button>
          ))}
        </div>
      )}

      {error && (
        <p className="border-error/40 bg-error/10 text-error rounded border px-3 py-2 text-xs">
          {error}
        </p>
      )}

      <div
        className="grid gap-2"
        style={{
          gridTemplateColumns: selectedContract
            ? 'minmax(0, 1.4fr) minmax(0, 1fr)'
            : 'minmax(0, 1fr)',
        }}
      >
        <section className="border-border bg-bg max-h-[55vh] overflow-auto rounded border">
          {!chain && !error && <EmptyState title="Loading chain…" />}
          {chain && strikeRows.length > 0 && (
            <table className="w-full text-[11px] tabular-nums">
              <thead className="bg-bg-elevated-2 sticky top-0 z-10">
                <tr className="text-[9px] tracking-wider uppercase">
                  <th colSpan={4} className="text-up border-border border-b px-2 py-1.5">
                    Calls
                  </th>
                  <th className="text-text-muted border-border border-b px-2 py-1.5">Strike</th>
                  <th colSpan={4} className="text-down border-border border-b px-2 py-1.5">
                    Puts
                  </th>
                </tr>
                <tr className="text-text-dim border-border border-b text-[9px] tracking-wider uppercase">
                  <SubTh>Bid</SubTh>
                  <SubTh>Ask</SubTh>
                  <SubTh>Last</SubTh>
                  <SubTh>Mid</SubTh>
                  <th />
                  <SubTh>Bid</SubTh>
                  <SubTh>Ask</SubTh>
                  <SubTh>Last</SubTh>
                  <SubTh>Mid</SubTh>
                </tr>
              </thead>
              <tbody>
                {strikeRows.map(({ strike, call, put }) => {
                  const isAtm = strike === atmStrike;
                  const strikeNum = Number(strike);
                  const itm =
                    underlyingPrice !== null
                      ? { call: strikeNum < underlyingPrice, put: strikeNum > underlyingPrice }
                      : { call: false, put: false };
                  return (
                    <tr
                      key={strike}
                      className={`border-border border-b text-center last:border-b-0 ${
                        isAtm ? 'bg-accent/8' : ''
                      }`}
                    >
                      <SideCells
                        row={call}
                        selected={selectedRow?.occ_symbol === call?.occ_symbol}
                        itm={itm.call}
                        onSelect={() => call && setSelectedRow(call)}
                      />
                      <td
                        className={`px-2 py-1 font-mono font-semibold ${
                          isAtm ? 'text-accent' : 'text-text'
                        }`}
                        title={isAtm ? 'at the money' : undefined}
                      >
                        ${strikeNum.toFixed(2)}
                      </td>
                      <SideCells
                        row={put}
                        selected={selectedRow?.occ_symbol === put?.occ_symbol}
                        itm={itm.put}
                        onSelect={() => put && setSelectedRow(put)}
                      />
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </section>

        {selectedContract && (
          <section className="border-border bg-bg max-h-[55vh] overflow-auto rounded border p-3">
            <div className="mb-2 flex items-baseline justify-between">
              <code className="text-accent text-xs font-semibold">
                {selectedContract.occ_symbol}
              </code>
              <button
                type="button"
                onClick={() => setSelectedRow(null)}
                className="text-text-dim hover:text-text cursor-pointer text-xs"
                aria-label="Close payoff"
              >
                ×
              </button>
            </div>
            <PayoffDiagram contract={selectedContract} currentUnderlying={underlyingPrice} />
          </section>
        )}
      </div>
    </div>
  );
}

function SubTh({ children }: { children: React.ReactNode }) {
  return <th className="px-2 py-1 font-normal">{children}</th>;
}

/** The four bid/ask/last/mid cells for one side of the chain. */
function SideCells({
  row,
  selected,
  itm,
  onSelect,
}: {
  row: OptionRow | null;
  selected: boolean;
  itm: boolean;
  onSelect: () => void;
}) {
  const base = `px-2 py-1 transition-colors ${
    row ? 'cursor-pointer hover:bg-bg-elevated-2' : 'cursor-default'
  } ${selected ? 'bg-accent/15 text-accent' : itm ? 'bg-bg-elevated-2/60 text-text' : 'text-text-muted'}`;
  return (
    <>
      <td className={base} onClick={onSelect}>
        {fmt(row?.bid)}
      </td>
      <td className={base} onClick={onSelect}>
        {fmt(row?.ask)}
      </td>
      <td className={base} onClick={onSelect}>
        {fmt(row?.last)}
      </td>
      <td className={`${base} font-semibold`} onClick={onSelect}>
        {fmt(row?.mid)}
      </td>
    </>
  );
}

interface StrikeRow {
  strike: string;
  call: OptionRow | null;
  put: OptionRow | null;
}

function buildStrikeRows(chain: ChainResponse | null): StrikeRow[] {
  if (!chain) return [];
  const byStrike = new Map<string, StrikeRow>();
  for (const c of chain.calls) {
    const row = byStrike.get(c.strike) ?? { strike: c.strike, call: null, put: null };
    row.call = c;
    byStrike.set(c.strike, row);
  }
  for (const p of chain.puts) {
    const row = byStrike.get(p.strike) ?? { strike: p.strike, call: null, put: null };
    row.put = p;
    byStrike.set(p.strike, row);
  }
  return Array.from(byStrike.values()).sort((a, b) => Number(a.strike) - Number(b.strike));
}

function pickPremium(row: OptionRow): number | null {
  if (row.mid !== null) return Number(row.mid);
  if (row.last !== null) return Number(row.last);
  if (row.ask !== null) return Number(row.ask);
  if (row.bid !== null) return Number(row.bid);
  return null;
}

function fmt(s: string | null | undefined): string {
  if (s === null || s === undefined) return '—';
  const n = Number(s);
  if (!isFinite(n) || n === 0) return '—';
  return n.toFixed(2);
}
