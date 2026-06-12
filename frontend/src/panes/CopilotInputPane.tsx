import { useEffect, useState } from 'react';

import { ThesisDisplay } from '../ThesisDisplay';
import { useModal } from '../chrome/ModalContext';
import { Pane } from '../chrome/Pane';
import { useReasoning } from '../chrome/ReasoningContext';
import { useSelection } from '../chrome/SelectionContext';

/**
 * Copilot input. Submitting kicks off an SSE-streamed run; the
 * AgentReasoningPane shows tool calls + thinking as they arrive. When the
 * final thesis lands, we open it in a modal so the result gets full real
 * estate to render the payoff diagram.
 */
export function CopilotInputPane() {
  const modal = useModal();
  const reasoning = useReasoning();
  const { selectedSymbol } = useSelection();
  const [input, setInput] = useState('');
  const [budget, setBudget] = useState('500');
  const [openedThesisId, setOpenedThesisId] = useState<string | null>(null);

  // When a thesis lands cleanly, open it in the modal (idempotent — only open once per run).
  useEffect(() => {
    if (
      reasoning.status === 'done' &&
      reasoning.thesis &&
      reasoning.thesis.correlation_id !== openedThesisId
    ) {
      setOpenedThesisId(reasoning.thesis.correlation_id);
      modal.open({
        title: `Thesis · ${reasoning.thesis.symbol}`,
        content: <ThesisDisplay thesis={reasoning.thesis} />,
      });
    }
  }, [reasoning.status, reasoning.thesis, openedThesisId, modal]);

  async function submit() {
    if (input.trim().length < 10 || reasoning.status === 'streaming') return;
    const parsed = parseFloat(budget);
    await reasoning.startStream({
      user_thesis: input.trim(),
      risk_budget_usd: Number.isFinite(parsed) && parsed > 0 ? parsed : null,
    });
  }

  const isStreaming = reasoning.status === 'streaming';
  const canSubmit = input.trim().length >= 10 && !isStreaming;

  const presets = [
    `${selectedSymbol} beats earnings this quarter`,
    `${selectedSymbol} sells off 5% in the next two weeks`,
    `${selectedSymbol} drifts up into its next earnings date`,
  ];

  return (
    <Pane title="Thesis idea">
      <div className="flex h-full flex-col gap-2 p-1">
        <textarea
          id="copilot-textarea"
          aria-label="Thesis idea"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) void submit();
          }}
          placeholder={`e.g. ${selectedSymbol} beats earnings tonight, $500 to risk`}
          disabled={isStreaming}
          className="bg-bg border-border-strong text-text focus:border-accent flex-1 resize-none rounded border p-2 font-mono text-xs leading-relaxed outline-none"
        />
        <div className="flex flex-wrap items-center gap-1">
          <span className="text-text-dim text-[9px] tracking-wider uppercase">try:</span>
          {presets.map((p) => (
            <button
              key={p}
              type="button"
              disabled={isStreaming}
              onClick={() => setInput(p)}
              className="text-text-muted hover:text-text border-border hover:border-border-strong cursor-pointer truncate rounded border px-1.5 py-0.5 font-mono text-[9px] transition-colors disabled:opacity-50"
            >
              {p}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <label className="text-text-muted flex items-center gap-1 text-[10px]">
            <span className="text-text-dim text-[9px] tracking-wider uppercase">risk $</span>
            <input
              type="number"
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
              min="50"
              step="50"
              disabled={isStreaming}
              className="bg-bg border-border-strong text-text w-20 rounded border px-1.5 py-1 font-mono text-xs"
            />
          </label>
          {(reasoning.status === 'done' || reasoning.status === 'error') && (
            <button
              type="button"
              onClick={reasoning.reset}
              className="border-border-strong text-text-muted hover:text-text hover:border-text-muted cursor-pointer rounded border px-2 py-1 font-mono text-[10px] tracking-wider uppercase"
            >
              clear
            </button>
          )}
          <button
            type="button"
            onClick={() => void submit()}
            disabled={!canSubmit}
            className={`bg-accent hover:bg-accent/90 ml-auto cursor-pointer rounded px-4 py-1.5 font-mono text-xs font-bold text-black transition-colors disabled:cursor-not-allowed disabled:bg-zinc-800 disabled:text-zinc-500 ${
              isStreaming ? 'animate-pulse' : ''
            }`}
          >
            {isStreaming ? 'streaming…' : 'RUN ▸'}
          </button>
        </div>
        {isStreaming && (
          <p className="text-text-dim text-[10px] italic">
            watch the Agent Reasoning pane for live tool calls
          </p>
        )}
        {reasoning.status === 'error' && reasoning.errorMessage && (
          <p className="border-error/40 bg-error/10 text-error rounded border px-2 py-1 text-[10px]">
            {reasoning.errorMessage}
          </p>
        )}
      </div>
    </Pane>
  );
}
