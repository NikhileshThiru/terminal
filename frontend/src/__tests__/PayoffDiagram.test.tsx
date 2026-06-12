import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { PayoffDiagram } from '../PayoffDiagram';
import type { SuggestedContract } from '../lib/api';

function makeCall(overrides: Partial<SuggestedContract> = {}): SuggestedContract {
  return {
    underlying: 'AAPL',
    occ_symbol: 'AAPL260821C00350000',
    option_type: 'call',
    strike: '350.00',
    expiration: '2026-08-21',
    estimated_premium_per_contract: '3.88',
    contracts: 1,
    max_risk_usd: '388.00',
    ...overrides,
  };
}

function makePut(overrides: Partial<SuggestedContract> = {}): SuggestedContract {
  return makeCall({ option_type: 'put', occ_symbol: 'AAPL260821P00350000', ...overrides });
}

describe('PayoffDiagram', () => {
  it('renders a long call payoff with strike, break-even, max loss labels', () => {
    render(<PayoffDiagram contract={makeCall()} />);
    expect(screen.getByText(/Long call/i)).toBeInTheDocument();
    expect(screen.getByText('K=$350')).toBeInTheDocument();
    expect(screen.getByText('BE=$353.88')).toBeInTheDocument(); // 350 + 3.88
    expect(screen.getByText(/Max loss/i)).toBeInTheDocument();
    // Max loss for 1 contract = premium * 100 = $388
    expect(screen.getByText('-$388')).toBeInTheDocument();
    expect(screen.getByText(/profit above/i)).toBeInTheDocument();
  });

  it('renders a long put payoff with break-even = strike - premium', () => {
    render(<PayoffDiagram contract={makePut()} />);
    expect(screen.getByText(/Long put/i)).toBeInTheDocument();
    expect(screen.getByText('BE=$346.12')).toBeInTheDocument(); // 350 - 3.88
    expect(screen.getByText(/profit below/i)).toBeInTheDocument();
  });

  it('renders the current underlying marker when provided', () => {
    render(<PayoffDiagram contract={makeCall()} currentUnderlying={355} />);
    expect(screen.getByText('S₀=$355.00')).toBeInTheDocument();
  });

  it('scales max loss by contract count', () => {
    render(<PayoffDiagram contract={makeCall({ contracts: 3, max_risk_usd: '1164.00' })} />);
    expect(screen.getByText('-$1164')).toBeInTheDocument();
  });

  it('shows percent-from-spot hint on break-even when underlying is given', () => {
    render(<PayoffDiagram contract={makeCall()} currentUnderlying={300} />);
    // (353.88 - 300) / 300 * 100 = 17.96%
    expect(screen.getByText(/18\.0% from spot/i)).toBeInTheDocument();
  });
});
