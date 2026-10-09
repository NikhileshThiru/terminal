# ADR-0006: Route every LLM step through an ordered, quota-aware failover chain

- **Date:** 2026-10-09
- **Status:** accepted
- **Phase:** 8 (hardening for continuous operation)

## Context

Each pipeline step (triage, thesis) originally had exactly one provider and
one model, picked by config. Three things broke that in practice:

- **Daily quotas run out mid-session.** On 2026-07-07 both Gemini and Groq
  daily caps were exhausted by mid-afternoon; every later event burned three
  backoff retries on a dead quota, failed, and vanished from the UI.
- **Free quotas are small and per model.** Verified 2026-10-09: Gemini's
  free tier allows ~20 requests/day *per model* (2.5-flash-lite 429'd with a
  `PerDay` quota after exactly 20 calls); Groq allows 1,000 requests/day and
  8K tokens/minute *per model*. One thesis is 5-10 calls.
- **Models disappear.** Groq retired `llama-3.3-70b-versatile` (404) — the
  configured triage model — while the stack was idle, so every triage call
  would have failed on restart.

The project's rule is free-forever (no paid tier, no Anthropic API), so the
answer can't be "buy more quota."

## Decision

Each step gets an `LLMRouter` (`app/llm/router.py`) that implements the same
`LLMProvider` Protocol and walks an ordered, config-driven list of
`provider:model` routes. A shared health registry (`app/llm/health.py`,
keyed by provider *and* model) parks a route after a quota error for exactly
as long as the upstream says (`retryDelay`, `Retry-After`, "try again in
15m29s"; Gemini daily quotas until midnight Pacific), parks retired models
and bad keys for an hour, and lets generic failures fall through to the next
route without a cooldown. Providers classify 429s themselves: a short
per-minute limit is waited out inline; a daily limit raises immediately so
the router can fail over. Every attempt is written to an `llm_calls` table
(step, route, latency, tokens, outcome). When every route is parked, the
reactive runner records a visible "skipped: quota" decision instead of
dropping the event silently, and a daily reactive-thesis cap reserves
thesis quota for the catalyst path.

Chains (config defaults): triage `groq:gpt-oss-20b → groq:gpt-oss-120b →
gemini:3.1-flash-lite`; thesis `gemini:2.5-flash → 3.5-flash → 3.7-flash → 3.6-flash →
3-flash-preview → 3.8-flash → 2.5-flash-lite` (seven separate daily quotas;
the first live morning exhausted three of an original four by 09:40). Groq is not in the thesis chain: one multi-tool thesis
request exceeds its 8K tokens/minute cap. Triage's Gemini fallback is a
model the thesis chain doesn't use, so triage can never starve theses.

## Alternatives considered

- **Retry harder on the same model.** Rejected: a daily quota doesn't come
  back in 56 seconds of backoff; it's the July failure mode, made slower.
- **Local Ollama as the last resort.** Rejected for this project: a
  multi-GB model download on the dev Mac for a path the multi-model cloud
  chain already covers, with markedly worse thesis quality.
- **One router for the whole app.** Rejected: triage and thesis have
  different cost profiles (one short call vs. a long tool-calling
  conversation) and different viable providers.
- **Pay for a tier.** Violates principle 1 (free forever).

## Consequences

- **Positive:** quota exhaustion degrades capacity instead of breaking the
  pipeline; a retired model costs one failed call, not a dead pipeline; the
  header shows which routes are parked and until when; `llm_calls` turns
  "when did we hit the cap?" into one query.
- **Negative:** theses can come from different models on the same day (each
  thesis records the model that actually served its final turn, so the eval
  can be sliced by model). Gemini 3 needs `thought_signature` echoed on
  tool-calling turns — including a documented skip sentinel when a
  conversation fails over between Gemini models mid-thesis — which ties the
  provider-neutral message type to one provider's quirk via an opaque field.
- **Reversibility:** High. Set the fallback lists to `[]` and each step is a
  single route again.

## References

- Related: [[ADR-0002]] (data-source resilience — same idea, one layer down).
- `app/llm/router.py`, `app/llm/health.py`, `app/llm/quota.py`, `app/llm/usage_log.py`.
