import '@testing-library/jest-dom/vitest';
import { vi } from 'vitest';

// lightweight-charts needs Canvas + matchMedia + ResizeObserver, none of which
// jsdom ships. Component-level chart behavior is tested separately via the
// real DOM; mock the module here so App-level integration tests can mount
// the grid without dragging a canvas backend into jsdom.
// jsdom doesn't ship EventSource; the WatchlistPane uses it for the live
// price stream. A no-op stub is enough for component tests — real wire
// behavior is verified manually in the browser smoke.
if (typeof globalThis.EventSource === 'undefined') {
  class EventSourceStub {
    onmessage: ((ev: MessageEvent) => void) | null = null;
    onerror: ((ev: Event) => void) | null = null;
    onopen: ((ev: Event) => void) | null = null;
    readyState = 0;
    addEventListener(): void {}
    removeEventListener(): void {}
    close(): void {}
  }
  // @ts-expect-error stub
  globalThis.EventSource = EventSourceStub;
}

vi.mock('lightweight-charts', () => {
  const series = { setData: vi.fn(), update: vi.fn(), applyOptions: vi.fn() };
  const timeScale = {
    fitContent: vi.fn(),
    timeToCoordinate: vi.fn(() => null),
    subscribeVisibleLogicalRangeChange: vi.fn(),
    unsubscribeVisibleLogicalRangeChange: vi.fn(),
  };
  const priceScale = { applyOptions: vi.fn() };
  const chart = {
    addAreaSeries: () => series,
    addLineSeries: () => series,
    addCandlestickSeries: () => series,
    addHistogramSeries: () => series,
    timeScale: () => timeScale,
    priceScale: () => priceScale,
    subscribeCrosshairMove: vi.fn(),
    unsubscribeCrosshairMove: vi.fn(),
    remove: vi.fn(),
    resize: vi.fn(),
    applyOptions: vi.fn(),
  };
  return {
    createChart: vi.fn(() => chart),
    LineStyle: { Solid: 0, Dotted: 1, Dashed: 2 },
  };
});
