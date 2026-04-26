"""Tests for audit_log.py: audit JSON writer."""
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
