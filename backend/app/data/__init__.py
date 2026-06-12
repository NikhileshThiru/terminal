"""Data-access abstraction layer (DESIGN.md §7).

Every external source sits behind an interface here. Swapping a source or
adding a fallback must not touch callers.
"""
