import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ModalProvider } from '../chrome/ModalContext';
import { AutonomousPage } from '../pages/AutonomousPage';

const STOPPED_STATUS = {
  state: 'stopped',
  started_at: null,
  stopped_at: null,
  watchlist: ['AAPL', 'MSFT', 'NVDA'],
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

const RUNNING_STATUS = {
  ...STOPPED_STATUS,
  state: 'running',
  started_at: '2026-06-03T10:00:00Z',
};

/** Returns a fetch mock that dispatches based on URL substring + method. */
function mockFetch(routes: Array<[string, string, () => unknown]>) {
  return vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    for (const [routeMethod, routeUrlContains, handler] of routes) {
      if (routeMethod === method && url.includes(routeUrlContains)) {
        return {
          ok: true,
          status: 200,
          json: async () => handler(),
        } as Response;
      }
    }
    throw new Error(`Unmocked: ${method} ${url}`);
  });
}

function renderPage() {
  return render(
    <ModalProvider>
      <AutonomousPage />
    </ModalProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('AutonomousPage', () => {
  it('renders title, status grid (stopped), and toggle button', async () => {
    globalThis.fetch = mockFetch([
      ['GET', '/autonomous/status', () => STOPPED_STATUS],
      ['GET', '/autonomous/theses', () => []],
      ['GET', '/autonomous/catalysts', () => []],
    ]);
    renderPage();
    expect(screen.getByRole('heading', { name: /autonomous worker/i })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('stopped')).toBeInTheDocument());
    expect(screen.getByRole('button', { name: /click to start/i })).toBeInTheDocument();
  });

  it('clicking start fires POST /autonomous/start and updates UI to running', async () => {
    let startCalled = false;
    globalThis.fetch = mockFetch([
      ['GET', '/autonomous/status', () => STOPPED_STATUS],
      ['GET', '/autonomous/theses', () => []],
      ['GET', '/autonomous/catalysts', () => []],
      [
        'POST',
        '/autonomous/start',
        () => {
          startCalled = true;
          return RUNNING_STATUS;
        },
      ],
    ]);
    const user = userEvent.setup();
    renderPage();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /click to start/i })).toBeInTheDocument(),
    );
    await user.click(screen.getByRole('button', { name: /click to start/i }));
    await waitFor(() => expect(startCalled).toBe(true));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /click to stop/i })).toBeInTheDocument(),
    );
  });

  it('renders triage decisions when present', async () => {
    globalThis.fetch = mockFetch([
      [
        'GET',
        '/autonomous/status',
        () => ({
          ...RUNNING_STATUS,
          recent_triage_decisions: [
            {
              event_id: 'acc-1',
              symbol: 'AAPL',
              headline: 'AAPL 8-K earnings beat',
              passed: true,
              reason: 'material earnings',
              confidence: 0.85,
              at: '2026-06-03T10:01:00Z',
            },
            {
              event_id: 'acc-2',
              symbol: 'MSFT',
              headline: 'MSFT board reelection',
              passed: false,
              reason: 'routine governance',
              confidence: 0.7,
              at: '2026-06-03T10:02:00Z',
            },
          ],
        }),
      ],
      ['GET', '/autonomous/theses', () => []],
      ['GET', '/autonomous/catalysts', () => []],
    ]);
    renderPage();
    await waitFor(() => expect(screen.getByText(/AAPL 8-K earnings beat/)).toBeInTheDocument());
    expect(screen.getByText('PASS')).toBeInTheDocument();
    expect(screen.getByText('DROP')).toBeInTheDocument();
  });

  it('sorts triage decisions with watchlist symbols first', async () => {
    // Order in the data is non-watchlist first, watchlist second. After
    // sorting we expect the watchlist row (starred) to render first.
    globalThis.fetch = mockFetch([
      [
        'GET',
        '/autonomous/status',
        () => ({
          ...RUNNING_STATUS,
          watchlist: ['AAPL'],
          recent_triage_decisions: [
            {
              event_id: 'tsla-1',
              symbol: 'TSLA',
              headline: 'TSLA recall',
              passed: true,
              reason: 'material',
              confidence: 0.6,
              at: '2026-06-03T10:00:00Z',
            },
            {
              event_id: 'aapl-1',
              symbol: 'AAPL',
              headline: 'AAPL on watchlist',
              passed: true,
              reason: 'material',
              confidence: 0.8,
              at: '2026-06-03T10:01:00Z',
            },
          ],
        }),
      ],
      ['GET', '/autonomous/theses', () => []],
      ['GET', '/autonomous/catalysts', () => []],
    ]);
    renderPage();
    await waitFor(() => expect(screen.getByText(/AAPL on watchlist/)).toBeInTheDocument());
    const aapl = screen.getByText(/AAPL on watchlist/).closest('li');
    const tsla = screen.getByText(/TSLA recall/).closest('li');
    expect(aapl).not.toBeNull();
    expect(tsla).not.toBeNull();
    // The watchlist row carries the star and renders before the off-list row.
    expect(aapl?.textContent).toMatch(/★/);
    expect(tsla?.textContent).not.toMatch(/★/);
    expect(aapl!.compareDocumentPosition(tsla!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
