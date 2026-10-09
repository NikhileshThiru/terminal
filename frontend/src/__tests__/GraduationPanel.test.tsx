import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { GraduationPanel, progress } from '../GraduationPanel';
import type { GraduationCriterion } from '../lib/api';

const NOT_READY = {
  ready: false,
  criteria: [
    {
      key: 'volume',
      label: 'Shadow theses logged',
      value: 22,
      threshold: 50,
      comparator: '>=',
      passed: false,
      detail: '22 of 50 autonomous theses',
    },
    {
      key: 'grounding',
      label: 'Grounding pass rate',
      value: 0.97,
      threshold: 0.95,
      comparator: '>=',
      passed: true,
      detail: '33 grounded / 34 judged attempts',
    },
    {
      key: 'calibration',
      label: 'Brier score (resolved shadow theses)',
      value: 0.31,
      threshold: 0.25,
      comparator: '<=',
      passed: false,
      detail: 'over 18 resolved',
    },
  ],
  shadow_theses: 22,
  resolved: 18,
  attempts: 34,
  grounding_rejected: 1,
  hit_rate: 0.44,
  attempts_tracked_since: '2026-10-09T13:30:00Z',
  notes: ['Brier is over only 18 resolved theses — too few to trust either way.'],
};

afterEach(() => vi.restoreAllMocks());

describe('GraduationPanel', () => {
  it('reports each criterion honestly and never claims readiness early', async () => {
    globalThis.fetch = vi.fn(
      async () => ({ ok: true, status: 200, json: async () => NOT_READY }) as Response,
    );
    render(<GraduationPanel />);
    await waitFor(() => expect(screen.getByText('shadow only')).toBeInTheDocument());
    expect(screen.getByText('22')).toBeInTheDocument();
    expect(screen.getByText('97%')).toBeInTheDocument();
    expect(screen.getByText('0.310')).toBeInTheDocument();
    expect(screen.getByText(/too few to trust/)).toBeInTheDocument();
  });

  it('progress handles both comparator directions', () => {
    const c = (over: Partial<GraduationCriterion>): GraduationCriterion => ({
      key: 'volume',
      label: '',
      value: 25,
      threshold: 50,
      comparator: '>=',
      passed: false,
      detail: '',
      ...over,
    });
    expect(progress(c({}))).toBe(0.5);
    expect(progress(c({ value: null }))).toBe(0);
    expect(progress(c({ key: 'calibration', comparator: '<=', value: 0.2, threshold: 0.25 }))).toBe(
      1,
    );
    expect(progress(c({ key: 'calibration', comparator: '<=', value: 0.5, threshold: 0.25 }))).toBe(
      0.5,
    );
  });
});
