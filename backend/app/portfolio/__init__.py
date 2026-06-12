"""Paper-trading portfolio layer (DESIGN.md §8).

Phase 7 MVP: shadow mode only. Theses pass through a deterministic risk
engine; approved trades are recorded as ShadowTrade rows. No real position
tracking, no mark-to-market — those land in a follow-up session along with
the graduation criteria to move from shadow to actual paper trading.
"""
