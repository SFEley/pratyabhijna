"""Audit sub-agent: batch-evaluate graph nodes for hygiene issues."""
from __future__ import annotations

AUDIT_REVISION = 1
"""Bumped by hand when audit evaluation logic changes; nodes audited at lower
revisions get re-discovered by the audit-rediscovery query."""
