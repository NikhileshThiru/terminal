import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';

import { streamThesis, type CopilotStreamEvent, type Thesis, type ThesisRequest } from '../lib/api';

export interface ReasoningEvent extends CopilotStreamEvent {
  /** Local sequence id — needed for stable React keys when the same
   * event kind fires multiple times. */
  id: number;
  /** Wall-clock when the event landed in the browser. */
  receivedAt: number;
  /** "manual" if this came from the user's own copilot run; "global" if
   * it came from the broadcaster (autonomous reactive/catalyst). */
  origin: 'manual' | 'global';
}

export type ReasoningStatus = 'idle' | 'streaming' | 'done' | 'error';

interface ReasoningApi {
  events: ReasoningEvent[];
  status: ReasoningStatus;
  /** Last `error` event payload, if any. */
  errorMessage: string | null;
  /** Final thesis if a manual run completed cleanly. */
  thesis: Thesis | null;
  /** Start a manual streaming run. Returns the final thesis (or null on error). */
  startStream: (req: ThesisRequest) => Promise<Thesis | null>;
  /** Clear manual-run state. Does not affect the global event log. */
  reset: () => void;
  /** True if the global broadcaster SSE is connected. */
  globalConnected: boolean;
}

const Ctx = createContext<ReasoningApi | null>(null);

const API_URL = import.meta.env.VITE_API_URL ?? 'http://localhost:8000';
const MAX_EVENTS = 200;

export function ReasoningProvider({ children }: { children: ReactNode }) {
  const [events, setEvents] = useState<ReasoningEvent[]>([]);
  const [status, setStatus] = useState<ReasoningStatus>('idle');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [thesis, setThesis] = useState<Thesis | null>(null);
  const [globalConnected, setGlobalConnected] = useState(false);
  const seqRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  const append = useCallback((evt: CopilotStreamEvent, origin: 'manual' | 'global') => {
    seqRef.current += 1;
    const id = seqRef.current;
    setEvents((prev) => {
      const next = [...prev, { ...evt, id, receivedAt: Date.now(), origin }];
      // Keep a rolling buffer so the log doesn't grow unbounded over a
      // long autonomous run.
      return next.length > MAX_EVENTS ? next.slice(-MAX_EVENTS) : next;
    });
  }, []);

  // Subscribe to the global broadcaster as soon as the provider mounts.
  // Stays open for the life of the app; SSE reconnects automatically.
  useEffect(() => {
    const es = new EventSource(`${API_URL}/agent/events/stream`);
    setGlobalConnected(false);

    const onAny = (e: MessageEvent<string>, kind: string) => {
      try {
        const payload = JSON.parse(e.data) as Record<string, unknown>;
        append({ kind: kind as CopilotStreamEvent['kind'], payload }, 'global');
      } catch {
        /* skip malformed */
      }
    };

    const kinds: CopilotStreamEvent['kind'][] = [
      'started',
      'thinking',
      'tool_call',
      'tool_result',
      'thesis_validating',
      'thesis_rejected',
      'thesis_accepted',
      'grounding_check',
      'grounding_retry',
      'fallback_forced',
      'done',
      'error',
    ];
    const handlers = kinds.map((k) => {
      const fn = (e: MessageEvent<string>) => onAny(e, k);
      es.addEventListener(k, fn as EventListener);
      return [k, fn] as const;
    });
    es.addEventListener('hello', () => setGlobalConnected(true));
    es.addEventListener('tick', () => {
      /* keep-alive only */
    });

    es.onerror = () => setGlobalConnected(false);

    return () => {
      for (const [k, fn] of handlers) es.removeEventListener(k, fn as EventListener);
      es.close();
    };
  }, [append]);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setEvents([]);
    setThesis(null);
    setErrorMessage(null);
    setStatus('idle');
  }, []);

  const startStream = useCallback(
    async (req: ThesisRequest): Promise<Thesis | null> => {
      abortRef.current?.abort();
      const ac = new AbortController();
      abortRef.current = ac;
      // Don't clear `events` — autonomous activity should keep streaming
      // even while a manual run is in flight; visually they share the log.
      setThesis(null);
      setErrorMessage(null);
      setStatus('streaming');
      try {
        let finalThesis: Thesis | null = null;
        for await (const evt of streamThesis(req, ac.signal)) {
          append(evt, 'manual');
          if (evt.kind === 'done' && evt.payload.thesis) {
            finalThesis = evt.payload.thesis as Thesis;
            setThesis(finalThesis);
          }
          if (evt.kind === 'error') {
            const msg = String(evt.payload.message ?? 'unknown error');
            setErrorMessage(msg);
          }
        }
        if (finalThesis) {
          setStatus('done');
        } else if (ac.signal.aborted) {
          setStatus('idle');
        } else {
          setStatus('error');
          if (!errorMessage) setErrorMessage('stream ended without a thesis');
        }
        return finalThesis;
      } catch (e) {
        if (ac.signal.aborted) {
          setStatus('idle');
          return null;
        }
        const msg = e instanceof Error ? e.message : String(e);
        setErrorMessage(msg);
        setStatus('error');
        return null;
      }
    },
    [append, errorMessage],
  );

  const api = useMemo<ReasoningApi>(
    () => ({ events, status, errorMessage, thesis, startStream, reset, globalConnected }),
    [events, status, errorMessage, thesis, startStream, reset, globalConnected],
  );
  return <Ctx.Provider value={api}>{children}</Ctx.Provider>;
}

export function useReasoning(): ReasoningApi {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('useReasoning must be used inside <ReasoningProvider>');
  return ctx;
}
