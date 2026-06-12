import { useNavigation } from '../chrome/NavigationContext';
import { useReasoning } from '../chrome/ReasoningContext';

/**
 * Slim agent-activity indicator. Replaces the full Agent Reasoning pane on
 * the Dashboard — the dashboard is autonomous-centric (live theses, news,
 * ticker info); the live tool-call stream is more interesting on the
 * Manual Copilot page where you submitted the thesis yourself.
 *
 * Click → jumps to the Copilot page where the full stream lives.
 */
export function AgentActivityIndicator() {
  const { events, status, globalConnected } = useReasoning();
  const { setPage } = useNavigation();
  const latest = events[events.length - 1];

  return (
    <button
      type="button"
      onClick={() => setPage('copilot')}
      className="border-border bg-bg-elevated hover:bg-bg-elevated-2 flex w-full cursor-pointer items-center gap-3 rounded border px-3 py-2 text-left"
      title="Open Manual Copilot for the full agent-reasoning stream"
    >
      <span
        className={`inline-block h-2 w-2 rounded-full ${
          globalConnected ? 'bg-accent animate-pulse' : 'bg-text-dim'
        }`}
      />
      <div className="flex flex-1 items-baseline justify-between gap-2 overflow-hidden">
        <span className="text-text-muted shrink-0 font-mono text-[10px] tracking-wider uppercase">
          Agent
        </span>
        <span className="flex-1 truncate font-mono text-[11px]">
          {status === 'streaming' ? (
            <span className="text-accent">manual run streaming…</span>
          ) : latest ? (
            <LatestEventDescription />
          ) : globalConnected ? (
            <span className="text-text-dim italic">idle · waiting for autonomous activity</span>
          ) : (
            <span className="text-text-dim italic">broadcaster disconnected</span>
          )}
        </span>
        <span className="text-text-dim shrink-0 text-[10px]">
          {events.length > 0 && `${events.length} events`}
        </span>
        <span className="text-text-dim shrink-0 text-[10px]" title="Open Manual Copilot page">
          →
        </span>
      </div>
    </button>
  );
}

function LatestEventDescription() {
  const { events } = useReasoning();
  const latest = events[events.length - 1];
  if (!latest) return null;
  const kindLabel = describeKind(latest.kind);
  return (
    <span className="text-text-muted">
      latest: <span className="text-text-dim">{kindLabel}</span>
    </span>
  );
}

function describeKind(kind: string): string {
  switch (kind) {
    case 'started':
      return 'agent started';
    case 'thinking':
      return 'agent thinking';
    case 'tool_call':
      return 'calling a research tool';
    case 'tool_result':
      return 'tool result';
    case 'thesis_validating':
      return 'validating thesis';
    case 'thesis_accepted':
      return 'thesis accepted';
    case 'thesis_rejected':
      return 'thesis rejected';
    case 'grounding_check':
      return 'grounding check';
    case 'grounding_retry':
      return 'grounding retry';
    case 'fallback_forced':
      return 'fallback forced';
    case 'done':
      return 'agent done';
    case 'error':
      return 'agent error';
    default:
      return kind;
  }
}
