import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ChainView } from '../ChainView';

const EXPIRATIONS = (symbol: string) => ({
  symbol,
  // Deliberately identical dates across symbols — regression guard for the
  // "chain doesn't follow the ticker" bug (an unchanged expiration string
  // must not suppress the reload).
  expirations: ['2026-08-21', '2026-09-18'],
});

const CHAIN = (symbol: string) => ({
  symbol,
  expiration: '2026-08-21',
  // Different spot per symbol so tests can detect which chain is shown.
  underlying_price: symbol === 'NVDA' ? '500.00' : '320.00',
  calls: [
    {
      occ_symbol: `${symbol}260821C00310000`,
      expiration: '2026-08-21',
      strike: '310.00',
      option_type: 'call' as const,
      bid: '15.00',
      ask: '15.20',
      last: '15.10',
      mid: '15.10',
    },
    {
      occ_symbol: `${symbol}260821C00320000`,
      expiration: '2026-08-21',
      strike: '320.00',
      option_type: 'call' as const,
      bid: '8.00',
      ask: '8.20',
      last: '8.10',
      mid: '8.10',
    },
  ],
  puts: [
    {
      occ_symbol: `${symbol}260821P00320000`,
      expiration: '2026-08-21',
      strike: '320.00',
      option_type: 'put' as const,
      bid: '6.00',
      ask: '6.20',
      last: '6.10',
      mid: '6.10',
    },
  ],
});

function mockFetch() {
  return vi.fn(async (input: string | URL | Request) => {
    const url = typeof input === 'string' ? input : input.toString();
    const m = url.match(/\/chain\/([A-Z]+)/);
    const sym = m ? m[1] : 'AAPL';
    const body = url.includes('/expirations') ? EXPIRATIONS(sym) : CHAIN(sym);
    return { ok: true, status: 200, json: async () => body } as Response;
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('ChainView', () => {
  it('auto-loads the chain for the given symbol on mount', async () => {
    globalThis.fetch = mockFetch();
    render(<ChainView symbol="AAPL" />);
    await waitFor(() => expect(screen.getByText('Calls')).toBeInTheDocument());
    expect(screen.getByText('Puts')).toBeInTheDocument();
    // "$320.00" appears at least twice: spot price + the ATM strike cell.
    expect(screen.getAllByText('$320.00').length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText('$310.00')).toBeInTheDocument();
  });

  it('selects a contract on cell click and shows its payoff', async () => {
    globalThis.fetch = mockFetch();
    const user = userEvent.setup();
    render(<ChainView symbol="AAPL" />);
    await waitFor(() => expect(screen.getByText('Calls')).toBeInTheDocument());
    await user.click(screen.getByText('15.00'));
    await waitFor(() => expect(screen.getByText(/Long call/i)).toBeInTheDocument());
    expect(screen.getByText('AAPL260821C00310000')).toBeInTheDocument();
  });

  it('reloads when the symbol changes even if expirations are identical', async () => {
    globalThis.fetch = mockFetch();
    const { rerender } = render(<ChainView symbol="AAPL" />);
    // AAPL spot is $320.
    await waitFor(() => expect(screen.getAllByText('$320.00').length).toBeGreaterThanOrEqual(2));

    rerender(<ChainView symbol="NVDA" />);
    // NVDA spot is $500 — must replace the AAPL chain despite both symbols
    // sharing the exact same expiration dates (the old bug).
    await waitFor(() => expect(screen.getByText('$500.00')).toBeInTheDocument());
  });
});
