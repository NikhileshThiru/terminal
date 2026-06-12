import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { PortfolioPage } from '../pages/PortfolioPage';

const ACCOUNTS = [
  {
    id: 1,
    kind: 'conservative',
    name: 'Conservative',
    starting_balance_usd: '100000.00',
    equity_usd: '100000.00',
    min_confidence: 0.7,
    max_trade_cost_usd: '300.00',
    max_trades_per_day: 3,
    max_concurrent_positions: 5,
    kill_switch: false,
    open_shadow_positions: 1,
    shadow_trades_today: 2,
    shadow_trades_total: 4,
    total_cost_open_usd: '250.00',
  },
  {
    id: 2,
    kind: 'aggressive',
    name: 'Aggressive',
    starting_balance_usd: '100000.00',
    equity_usd: '100000.00',
    min_confidence: 0.5,
    max_trade_cost_usd: '500.00',
    max_trades_per_day: 8,
    max_concurrent_positions: 10,
    kill_switch: false,
    open_shadow_positions: 3,
    shadow_trades_today: 5,
    shadow_trades_total: 12,
    total_cost_open_usd: '1200.00',
  },
];

const TRADES = [
  {
    id: 1,
    account_id: 2,
    account_kind: 'aggressive',
    thesis_id: 10,
    opened_at: '2026-06-04T14:30:00Z',
    underlying: 'AAPL',
    occ_symbol: 'AAPL301220C00150000',
    option_type: 'call' as const,
    strike: '150',
    expiration: '2030-12-20',
    contracts: 1,
    premium_per_contract_usd: '4.60',
    total_cost_usd: '460.00',
    status: 'shadow_open',
    risk_reason: 'Confidence 0.65 ≥ aggressive threshold 0.50.',
    closed_at: null,
    close_reason: null,
    realized_pnl_usd: null,
    unrealized_pnl_usd: '-42.00',
    marked_at: '2026-06-04T15:00:00Z',
  },
];

const EQUITY_EMPTY = (kind: string) => ({
  account_kind: kind,
  starting_balance_usd: '100000.00',
  points: [],
});

function _mockFetch(routes: Array<[string, () => unknown]>) {
  // Always provide a safe default for the equity-curve endpoint so the
  // EquityCurveChart can render. Test-specific routes take precedence.
  const fallbackRoutes: Array<[string, () => unknown]> = [
    ['/equity-curve', () => EQUITY_EMPTY('conservative')],
  ];
  return vi.fn(async (input: string | URL | Request) => {
    const url = typeof input === 'string' ? input : input.toString();
    for (const [contains, handler] of [...routes, ...fallbackRoutes]) {
      if (url.includes(contains)) {
        return { ok: true, status: 200, json: async () => handler() } as Response;
      }
    }
    throw new Error(`Unmocked: ${url}`);
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('PortfolioPage', () => {
  it('renders both account cards with stats', async () => {
    globalThis.fetch = _mockFetch([
      ['/portfolio/accounts', () => ACCOUNTS],
      ['/portfolio/shadow-trades', () => []],
    ]);
    render(<PortfolioPage />);
    await waitFor(() => expect(screen.getByText('Conservative')).toBeInTheDocument());
    expect(screen.getByText('Aggressive')).toBeInTheDocument();
    // Caps are visible: trades-today/max for both accounts.
    expect(screen.getByText('2 / 3')).toBeInTheDocument();
    expect(screen.getByText('5 / 8')).toBeInTheDocument();
  });

  it('renders an open position with its live mark', async () => {
    globalThis.fetch = _mockFetch([
      ['/portfolio/accounts', () => ACCOUNTS],
      ['/portfolio/shadow-trades', () => TRADES],
    ]);
    render(<PortfolioPage />);
    await waitFor(() => expect(screen.getByText('AAPL301220C00150000')).toBeInTheDocument());
    // Open position shows its latest unrealized mark, colored + signed.
    expect(screen.getByText(/-\$42/)).toBeInTheDocument();
    // Risk reason is preserved as the row tooltip.
    expect(screen.getByTitle(/Confidence 0\.65/)).toBeInTheDocument();
    // History section stays honest when nothing has closed.
    expect(screen.getByText(/No closed trades yet/i)).toBeInTheDocument();
  });

  it('shows honest empty states when no trades', async () => {
    globalThis.fetch = _mockFetch([
      ['/portfolio/accounts', () => ACCOUNTS],
      ['/portfolio/shadow-trades', () => []],
    ]);
    render(<PortfolioPage />);
    await waitFor(() => expect(screen.getByText(/Nothing open right now/i)).toBeInTheDocument());
    expect(screen.getByText(/No closed trades yet/i)).toBeInTheDocument();
  });
});
