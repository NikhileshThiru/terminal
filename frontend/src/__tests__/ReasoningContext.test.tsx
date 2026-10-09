import { act, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ReasoningProvider, useReasoning } from '../chrome/ReasoningContext';

/** Captures the provider's broadcaster subscription so the test can push events. */
class FakeEventSource {
  static last: FakeEventSource | null = null;
  listeners = new Map<string, (e: MessageEvent<string>) => void>();
  onerror: (() => void) | null = null;
  constructor() {
    FakeEventSource.last = this;
  }
  addEventListener(kind: string, fn: (e: MessageEvent<string>) => void) {
    this.listeners.set(kind, fn);
  }
  removeEventListener() {}
  close() {}
  emit(kind: string, payload: unknown) {
    this.listeners.get(kind)?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
  }
}

function Count() {
  const { events } = useReasoning();
  return <span data-testid="count">{events.length}</span>;
}

afterEach(() => vi.unstubAllGlobals());

describe('ReasoningProvider', () => {
  it('renders each step of a run once even when it arrives twice', () => {
    vi.stubGlobal('EventSource', FakeEventSource);
    render(
      <ReasoningProvider>
        <Count />
      </ReasoningProvider>,
    );
    const es = FakeEventSource.last!;
    act(() => {
      es.emit('started', { run_id: 'r1', seq: 1 });
      es.emit('started', { run_id: 'r1', seq: 1 }); // the duplicate copy
      es.emit('tool_call', { run_id: 'r1', seq: 2, name: 'get_quote' });
      es.emit('started', { run_id: 'r2', seq: 1 }); // a different run
    });
    expect(screen.getByTestId('count').textContent).toBe('3');
  });
});
