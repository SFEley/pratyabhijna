"""Tests for audit_log.py: AUDIT.md appender + audit JSON writer."""
from pathlib import Path

from pratyabhijna.audit_log import append_to_audit_md


def test_append_creates_file_with_header_when_missing(tmp_path):
    repo = tmp_path
    (repo / "memory").mkdir()
    append_to_audit_md(
        repo_path=repo,
        run_summary={
            "started_at": "2026-04-26T12:00:00Z",
            "valid": 5, "update": 2, "unfixable": 1, "errored": 0,
            "unfixable_details": [
                {"uuid": "N1", "name": "x", "analysis": "weird"},
            ],
        },
    )
    text = (repo / "memory" / "AUDIT.md").read_text()
    # Header present (first time)
    assert "# Audit Log" in text
    # Run section present with timestamp + counts
    assert "2026-04-26T12:00:00Z" in text
    assert "Valid: 5" in text
    assert "Update: 2" in text
    assert "Unfixable: 1" in text
    assert "Errored: 0" in text
    # Unfixable details listed
    assert "N1" in text
    assert "weird" in text


def test_append_preserves_prior_content(tmp_path):
    repo = tmp_path
    (repo / "memory").mkdir()
    (repo / "memory" / "AUDIT.md").write_text("# Audit Log\n\nprior entry\n")
    append_to_audit_md(
        repo_path=repo,
        run_summary={
            "started_at": "2026-04-26T13:00:00Z",
            "valid": 0, "update": 0, "unfixable": 0, "errored": 0,
            "unfixable_details": [],
        },
    )
    text = (repo / "memory" / "AUDIT.md").read_text()
    assert "prior entry" in text
    assert "2026-04-26T13:00:00Z" in text


def test_append_omits_unfixable_section_when_empty(tmp_path):
    repo = tmp_path
    (repo / "memory").mkdir()
    append_to_audit_md(
        repo_path=repo,
        run_summary={
            "started_at": "2026-04-26T14:00:00Z",
            "valid": 10, "update": 0, "unfixable": 0, "errored": 0,
            "unfixable_details": [],
        },
    )
    text = (repo / "memory" / "AUDIT.md").read_text()
    # No Unfixable subsection when there are none
    assert "### Unfixable" not in text


import json
from pratyabhijna.audit_log import write_audit_file


def test_write_audit_file_creates_dated_json(tmp_path):
    log_dir = tmp_path
    path = write_audit_file(
        log_dir=log_dir,
        run_metadata={
            "started_at": "2026-04-26T12:00:00Z",
            "completed_at": "2026-04-26T12:05:00Z",
            "audit_revision": 1,
            "guidance": None,
            "model": "claude-sonnet-4-6",
            "cohort_size": 2,
        },
        results=[
            {"uuid": "N1", "name": "x", "status": "Valid", "analysis": "ok"},
            {
                "uuid": "N2", "name": "y", "status": "Update",
                "analysis": "wrong type",
                "request": "Change type to Observation",
            },
        ],
    )
    assert path.parent.name == "audit"
    assert path.parent.parent == log_dir
    assert path.name.startswith("audit-")
    assert path.name.endswith(".json")
    # File-safe timestamp: colons replaced with dashes
    assert ":" not in path.name

    data = json.loads(path.read_text())
    assert data["run_metadata"]["audit_revision"] == 1
    assert data["run_metadata"]["model"] == "claude-sonnet-4-6"
    assert len(data["results"]) == 2
    assert data["results"][0]["uuid"] == "N1"
    assert data["results"][1]["request"] == "Change type to Observation"


def test_write_audit_file_creates_directory_if_missing(tmp_path):
    """log_dir/audit/ should be created on demand."""
    log_dir = tmp_path / "fresh_logs"
    path = write_audit_file(
        log_dir=log_dir,
        run_metadata={"started_at": "2026-04-26T12:00:00Z"},
        results=[],
    )
    assert path.exists()
    assert path.parent == log_dir / "audit"
