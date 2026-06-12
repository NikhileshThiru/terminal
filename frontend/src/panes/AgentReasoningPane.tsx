import { useEffect, useRef } from 'react';

import { Pane } from '../chrome/Pane';
import { useReasoning, type ReasoningEvent } from '../chrome/ReasoningContext';

/**
 * Live stream of the agent's tool-calling loop. Subscribes to the shared
 * ReasoningContext, which a CopilotInputPane submit kicks off via SSE.
 * Each event renders as a single line — terminal aesthetic, dense and
 * scrolly, the way trading terminals stream the tape.
 */
export function AgentReasoningPane() {
  const { events, status, errorMessage, globalConnected } = useReasoning();
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll to bottom as new events arrive.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events.length]);

  return (
    <Pane
      title="Agent reasoning"
      subtitle={
        <span className="text-text-dim flex items-center gap-2 text-[10px]">
          <span className="flex items-center gap-1">
            <span
              className={`inline-block h-1.5 w-1.5 rounded-full ${
                globalConnected ? 'bg-accent animate-pulse' : 'bg-text-dim'
              }`}
            />
            {globalConnected ? 'broadcaster live' : 'disconnected'}
          </span>
          {status === 'streaming' && <span className="text-accent">· manual run</span>}
          {status === 'error' && <span className="text-error">· error</span>}
          {events.length > 0 && <span>· {events.length} events</span>}
        </span>
      }
      bodyClassName="p-0"
    >
      <div
        ref={scrollRef}
        className="h-full overflow-auto px-2 py-1 font-mono text-[11px] leading-relaxed"
      >
        {events.length === 0 ? (
          <p className="text-text-dim mt-4 px-2 text-center text-xs italic">
            No agent activity yet. As soon as the autonomous worker triages an event or you submit a
            manual thesis, every tool call + grounding step lands here in real time.
          </p>
        ) : (
          <ul className="space-y-0.5">
            {events.map((e) => (
              <EventRow key={e.id} evt={e} />
            ))}
            {status === 'error' && errorMessage && (
              <li className="border-error/40 bg-error/10 text-error mt-2 rounded border px-2 py-1">
                {errorMessage}
              </li>
            )}
          </ul>
        )}
      </div>
    </Pane>
  );
}

interface EventRowProps {
  evt: ReasoningEvent;
}

function EventRow({ evt }: EventRowProps) {
  const t = formatTime(evt.receivedAt);
  switch (evt.kind) {
    case 'started': {
      const { model, provider } = evt.payload as { model?: string; provider?: string };
      return (
        <li className="text-text-muted">
          <Time t={t} /> <Tag tone="neutral">START</Tag>{' '}
          <span className="text-text-dim">
            {provider}/{model}
          </span>
        </li>
      );
    }
    case 'thinking': {
      const { text } = evt.payload as { text?: string };
      return (
        <li className="text-text">
          <Time t={t} /> <Tag tone="neutral">THINK</Tag> <span className="italic">{text}</span>
        </li>
      );
    }
    case 'tool_call': {
      const { name, arguments: args } = evt.payload as {
        name?: string;
        arguments?: Record<string, unknown>;
      };
      return (
        <li>
          <Time t={t} /> <Tag tone="accent">CALL</Tag>{' '}
          <span className="text-accent font-semibold">{name}</span>
          <span className="text-text-dim">({summariseArgs(args)})</span>
        </li>
      );
    }
    case 'tool_result': {
      const { name, success, summary, error } = evt.payload as {
        name?: string;
        success?: boolean;
        summary?: string;
        error?: string;
      };
      return (
        <li className={success ? 'text-text-muted' : 'text-error'}>
          <Time t={t} /> <Tag tone={success ? 'ok' : 'error'}>RES </Tag>{' '}
          <span className="text-text">{name}</span> ·{' '}
          <span>{success ? summary : (error ?? 'failed')}</span>
        </li>
      );
    }
    case 'thesis_validating':
      return (
        <li className="text-text-muted">
          <Time t={t} /> <Tag tone="neutral">EMIT</Tag> validating final thesis…
        </li>
      );
    case 'thesis_accepted':
      return (
        <li className="text-up">
          <Time t={t} /> <Tag tone="ok">EMIT</Tag> thesis accepted
        </li>
      );
    case 'thesis_rejected': {
      const { error } = evt.payload as { error?: string };
      return (
        <li className="text-warning">
          <Time t={t} /> <Tag tone="warn">REJ </Tag>
          <span className="text-text-muted">{error}</span>
        </li>
      );
    }
    case 'grounding_check': {
      const { passed, unverified } = evt.payload as { passed?: boolean; unverified?: string[] };
      return (
        <li className={passed ? 'text-up' : 'text-warning'}>
          <Time t={t} /> <Tag tone={passed ? 'ok' : 'warn'}>GRND</Tag>{' '}
          {passed
            ? 'all numbers verified'
            : `unverified: ${(unverified ?? []).slice(0, 3).join(', ')}`}
        </li>
      );
    }
    case 'grounding_retry':
      return (
        <li className="text-text-muted">
          <Time t={t} /> <Tag tone="warn">RTRY</Tag> grounding retry
        </li>
      );
    case 'fallback_forced':
      return (
        <li className="text-warning">
          <Time t={t} /> <Tag tone="warn">FBCK</Tag> structured fallback forced
        </li>
      );
    case 'done':
      return (
        <li className="text-up font-semibold">
          <Time t={t} /> <Tag tone="ok">DONE</Tag> thesis complete
        </li>
      );
    case 'error': {
      const { message } = evt.payload as { message?: string };
      return (
        <li className="text-error">
          <Time t={t} /> <Tag tone="error">ERR </Tag> {message}
        </li>
      );
    }
  }
}

function Time({ t }: { t: string }) {
  return <span className="text-text-dim">[{t}]</span>;
}

function Tag({
  children,
  tone,
}: {
  children: string;
  tone: 'ok' | 'warn' | 'error' | 'accent' | 'neutral';
}) {
  const toneClass =
    tone === 'ok'
      ? 'bg-up/15 text-up border-up/40'
      : tone === 'warn'
        ? 'bg-warning/15 text-warning border-warning/40'
        : tone === 'error'
          ? 'bg-error/15 text-error border-error/40'
          : tone === 'accent'
            ? 'bg-accent/10 text-accent border-accent/30'
            : 'bg-bg-elevated-2 text-text-muted border-border';
  return (
    <span className={`rounded border px-1 py-0 font-mono text-[9px] tracking-wider ${toneClass}`}>
      {children}
    </span>
  );
}

function summariseArgs(args: Record<string, unknown> | undefined): string {
  if (!args || Object.keys(args).length === 0) return '';
  return Object.entries(args)
    .slice(0, 3)
    .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join(', ');
}

function formatTime(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number) => n.toString().padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
