"""Tests for the JSON output-file writer used by ``pratyabhijna update``.

The writer wraps a list of per-update dicts into the canonical output
shape and writes it as ``output-{ISO8601-UTC}.json`` under
``{log_dir}/update/``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture
def updates_one():
    """One per-update dict in the shape produced by tools.update.update()."""
    return [{
        "status": "Updated",
        "request": "Fill missing bar on Foo nodes",
        "response": "Filled bar on 5 Foo nodes.",
        "guids": None,
        "count": 5,
        "queries": [
            {"turn": 1, "mode": "write", "thinking": [],
             "cypher": "MATCH (n:Foo) WHERE n.bar IS NULL SET n.bar = 'x'",
             "cypher_output": [{"properties_set": 5}]},
        ],
        "warnings": [],
        "errors": [],
    }]


def test_writes_output_file_to_update_subdir(tmp_path, updates_one):
    from pratyabhijna.output_log import write_output_file

    started = datetime(2026, 4, 25, 18, 30, 0, tzinfo=timezone.utc)
    completed = datetime(2026, 4, 25, 18, 30, 14, tzinfo=timezone.utc)

    path = write_output_file(
        log_dir=tmp_path,
        group_id="Vesper",
        started_at=started,
        completed_at=completed,
        cache_requested=False,
        updates=updates_one,
    )

    assert path.exists()
    assert path.parent == tmp_path / "update"
    assert path.name.startswith("output-")
    assert path.name.endswith(".json")


def test_creates_update_dir_if_missing(tmp_path, updates_one):
    from pratyabhijna.output_log import write_output_file

    log_dir = tmp_path / "fresh"  # does not exist yet
    started = datetime(2026, 4, 25, 18, 30, 0, tzinfo=timezone.utc)
    completed = datetime(2026, 4, 25, 18, 30, 14, tzinfo=timezone.utc)

    write_output_file(
        log_dir=log_dir,
        group_id="Vesper",
        started_at=started,
        completed_at=completed,
        cache_requested=False,
        updates=updates_one,
    )

    assert (log_dir / "update").is_dir()


def test_filename_uses_utc_iso8601(tmp_path, updates_one):
    from pratyabhijna.output_log import write_output_file

    started = datetime(2026, 4, 25, 18, 30, 0, tzinfo=timezone.utc)
    completed = datetime(2026, 4, 25, 18, 30, 14, tzinfo=timezone.utc)

    path = write_output_file(
        log_dir=tmp_path,
        group_id="Vesper",
        started_at=started,
        completed_at=completed,
        cache_requested=False,
        updates=updates_one,
    )

    # Filename contains the started_at as ISO8601 UTC with Z suffix
    # Colons in filenames are unfriendly on some filesystems — substitute.
    name = path.name
    assert "2026-04-25T18-30-00Z" in name or "2026-04-25T18:30:00Z" in name


def test_contents_have_top_level_fields(tmp_path, updates_one):
    from pratyabhijna.output_log import write_output_file

    started = datetime(2026, 4, 25, 18, 30, 0, tzinfo=timezone.utc)
    completed = datetime(2026, 4, 25, 18, 30, 14, tzinfo=timezone.utc)

    path = write_output_file(
        log_dir=tmp_path,
        group_id="Vesper",
        started_at=started,
        completed_at=completed,
        cache_requested=False,
        updates=updates_one,
    )

    data = json.loads(path.read_text())
    assert data["group_id"] == "Vesper"
    assert data["started_at"] == "2026-04-25T18:30:00+00:00"
    assert data["completed_at"] == "2026-04-25T18:30:14+00:00"
    assert data["duration_ms"] == 14000
    assert data["cache"] == {"requested": False}
    assert data["bootstrap"] == {"tiers_loaded": ["soul", "identity"]}
    assert data["thinking_config"] == {"type": "adaptive", "display": "summarized"}
    # Version comes from package metadata
    from importlib.metadata import version as _pkg_version
    assert data["pratyabhijna_version"] == _pkg_version("pratyabhijna")


def test_contents_include_updates_array(tmp_path, updates_one):
    from pratyabhijna.output_log import write_output_file

    started = datetime(2026, 4, 25, 18, 30, 0, tzinfo=timezone.utc)
    completed = datetime(2026, 4, 25, 18, 30, 14, tzinfo=timezone.utc)

    path = write_output_file(
        log_dir=tmp_path,
        group_id="Vesper",
        started_at=started,
        completed_at=completed,
        cache_requested=False,
        updates=updates_one,
    )

    data = json.loads(path.read_text())
    assert data["updates"] == updates_one


def test_cache_requested_true_records_true(tmp_path, updates_one):
    from pratyabhijna.output_log import write_output_file

    started = datetime(2026, 4, 25, 18, 30, 0, tzinfo=timezone.utc)
    completed = datetime(2026, 4, 25, 18, 30, 14, tzinfo=timezone.utc)

    path = write_output_file(
        log_dir=tmp_path,
        group_id="Vesper",
        started_at=started,
        completed_at=completed,
        cache_requested=True,
        updates=updates_one,
    )

    data = json.loads(path.read_text())
    assert data["cache"]["requested"] is True


def test_naive_datetime_raises_value_error(tmp_path, updates_one):
    """All timestamps must be UTC-aware. Naive datetimes are an error."""
    from pratyabhijna.output_log import write_output_file

    started = datetime(2026, 4, 25, 18, 30, 0)  # naive!
    completed = datetime(2026, 4, 25, 18, 30, 14, tzinfo=timezone.utc)

    with pytest.raises(ValueError):
        write_output_file(
            log_dir=tmp_path,
            group_id="Vesper",
            started_at=started,
            completed_at=completed,
            cache_requested=False,
            updates=updates_one,
        )
