"""LLM provider abstraction (DESIGN.md §3).

Two free cloud providers (Gemini, Groq) behind one `LLMProvider` Protocol.
Each pipeline step (triage / thesis) gets an `LLMRouter` that walks an ordered,
config-driven list of provider:model routes, parks rate-limited routes in a
cooldown, and logs every call to the `llm_calls` table. No Anthropic provider
by design (no free tier); no local Ollama (cut — see README).
"""
