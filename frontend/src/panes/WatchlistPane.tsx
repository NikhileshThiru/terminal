import { useEffect, useMemo, useRef, useState } from 'react';

import { useNavigation } from '../chrome/NavigationContext';
import { Pane } from '../chrome/Pane';
import { useSelection } from '../chrome/SelectionContext';
import { Sparkline } from '../chrome/Sparkline';
import { getAutonomousStatus, getBarsBatch, type BarPoint } from '../lib/api';

interface LiveQuote {
  mid: number | null;
  bid: number | null;
  ask: number | null;
  /** Last non-null mid we saw — used to compute up/down ticks even when a
   * poll returns null mid (one-sided quote). */
  prevMid: number | null;
  ts: number;
}

interface SymbolHistory {
  closes: number[];
  prevClose: number | null;
}

type QuoteMap = Record<string, LiveQuote>;
type HistoryMap = Record<string, SymbolHistory>;

const API_URL = import.meta.env.VITE_API_URL ?? 'http://localhost:8000';

/**
 * Watchlist sidebar. Pulls the symbol list from the autonomous worker,
 * then subscribes to /prices/stream for live(ish) quotes plus a one-shot
 * batch of 30-day daily bars per symbol for sparklines + day-change %.
 *
 * Free-tier Alpaca is 15-min delayed; the UX is the same as a real-time
 * terminal. Each row is a clickable button that drives the global symbol
 * selection (Chart + Chain + future deep-dive views follow along).
 */
