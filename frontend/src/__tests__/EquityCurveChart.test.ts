import { describe, expect, it } from 'vitest';

import { maxDrawdownOf } from '../EquityCurveChart';

describe('maxDrawdownOf', () => {
  it('measures drawdown % against the peak it fell from, not the later global peak', () => {
    // 100 -> 50 is a 50% drawdown, even though equity later reaches 200.
    const { maxDrawdown, maxDrawdownPct } = maxDrawdownOf([100, 50, 200]);
    expect(maxDrawdown).toBe(-50);
    expect(maxDrawdownPct).toBeCloseTo(-50);
  });

  it('is zero for a monotonically rising curve', () => {
    expect(maxDrawdownOf([1, 2, 3])).toEqual({ maxDrawdown: 0, maxDrawdownPct: 0 });
  });
});
