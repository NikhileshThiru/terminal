"""LLM provider abstraction (DESIGN.md §3).

Two cloud providers wired from day 1 (Gemini, Claude Haiku) to prove the
interface. Local Ollama fallback is documented but not implemented.
Provider selection is config-driven per pipeline step (triage / thesis).
"""
