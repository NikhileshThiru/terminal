import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import App from '../App';

const STOPPED_STATUS = {
  state: 'stopped',
  started_at: null,
  stopped_at: null,
  watchlist: ['AAPL'],
  poll_interval_seconds: 300,
  triage_provider: 'gemini',
  triage_model: 'gemini-2.5-flash-lite',
  thesis_provider: 'gemini',
  thesis_model: 'gemini-2.5-flash',
  polls_completed: 0,
  events_published: 0,
  events_consumed: 0,
  events_passed_triage: 0,
  theses_produced: 0,
  triage_failures: 0,
  thesis_failures: 0,
  persistence_failures: 0,
  queue_depth: 0,
  last_event_at: null,
  last_thesis_at: null,
  last_poll_at: null,
  last_error: null,
  recent_triage_decisions: [],
};

const EVAL_SUMMARY = {
  buckets: [
    { bucket: 'manual', count_theses: 0, count_resolved: 0, brier: null, hit_rate: null },
    { bucket: 'reactive', count_theses: 0, count_resolved: 0, brier: null, hit_rate: null },
    { bucket: 'catalyst', count_theses: 0, count_resolved: 0, brier: null, hit_rate: null },
  ],
};

const okJson = (body: unknown) => ({ ok: true, status: 200, json: async () => body }) as Response;

/** Routes fetch calls by URL substring. Tests pass a health responder; we
 * stub the rest with safe defaults so panes in the new grid don't crash. */
function routedFetch(healthResponder: () => Response | Promise<Response>) {
  return vi.fn(async (input: string | URL | Request) => {
    const url = typeof input === 'string' ? input : input.toString();
    if (url.includes('/health')) return healthResponder();
    if (url.includes('/autonomous/status')) return okJson(STOPPED_STATUS);
    if (url.includes('/autonomous/theses')) return okJson([]);
    if (url.includes('/autonomous/catalysts')) return okJson([]);
    if (url.includes('/portfolio/accounts')) return okJson([]);
    if (url.includes('/portfolio/shadow-trades')) return okJson([]);
    if (url.includes('/eval/summary')) return okJson(EVAL_SUMMARY);
    if (url.includes('/eval/calibration'))
      return okJson({ bucket: 'manual', n_buckets: 10, points: [] });
    if (url.includes('/llm/cost-summary'))
      return okJson({
        calls: 0,
        input_tokens: 0,
        output_tokens: 0,
        cost_usd: 0,
        since: '2026-06-01T00:00:00Z',
        by_model: [],
        daily_request_budget: 1500,
      });
    if (url.includes('/bars/')) return okJson({ symbol: 'AAPL', timeframe: '1Day', bars: [] });
    if (url.includes('/chain/')) {
      if (url.includes('/expirations')) return okJson({ symbol: 'AAPL', expirations: [] });
      return okJson({
        symbol: 'AAPL',
        expiration: '2026-07-25',
        underlying_price: null,
        calls: [],
        puts: [],
      });
    }
    if (url.includes('/tickers/') && url.includes('/info')) {
      return okJson({
        symbol: 'AAPL',
        long_name: 'Apple Inc.',
        short_name: 'Apple',
        sector: 'Technology',
        industry: 'Consumer Electronics',
        quote_type: 'EQUITY',
        market_cap_usd: 3000000000000,
        employees: 150000,
        long_business_summary: null,
        website: null,
        fifty_two_week_high: null,
        fifty_two_week_low: null,
        next_earnings_date: null,
        next_earnings_state: null,
        next_earnings_thesis_id: null,
      });
    }
    if (url.includes('/tickers/') && url.includes('/news')) {
      return okJson({ symbol: 'AAPL', rows: [] });
    }
    throw new Error(`Unmocked: ${url}`);
  });
}

// In the new sidebar layout the App test only verifies the top-level chrome
// and routing landmark. The deep panel behavior is covered by per-pane tests.

afterEach(() => {
  vi.restoreAllMocks();
});

describe('App', () => {
  it('renders the title', () => {
    globalThis.fetch = routedFetch(
      () => ({ ok: false, status: 503, json: async () => ({}) }) as Response,
    );
    render(<App />);
    expect(screen.getByRole('heading', { name: /terminal/i })).toBeInTheDocument();
  });

  it('shows backend ok when health check succeeds', async () => {
    globalThis.fetch = routedFetch(() =>
      okJson({
        status: 'ok',
        version: '0.1.0',
        timestamp: '2026-06-01T00:00:00Z',
      }),
    );
    render(<App />);
    await waitFor(() => expect(screen.getByLabelText('backend ok')).toBeInTheDocument());
  });

  it('shows backend offline when health check fails', async () => {
    globalThis.fetch = vi.fn(async (input: string | URL | Request) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('/health')) throw new Error('connection refused');
      if (url.includes('/autonomous/status')) return okJson(STOPPED_STATUS);
      if (url.includes('/autonomous/theses')) return okJson([]);
      if (url.includes('/autonomous/catalysts')) return okJson([]);
      if (url.includes('/portfolio/accounts')) return okJson([]);
      if (url.includes('/portfolio/shadow-trades')) return okJson([]);
      if (url.includes('/eval/summary')) return okJson(EVAL_SUMMARY);
      if (url.includes('/eval/calibration'))
        return okJson({ bucket: 'manual', n_buckets: 10, points: [] });
      if (url.includes('/bars/')) return okJson({ symbol: 'AAPL', timeframe: '1Day', bars: [] });
      if (url.includes('/chain/')) {
        if (url.includes('/expirations')) return okJson({ symbol: 'AAPL', expirations: [] });
        return okJson({
          symbol: 'AAPL',
          expiration: '2026-07-25',
          underlying_price: null,
          calls: [],
          puts: [],
        });
      }
      throw new Error(`Unmocked: ${url}`);
    });
    render(<App />);
    await waitFor(() => expect(screen.getByLabelText('backend offline')).toBeInTheDocument());
  });

  it('renders the sidebar nav and the default dashboard page', async () => {
    globalThis.fetch = routedFetch(() =>
      okJson({ status: 'ok', version: '0.1.0', timestamp: '2026-06-01T00:00:00Z' }),
    );
    render(<App />);
    // Sidebar nav items
    expect(screen.getByRole('button', { name: /Dashboard/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Portfolio/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Evaluation/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Ask the Agent/ })).toBeInTheDocument();
    // Watchlist (in sidebar) + Dashboard panes
    expect(screen.getByText('Watchlist')).toBeInTheDocument();
    expect(screen.getByText('Live theses')).toBeInTheDocument();
    expect(screen.getByText('Ticker info')).toBeInTheDocument();
  });
});
