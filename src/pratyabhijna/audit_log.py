"""Audit run output: JSON files in logs/audit/."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def write_audit_file(
    *,
    log_dir: str | Path,
    run_metadata: dict,
    results: list[dict],
) -> Path:
    """Write `logs/audit/audit-{ts}.json` and return its path.

    Creates `log_dir/audit/` if it doesn't exist. The timestamp in the filename
    comes from `run_metadata["started_at"]` (file-safe — colons replaced with
    dashes); falls back to the current UTC time if absent.
    """
    out_dir = Path(log_dir) / "audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = run_metadata.get("started_at") or datetime.now(timezone.utc).isoformat()
    safe_ts = ts.replace(":", "-")
    path = out_dir / f"audit-{safe_ts}.json"
    payload = {"run_metadata": run_metadata, "results": results}
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path
