import { ThesisDisplay } from '../ThesisDisplay';
import type { RecentThesis, Thesis } from '../lib/api';

/**
 * Renders a compact /autonomous/theses row as the full thesis view. The
 * compact endpoint returns a subset of fields, so we synthesise the
 * modal-friendly object from what we have; rows without a suggested
 * contract fall back to a text summary.
 */
export function RecentThesisModal({ recent }: { recent: RecentThesis }) {
  const full: Thesis | null = recent.suggested_contract
    ? ({
        symbol: recent.symbol,
        direction: recent.direction,
        confidence: recent.confidence,
        reasoning: recent.reasoning,
        prediction_window_days: 0,
        suggested_contract: recent.suggested_contract as unknown as Thesis['suggested_contract'],
        what_must_happen: '',
        correlation_id: recent.correlation_id,
        source_bucket: recent.source_bucket,
        generated_at: recent.generated_at,
        grounding_check_passed: recent.grounding_check_passed,
        grounding_notes: null,
        llm_provider: recent.llm_provider,
        llm_model: recent.llm_model,
        funnel_latency_ms: recent.funnel_latency_ms ?? 0,
      } as Thesis)
    : null;
  if (full) return <ThesisDisplay thesis={full} />;
  return (
    <div className="text-text-muted space-y-3 font-mono text-xs leading-relaxed">
      <div className="text-text text-sm font-bold tracking-wider">
        {recent.symbol} · {recent.direction.toUpperCase()} · {(recent.confidence * 100).toFixed(0)}%
      </div>
      <p>{recent.reasoning}</p>
      <p className="text-text-dim">
        No suggested contract was recorded for this thesis — likely the agent emitted a directional
        view without committing to a specific options leg.
      </p>
    </div>
  );
}
