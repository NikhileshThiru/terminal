import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { EvalPage } from '../pages/EvalPage';

const EMPTY_SUMMARY = {
  buckets: [
    { bucket: 'manual', count_theses: 0, count_resolved: 0, brier: null, hit_rate: null },
    { bucket: 'reactive', count_theses: 0, count_resolved: 0, brier: null, hit_rate: null },
    { bucket: 'catalyst', count_theses: 0, count_resolved: 0, brier: null, hit_rate: null },
  ],
};

const POPULATED_SUMMARY = {
  buckets: [
    { bucket: 'manual', count_theses: 5, count_resolved: 3, brier: 0.15, hit_rate: 0.67 },
    { bucket: 'reactive', count_theses: 2, count_resolved: 0, brier: null, hit_rate: null },
    { bucket: 'catalyst', count_theses: 0, count_resolved: 0, brier: null, hit_rate: null },
  ],
};

const POPULATED_CALIBRATION = {
  bucket: 'manual',
  n_buckets: 10,
  points: [
    {
      bucket_lower: 0.6,
      bucket_upper: 0.7,
      count: 2,
      mean_confidence: 0.65,
      realized_hit_rate: 1.0,
    },
    {
      bucket_lower: 0.8,
      bucket_upper: 0.9,
      count: 1,
      mean_confidence: 0.85,
      realized_hit_rate: 0.0,
    },
  ],
};

const EMPTY_CALIBRATION = { bucket: 'manual', n_buckets: 10, points: [] };

function _mockFetch(routes: Array<[string, () => unknown]>) {
  return vi.fn(async (input: string | URL | Request) => {
    const url = typeof input === 'string' ? input : input.toString();
    for (const [contains, handler] of routes) {
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

describe('EvalPage', () => {
  it('renders all three bucket cards with dashes when empty', async () => {
    globalThis.fetch = _mockFetch([
      ['/eval/summary', () => EMPTY_SUMMARY],
      ['/eval/calibration', () => EMPTY_CALIBRATION],
      ['/eval/outcomes', () => []],
    ]);
    render(<EvalPage />);
    await waitFor(() => expect(screen.getAllByText('manual').length).toBeGreaterThan(0));
    expect(screen.getByText('reactive')).toBeInTheDocument();
    expect(screen.getByText('catalyst')).toBeInTheDocument();
    // Empty buckets show em-dashes for Brier + hit rate, not zeros.
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
    // Honest empty-state copy for the calibration plot.
    expect(screen.getByText(/No resolved outcomes/i)).toBeInTheDocument();
  });

  it('shows brier and hit rate when bucket has resolved outcomes', async () => {
    globalThis.fetch = _mockFetch([
      ['/eval/summary', () => POPULATED_SUMMARY],
      ['/eval/calibration', () => POPULATED_CALIBRATION],
      ['/eval/outcomes', () => []],
    ]);
    render(<EvalPage />);
    await waitFor(() => expect(screen.getByText('0.150')).toBeInTheDocument()); // Brier
    expect(screen.getByText('67%')).toBeInTheDocument(); // hit rate
  });

  it('renders calibration points when data exists', async () => {
    globalThis.fetch = _mockFetch([
      ['/eval/summary', () => POPULATED_SUMMARY],
      ['/eval/calibration', () => POPULATED_CALIBRATION],
      ['/eval/outcomes', () => []],
    ]);
    const { container } = render(<EvalPage />);
    await waitFor(() => {
      const circles = container.querySelectorAll('svg circle');
      expect(circles.length).toBe(2);
    });
  });
});