export function WatchlistPane() {
  const { selectedSymbol, setSelectedSymbol } = useSelection();
  const { page, setPage } = useNavigation();
  const [symbols, setSymbols] = useState<string[]>([]);
  const [quotes, setQuotes] = useState<QuoteMap>({});
  const [history, setHistory] = useState<HistoryMap>({});
  const [lastTickAt, setLastTickAt] = useState<number | null>(null);
  const [, forceRerender] = useState(0);
  const esRef = useRef<EventSource | null>(null);

  // Pull watchlist symbols once.
  useEffect(() => {
    let cancelled = false;
    getAutonomousStatus()
      .then((s) => !cancelled && setSymbols(s.watchlist))
      .catch(() => {
        /* worker may not be initialised; non-fatal */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Fetch 30-day daily bars in one batch for sparklines + daily change.
  useEffect(() => {
    if (symbols.length === 0) return;
    let cancelled = false;
    getBarsBatch(symbols, '1Day', 30)
      .then((r) => {
        if (cancelled) return;
        const next: HistoryMap = {};
        for (const sym of symbols) {
          const points = r.series[sym] ?? [];
          const closes = points.map((p: BarPoint) => Number(p.c)).filter((n) => isFinite(n));
          const prevClose = closes.length >= 2 ? closes[closes.length - 2] : null;
          next[sym] = { closes, prevClose };
        }
        setHistory(next);
      })
      .catch(() => {
        /* non-fatal — sparklines just won't render */
      });
    return () => {
      cancelled = true;
    };
  }, [symbols]);

  // Open the SSE stream whenever the symbol list changes.
  useEffect(() => {
    if (symbols.length === 0) return;
    const url = `${API_URL}/prices/stream?symbols=${encodeURIComponent(symbols.join(','))}`;
    const es = new EventSource(url);
    esRef.current = es;

    es.addEventListener('quote', (e) => {
      try {
        const data = JSON.parse((e as MessageEvent<string>).data) as {
          symbol: string;
          bid: string | null;
          ask: string | null;
          mid: string | null;
          ts: string;
        };
        setQuotes((prev) => {
          const newMid = data.mid !== null ? Number(data.mid) : null;
          const prior = prev[data.symbol];
          return {
            ...prev,
            [data.symbol]: {
              mid: newMid,
              bid: data.bid !== null ? Number(data.bid) : null,
              ask: data.ask !== null ? Number(data.ask) : null,
              prevMid: prior?.mid ?? prior?.prevMid ?? null,
              ts: Date.parse(data.ts),
            },
          };
        });
        setLastTickAt(Date.now());
      } catch {
        /* malformed event; skip */
      }
    });

    es.addEventListener('unavailable', (e) => {
      try {
        const data = JSON.parse((e as MessageEvent<string>).data) as { symbol: string };
        setQuotes((prev) => ({
          ...prev,
          [data.symbol]: {
            mid: null,
            bid: null,
            ask: null,
            prevMid: prev[data.symbol]?.mid ?? null,
            ts: Date.now(),
          },
        }));
      } catch {
        /* skip */
      }
    });

    es.onerror = () => {
      // EventSource auto-reconnects; nothing to do.
    };

    return () => {
      es.close();
      esRef.current = null;
    };
  }, [symbols]);

  // Tick a re-render every 5s so the "Xs ago" freshness label stays current
  // even when no new quotes are arriving.
  useEffect(() => {
    const t = setInterval(() => forceRerender((n) => n + 1), 5000);
    return () => clearInterval(t);
  }, []);

  const freshnessLabel = useMemo(() => formatFreshness(lastTickAt), [lastTickAt]);

  return (
    <Pane
      title="Watchlist"
      subtitle={
        <span className="text-text-dim flex items-center gap-1.5 text-[10px]">
          <span
            className={`inline-block h-1.5 w-1.5 rounded-full ${
              esRef.current ? 'bg-accent animate-pulse' : 'bg-text-dim'
            }`}
          />
          {freshnessLabel}
        </span>
      }
      bodyClassName="p-0"
    >
      {symbols.length === 0 ? (
        <p className="text-text-dim p-2 text-xs italic">No watchlist symbols configured.</p>
      ) : (
        <ul className="divide-border divide-y">
          {symbols.map((sym) => (
            <WatchlistRow
              key={sym}
              symbol={sym}
              quote={quotes[sym]}
              history={history[sym]}
              active={sym === selectedSymbol}
              onClick={() => {
                setSelectedSymbol(sym);
                // The dashboard is the symbol-aware view; from anywhere else a
                // watchlist click jumps there so it visibly does something.
                if (page !== 'dashboard') {
                  setPage('dashboard');
                }
              }}
            />
          ))}
        </ul>
      )}
    </Pane>
  );
}

function WatchlistRow({
  symbol,
  quote,
  history,
  active,
  onClick,
}: {
  symbol: string;
  quote: LiveQuote | undefined;
  history: SymbolHistory | undefined;
  active: boolean;
  onClick: () => void;
}) {
  const mid = quote?.mid ?? null;
  const prev = quote?.prevMid ?? null;
  const arrow = mid !== null && prev !== null ? (mid > prev ? '▲' : mid < prev ? '▼' : '·') : '◦';
  const tick =
    mid !== null && prev !== null ? (mid > prev ? 'up' : mid < prev ? 'down' : null) : null;
  const arrowTone = tick === 'up' ? 'text-up' : tick === 'down' ? 'text-down' : 'text-text-dim';

  // Daily change: live mid (or last close) vs prior close.
  const refClose = history?.prevClose ?? null;
  const liveOrLastClose =
    mid !== null
      ? mid
      : history && history.closes.length > 0
        ? history.closes[history.closes.length - 1]
        : null;
  const change = refClose !== null && liveOrLastClose !== null ? liveOrLastClose - refClose : null;
  const changePct = change !== null && refClose ? (change / refClose) * 100 : null;
  const changeTone =
    change === null
      ? 'text-text-dim'
      : change > 0
        ? 'text-up'
        : change < 0
          ? 'text-down'
          : 'text-text-muted';

  const sparkValues = history?.closes ?? [];

  return (
    <li
      // Re-keying by quote timestamp restarts the flash animation on every tick.
      key={quote?.ts ?? 0}
      className={tick === 'up' ? 'animate-flash-up' : tick === 'down' ? 'animate-flash-down' : ''}
    >
      <button
        type="button"
        onClick={onClick}
        className={`grid w-full cursor-pointer items-center gap-1.5 border-l-2 px-2 py-1.5 text-left text-xs transition-colors ${
          active
            ? 'bg-bg-elevated-2 border-accent text-accent'
            : 'text-text hover:bg-bg-elevated-2 border-transparent'
        }`}
        style={{ gridTemplateColumns: '4ch minmax(48px, 1fr) auto' }}
      >
        <span className="font-mono font-semibold tracking-wider">{symbol}</span>
        <Sparkline values={sparkValues} negative={change !== null && change < 0} />
        <span className="flex flex-col items-end gap-0 font-mono tabular-nums leading-tight">
          <span className="flex items-baseline gap-1">
            <span className={`text-[9px] ${arrowTone}`}>{arrow}</span>
            {/* No live mid (one-sided after-hours quote) → show last close, dimmed. */}
            <span className={mid !== null ? '' : 'text-text-dim'}>
              {mid !== null
                ? mid.toFixed(2)
                : liveOrLastClose !== null
                  ? liveOrLastClose.toFixed(2)
                  : '—'}
            </span>
          </span>
          <span className={`text-[9px] ${changeTone}`}>
            {changePct === null ? '—' : `${changePct >= 0 ? '+' : ''}${changePct.toFixed(2)}%`}
          </span>
        </span>
      </button>
    </li>
  );
}

function formatFreshness(lastTickAt: number | null): string {
  if (lastTickAt === null) return 'waiting';
  const secs = Math.floor((Date.now() - lastTickAt) / 1000);
  if (secs < 2) return 'just now';
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  return `${Math.floor(mins / 60)}h ago`;
}
