"""Audit run output: JSON files in logs/audit/ + appended summary in memory/AUDIT.md."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

AUDIT_MD_HEADER = (
    "# Audit Log\n\n"
    "This file is appended to by `pratyabhijna audit` runs. "
    "Not loaded on bootstrap.\n\n"
)


def append_to_audit_md(*, repo_path: str | Path, run_summary: dict) -> None:
    """Append a dated section summarizing one audit run.

    Creates the file with a header if it doesn't exist; otherwise appends.
    The Unfixable subsection is omitted when there are no unfixable nodes.
    """
    audit_md = Path(repo_path) / "memory" / "AUDIT.md"
    if not audit_md.exists():
        audit_md.write_text(AUDIT_MD_HEADER)
    section = _format_section(run_summary)
    with audit_md.open("a") as f:
        f.write(section)


def _format_section(s: dict) -> str:
    lines = [
        f"\n## Run {s['started_at']}\n",
        f"- Valid: {s['valid']}",
        f"- Update: {s['update']}",
        f"- Unfixable: {s['unfixable']}",
        f"- Errored: {s['errored']}",
    ]
    if s["unfixable_details"]:
        lines.append("\n### Unfixable\n")
        for u in s["unfixable_details"]:
            lines.append(f"- **{u['name']}** (`{u['uuid']}`): {u['analysis']}")
    return "\n".join(lines) + "\n"


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
