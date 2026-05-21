"""Prompt templates for the extract and reconcile stages.

Layered for prompt caching: system (stable across all episodes) → per-session
(stable across a saga) → previous-episode context (changes per session) →
the episode itself.

Implemented across Tasks 7, 11, 13.
"""
