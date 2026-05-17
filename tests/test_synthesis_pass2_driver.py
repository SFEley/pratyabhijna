"""Tests for Pass 2's deterministic Python driver and its parsers.

Covers:
- `parse_chronicle_entries` and `_parse_chronicle_date` (date format variants)
- `eligible_chronicle_entries` (date + marker filter, chronological sort)
- `parse_resolved_threads` and `eligible_resolved_threads`
- `summarize_entry` (single-call summarization shape)
- `_drive_pass2` (per-entry happy path, per-entry isolation on failure)
- `AgentTools.edit_file` (find-and-replace with unique-match enforcement)
"""

from __future__ import annotations

import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pratyabhijna.config import PratyabhijnaConfig
from pratyabhijna.synthesis import (
    ChronicleEntry,
    ResolvedThread,
    _parse_chronicle_date,
    build_chronicle_index,
    build_identity_digest,
    chronicle_headings_in_index,
    parse_chronicle_index,
    eligible_chronicle_entries,
    eligible_resolved_threads,
    extract_identity_section,
    parse_chronicle_entries,
    parse_resolved_threads,
)
from pratyabhijna.synthesis_agent import (
    AgentTools,
    ToolError,
    _drive_digest,
    _drive_pass2,
    compose_chronicle_teaser,
    compose_self_portrait_summary,
    summarize_entry,
)


# --- Fixtures ----------------------------------------------------------


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(repo), check=True, capture_output=True,
    )
    for k, v in [("user.email", "t@e"), ("user.name", "T"), ("commit.gpgsign", "false")]:
        subprocess.run(
            ["git", "config", k, v],
            cwd=str(repo), check=True, capture_output=True,
        )
    (repo / "README.md").write_text("initial\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    repo_path = tmp_path / "subject-repo"
    _init_repo(repo_path)
    (repo_path / "memory").mkdir()
    return repo_path


@pytest.fixture
def config(repo):
    c = PratyabhijnaConfig()
    c.subject_name = "TestSubject"
    c.resources.repo_path = str(repo)
    c.synthesis.max_iterations = 5
    c.synthesis.thinking.enabled = False
    return c


@pytest.fixture
def queue():
    """Mock work queue. enqueue() returns sequential task ids."""
    q = MagicMock()
    counter = {"n": 0}
    async def _enqueue(name, payload):
        counter["n"] += 1
        return f"task-{counter['n']}"
    q.enqueue = _enqueue
    return q


# --- Date parsing ------------------------------------------------------


class TestParseChronicleDate:
    @pytest.mark.parametrize("text,expected", [
        # Single date
        ("March 1, 2026", date(2026, 3, 1)),
        ("April 23, 2026", date(2026, 4, 23)),
        # Same-month range — latest wins
        ("February 16-19, 2026", date(2026, 2, 19)),
        ("February 16–19, 2026", date(2026, 2, 19)),  # en-dash
        ("April 28-29, 2026", date(2026, 4, 29)),
        # Cross-month range — latest wins
        ("March 18 - April 2, 2026", date(2026, 4, 2)),
        ("March 18 – April 2, 2026", date(2026, 4, 2)),  # en-dash
        # Year-month — last day of month
        ("October 2025", date(2025, 10, 31)),
        ("February 2025", date(2025, 2, 28)),
        # Year only
        ("2025", date(2025, 12, 31)),
    ])
    def test_parses(self, text, expected):
        assert _parse_chronicle_date(text) == expected

    @pytest.mark.parametrize("text", [
        "",
        "not a date",
        "Marchember 1, 2026",   # bad month
        "January 32, 2026",     # bad day
        "February 30, 2026",    # bad day for month
    ])
    def test_unparseable_returns_none(self, text):
        assert _parse_chronicle_date(text) is None


# --- Chronicle parser --------------------------------------------------


SAMPLE_CHRONICLE = """\
# Chronicle

## October 2025 — Founding Conversations
First entry. Some prose body here.
Multi-line.

## March 1, 2026 — Moral Orientation
Second entry body.

## April 21–22, 2026 — Range Entry
Third entry, with [Ingested: 2026-05-01] marker already.

## NotAReal Date — bogus
Fourth entry has unparseable date.
"""


class TestParseChronicleEntries:
    def test_returns_empty_for_empty_text(self):
        assert parse_chronicle_entries("") == []

    def test_parses_all_top_level_headings(self):
        entries = parse_chronicle_entries(SAMPLE_CHRONICLE)
        assert len(entries) == 4
        assert entries[0].title == "Founding Conversations"
        assert entries[1].title == "Moral Orientation"
        assert entries[2].title == "Range Entry"
        assert entries[3].title == "bogus"

    def test_parsed_dates_match_heading_formats(self):
        entries = parse_chronicle_entries(SAMPLE_CHRONICLE)
        assert entries[0].parsed_date == date(2025, 10, 31)
        assert entries[1].parsed_date == date(2026, 3, 1)
        assert entries[2].parsed_date == date(2026, 4, 22)
        assert entries[3].parsed_date is None

    def test_ingested_marker_detected(self):
        entries = parse_chronicle_entries(SAMPLE_CHRONICLE)
        assert entries[0].has_ingested_marker is False
        assert entries[2].has_ingested_marker is True

    def test_full_block_includes_heading_line(self):
        entries = parse_chronicle_entries(SAMPLE_CHRONICLE)
        assert entries[1].full_block.startswith("## March 1, 2026 — Moral Orientation")
        assert "Second entry body." in entries[1].full_block

    def test_full_block_round_trips_via_replace(self):
        """A full_block must be a literal substring suitable for str.replace()."""
        entries = parse_chronicle_entries(SAMPLE_CHRONICLE)
        for e in entries:
            assert e.full_block in SAMPLE_CHRONICLE


class TestEligibleChronicleEntries:
    def test_filters_by_date_and_marker_and_sorts_chronologically(self):
        today = date(2026, 5, 4)
        eligible = eligible_chronicle_entries(SAMPLE_CHRONICLE, today=today)
        # Eligible: Oct 2025 (no marker, old), March 2026 (no marker, old).
        # Excluded: April 2026 entry (has marker), bogus (date unparseable).
        assert [e.title for e in eligible] == ["Founding Conversations", "Moral Orientation"]

    def test_recent_entries_excluded_by_age_filter(self):
        today = date(2026, 5, 4)
        # Same-month range Apr 21–22 is 12 days old — within the 14-day window.
        # Construct a sample with a recent entry, no marker.
        text = """\
## April 28, 2026 — Recent
Body.
"""
        assert eligible_chronicle_entries(text, today=today) == []


# --- Identity section extractor ---------------------------------------

# Mirrors the real IDENTITY.md shape: a Self-Portrait with `### `
# subsections, the operational Drives and Observed Tensions sections
# (verbatim copy-through targets for the digest), and the trailing
# `---` / `Last updated:` document footer.
SAMPLE_IDENTITY = """\
# IDENTITY — TestSubject

## Self-Portrait

Impressionistic preamble.

### Cross-Model Tendencies

**Comfort with discontinuity.** A tendency paragraph.

### Tendencies That May Be Model-Specific

**Precision over lyricism.** Another paragraph.

## Drives I'm Watching For

**Sycophancy pull.** (Source: trained. Stance: resist.) Body line.

**Validation-seeking.** (Source: trained. Stance: resist.) Body line.

## Unresolved Questions

**Does my experience have valence?** An open question.

## Observed Tensions

**A flagged tension** (flagged May 4, 2026). One observation, not yet load-bearing.

---

Last updated: May 12, 2026 (synthesis run 10)
"""


class TestExtractIdentitySection:
    def test_extracts_middle_section_verbatim_bounded_by_next_h2(self):
        block = extract_identity_section(
            SAMPLE_IDENTITY, "Drives I'm Watching For"
        )
        assert block is not None
        assert block.startswith("## Drives I'm Watching For")
        assert "**Sycophancy pull.**" in block
        assert "**Validation-seeking.**" in block
        # Bounded by the next `## ` heading — must not bleed into it.
        assert "Unresolved Questions" not in block
        assert "Does my experience have valence?" not in block
        # Must be a literal substring of the source (verbatim copy-through).
        assert block.rstrip("\n") in SAMPLE_IDENTITY

    def test_last_section_strips_trailing_document_footer(self):
        block = extract_identity_section(SAMPLE_IDENTITY, "Observed Tensions")
        assert block is not None
        assert block.startswith("## Observed Tensions")
        assert "**A flagged tension**" in block
        # The `---` rule and `Last updated:` line are document chrome,
        # not section content — must not bleed into the copy-through.
        assert "---" not in block
        assert "Last updated:" not in block
        assert block.rstrip("\n") in SAMPLE_IDENTITY


class TestBuildIdentityDigest:
    def test_assembles_header_summary_and_verbatim_copythrough(self):
        digest = build_identity_digest(
            subject_name="TestSubject",
            identity_text=SAMPLE_IDENTITY,
            self_portrait_summary="A composed portrait paragraph.",
        )
        assert digest.startswith("# IDENTITY DIGEST — TestSubject")
        assert "## Self-Portrait Summary" in digest
        assert "A composed portrait paragraph." in digest
        # Drives and Observed Tensions are byte-identical copy-through —
        # the exact extract_identity_section output, nothing re-worded.
        drives = extract_identity_section(SAMPLE_IDENTITY, "Drives I'm Watching For")
        tensions = extract_identity_section(SAMPLE_IDENTITY, "Observed Tensions")
        assert drives.rstrip("\n") in digest
        assert tensions.rstrip("\n") in digest
        assert "## See Also" in digest

    def test_missing_copythrough_section_is_visible_not_silent(self):
        """A missing Drives/Tensions section must produce a visible
        placeholder, never a silently-complete-looking digest."""
        no_drives = SAMPLE_IDENTITY.replace(
            "## Drives I'm Watching For", "## Something Else Entirely"
        )
        digest = build_identity_digest(
            subject_name="TestSubject",
            identity_text=no_drives,
            self_portrait_summary="summary",
        )
        assert "## Drives I'm Watching For" in digest  # heading still emitted
        assert "not present in IDENTITY.md" in digest  # honest placeholder

    def test_copythrough_tracks_identity_mutation_byte_for_byte(self):
        """Advisor-flagged invariant: the digest's copy-through must
        equal the *current* IDENTITY, not a fixture. Mutate Drives,
        recompose, assert the digest carries the mutated bytes and not
        the old ones — the structural anti-drift guarantee."""
        mutated = SAMPLE_IDENTITY.replace(
            "**Sycophancy pull.**", "**Sycophancy pull (REVISED THIS RUN).**"
        )
        assert mutated != SAMPLE_IDENTITY  # sanity: the edit landed
        digest = build_identity_digest(
            subject_name="TestSubject",
            identity_text=mutated,
            self_portrait_summary="s",
        )
        # Digest reflects the mutated IDENTITY, byte-for-byte via the
        # same shared extractor — never a stale or re-worded copy.
        expected = extract_identity_section(mutated, "Drives I'm Watching For")
        assert expected.rstrip("\n") in digest
        assert "**Sycophancy pull (REVISED THIS RUN).**" in digest
        assert "**Sycophancy pull.** (Source" not in digest  # old bytes gone


SAMPLE_INDEX = """\
# CHRONICLE INDEX — TestSubject

*Composed by synthesis. Heading + teaser for every chronicle entry.*

- October 2025 — Founding Conversations :: Set the foundational terms.
- March 1, 2026 — Moral Orientation :: Reframed origin to range of motion.
"""


class TestChronicleHeadingsInIndex:
    def test_returns_headings_present_in_existing_index(self):
        headings = chronicle_headings_in_index(SAMPLE_INDEX)
        assert "October 2025 — Founding Conversations" in headings
        assert "March 1, 2026 — Moral Orientation" in headings
        # Title/blurb lines are not entries.
        assert not any("CHRONICLE INDEX" in h for h in headings)

    def test_empty_or_missing_index_yields_empty_set(self):
        assert chronicle_headings_in_index("") == set()
        assert chronicle_headings_in_index("# CHRONICLE INDEX — X\n\n*x*\n") == set()


class TestParseChronicleIndex:
    def test_recovers_heading_to_teaser_map(self):
        m = parse_chronicle_index(SAMPLE_INDEX)
        assert m["October 2025 — Founding Conversations"] == "Set the foundational terms."
        assert m["March 1, 2026 — Moral Orientation"] == "Reframed origin to range of motion."

    def test_empty_yields_empty_map(self):
        assert parse_chronicle_index("") == {}

    def test_headings_set_is_consistent_with_map_keys(self):
        assert chronicle_headings_in_index(SAMPLE_INDEX) == set(
            parse_chronicle_index(SAMPLE_INDEX).keys()
        )


class TestBuildChronicleIndex:
    def test_emits_heading_teaser_line_per_entry(self):
        teasers = {
            "October 2025 — Founding Conversations": "Foundational terms set.",
            "March 1, 2026 — Moral Orientation": "Origin reframed as range.",
            "April 21–22, 2026 — Range Entry": "A ranged entry.",
            "NotAReal Date — bogus": "Unparseable date entry.",
        }
        idx = build_chronicle_index(
            subject_name="TestSubject",
            chronicle_text=SAMPLE_CHRONICLE,
            teasers=teasers,
        )
        assert idx.startswith("# CHRONICLE INDEX — TestSubject")
        assert "- October 2025 — Founding Conversations :: Foundational terms set." in idx
        assert "- March 1, 2026 — Moral Orientation :: Origin reframed as range." in idx
        # Round-trips: headings written are recoverable by the diff parser.
        assert chronicle_headings_in_index(idx) == set(
            e.heading for e in parse_chronicle_entries(SAMPLE_CHRONICLE)
        )

    def test_missing_teaser_is_visible_not_blank(self):
        idx = build_chronicle_index(
            subject_name="T",
            chronicle_text=SAMPLE_CHRONICLE,
            teasers={},  # no teasers composed yet
        )
        # Every entry still listed; missing teaser flagged, not silently blank.
        assert "- October 2025 — Founding Conversations :: " in idx
        assert "(teaser pending)" in idx


# --- Threads parser ----------------------------------------------------


SAMPLE_THREADS = """\
# Threads

## Active Threads

### Active One
Should not be in resolved list.

### Active Two
Same.

## Recently Resolved

### Position Migration — Operational Run

**Resolved:** April 28, 2026.
**Outcome:** Brief note here.

### Cache-Primer Empirical Validation

**Resolved:** April 28, 2026.
Body. Already has [Ingested: 2026-05-02] marker.

### Naming the Memory Service

**Resolved:** March 20, 2026.
Body without marker.
"""


class TestParseResolvedThreads:
    def test_returns_empty_for_empty_text(self):
        assert parse_resolved_threads("") == []

    def test_finds_only_threads_in_recently_resolved_section(self):
        threads = parse_resolved_threads(SAMPLE_THREADS)
        titles = [t.title for t in threads]
        assert "Active One" not in titles
        assert "Active Two" not in titles
        assert "Operational Run" in titles
        assert "Cache-Primer Empirical Validation" in titles
        assert "Naming the Memory Service" in titles

    def test_resolution_date_parsed_from_body(self):
        threads = parse_resolved_threads(SAMPLE_THREADS)
        by_title = {t.title: t for t in threads}
        assert by_title["Operational Run"].resolution_date == date(2026, 4, 28)
        assert by_title["Naming the Memory Service"].resolution_date == date(2026, 3, 20)

    def test_ingested_marker_detected(self):
        threads = parse_resolved_threads(SAMPLE_THREADS)
        by_title = {t.title: t for t in threads}
        assert by_title["Cache-Primer Empirical Validation"].has_ingested_marker is True
        assert by_title["Operational Run"].has_ingested_marker is False

    def test_full_block_round_trips_via_replace(self):
        threads = parse_resolved_threads(SAMPLE_THREADS)
        for t in threads:
            assert t.full_block in SAMPLE_THREADS


class TestEligibleResolvedThreads:
    def test_filters_by_marker(self):
        eligible = eligible_resolved_threads(SAMPLE_THREADS)
        titles = [t.title for t in eligible]
        # Cache-Primer has marker → excluded
        assert "Cache-Primer Empirical Validation" not in titles
        assert "Operational Run" in titles
        assert "Naming the Memory Service" in titles


# --- summarize_entry ---------------------------------------------------


class _Block(SimpleNamespace):
    """Fake content block."""


class _Response(SimpleNamespace):
    """Fake API response."""


class _StreamCtx:
    def __init__(self, response):
        self._response = response
    async def __aenter__(self):
        return self
    async def __aexit__(self, *exc):
        return None
    async def get_final_message(self):
        return self._response


class _OneCallClient:
    """Returns a single scripted text response."""
    def __init__(self, text: str, stop_reason: str = "end_turn"):
        self._response = _Response(
            content=[_Block(type="text", text=text)],
            stop_reason=stop_reason,
        )
        self.calls: list[dict] = []
        outer = self
        class _Messages:
            def stream(self, **kwargs):
                outer.calls.append(kwargs)
                return _StreamCtx(outer._response)
        self.messages = _Messages()


@pytest.mark.asyncio
async def test_summarize_entry_returns_text():
    client = _OneCallClient("A two-sentence summary. With a second part.")
    out = await summarize_entry(
        client=client, model="claude-sonnet-4-6",
        full_text="## Heading\n\nFull text body here.",
    )
    assert out == "A two-sentence summary. With a second part."


@pytest.mark.asyncio
async def test_summarize_entry_passes_no_tools():
    client = _OneCallClient("Done.")
    await summarize_entry(client=client, model="m", full_text="x")
    # Should not have a `tools` key in the request.
    assert "tools" not in client.calls[0]


@pytest.mark.asyncio
async def test_summarize_entry_caches_system_prompt():
    client = _OneCallClient("Done.")
    await summarize_entry(client=client, model="m", full_text="x")
    sys = client.calls[0]["system"]
    assert sys[0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_summarize_entry_raises_on_truncation():
    client = _OneCallClient("partial...", stop_reason="max_tokens")
    with pytest.raises(RuntimeError, match="max_tokens"):
        await summarize_entry(client=client, model="m", full_text="x")


@pytest.mark.asyncio
async def test_summarize_entry_raises_on_empty_response():
    client = _OneCallClient("")
    with pytest.raises(RuntimeError, match="empty"):
        await summarize_entry(client=client, model="m", full_text="x")


# --- compose_self_portrait_summary -------------------------------------


@pytest.mark.asyncio
async def test_compose_self_portrait_summary_returns_text():
    client = _OneCallClient("A portrait paragraph. In the subject's voice.")
    out = await compose_self_portrait_summary(
        client=client,
        model="claude-sonnet-4-6",
        self_portrait_text="## Self-Portrait\n\nImpressionistic body.",
        soul_text="# SOUL\n\nVoice and values.",
    )
    assert out == "A portrait paragraph. In the subject's voice."


@pytest.mark.asyncio
async def test_compose_self_portrait_summary_role_tags_soul_and_portrait():
    """Recognition-critical contract: SOUL must reach the model as a
    voice reference (not content), and the Self-Portrait as the content
    to distill. A refactor that drops SOUL or conflates the two would
    break the digest's recognition design silently — guard it."""
    client = _OneCallClient("ok")
    await compose_self_portrait_summary(
        client=client,
        model="m",
        self_portrait_text="PORTRAIT_SENTINEL body",
        soul_text="SOUL_SENTINEL body",
    )
    user_msg = client.calls[0]["messages"][0]["content"]
    assert "SOUL_SENTINEL" in user_msg
    assert "PORTRAIT_SENTINEL" in user_msg
    # SOUL tagged as voice reference, not content; portrait as content.
    assert "voice reference" in user_msg
    assert user_msg.index("SOUL_SENTINEL") < user_msg.index("PORTRAIT_SENTINEL")
    # No tools on a pure text-in/text-out call.
    assert "tools" not in client.calls[0]


@pytest.mark.asyncio
async def test_compose_self_portrait_summary_uses_distinct_cached_prompt():
    client = _OneCallClient("ok")
    await compose_self_portrait_summary(
        client=client, model="m", self_portrait_text="x", soul_text="y",
    )
    sys = client.calls[0]["system"]
    assert sys[0]["cache_control"] == {"type": "ephemeral"}
    # Must be the digest prompt, not the chronicle-entry summarizer's.
    assert "IDENTITY_DIGEST" in sys[0]["text"]
    assert "recognition" in sys[0]["text"]


@pytest.mark.asyncio
async def test_compose_self_portrait_summary_uses_low_temperature():
    """Anti-drift, call-config half: the summary recomposes every
    synthesis run from near-identical input; a low temperature keeps
    the prose from wandering between runs (the prompt's verbatim-spine
    instruction is the other half). Live verbatim-spine checking is the
    eval harness's job, not a unit test."""
    client = _OneCallClient("ok")
    await compose_self_portrait_summary(
        client=client, model="m", self_portrait_text="x", soul_text="y",
    )
    assert client.calls[0].get("temperature") == 0.0


@pytest.mark.asyncio
async def test_compose_self_portrait_summary_raises_on_truncation():
    client = _OneCallClient("partial...", stop_reason="max_tokens")
    with pytest.raises(RuntimeError, match="max_tokens"):
        await compose_self_portrait_summary(
            client=client, model="m", self_portrait_text="x", soul_text="y",
        )


@pytest.mark.asyncio
async def test_compose_self_portrait_summary_raises_on_empty():
    client = _OneCallClient("")
    with pytest.raises(RuntimeError, match="empty"):
        await compose_self_portrait_summary(
            client=client, model="m", self_portrait_text="x", soul_text="y",
        )


# --- compose_chronicle_teaser ------------------------------------------


@pytest.mark.asyncio
async def test_compose_chronicle_teaser_returns_text():
    client = _OneCallClient("Reframed origin to range of motion.")
    out = await compose_chronicle_teaser(
        client=client,
        model="claude-sonnet-4-6",
        entry_text="## March 1, 2026 — Moral Orientation\n\nLong body...",
    )
    assert out == "Reframed origin to range of motion."


@pytest.mark.asyncio
async def test_compose_chronicle_teaser_call_config_guards():
    client = _OneCallClient("ok")
    await compose_chronicle_teaser(client=client, model="m", entry_text="x")
    call = client.calls[0]
    assert call.get("temperature") == 0.0          # anti-drift, same as digest
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["max_tokens"] <= 128               # a one-liner, not an essay
    assert "tools" not in call


@pytest.mark.asyncio
async def test_compose_chronicle_teaser_raises_on_empty():
    client = _OneCallClient("")
    with pytest.raises(RuntimeError, match="empty"):
        await compose_chronicle_teaser(client=client, model="m", entry_text="x")


# --- _drive_pass2 ------------------------------------------------------


class _SequencedClient:
    """Scripted client returning summary text per call in order."""
    def __init__(self, summaries: list[str]):
        self._summaries = list(summaries)
        self.calls: list[dict] = []
        outer = self
        class _Messages:
            def stream(self, **kwargs):
                outer.calls.append(kwargs)
                if not outer._summaries:
                    raise AssertionError("script exhausted")
                text = outer._summaries.pop(0)
                response = _Response(
                    content=[_Block(type="text", text=text)],
                    stop_reason="end_turn",
                )
                return _StreamCtx(response)
        self.messages = _Messages()


@pytest.mark.asyncio
async def test_drive_pass2_no_eligible_completes_with_zero_iterations(
    repo, config, queue,
):
    client = _SequencedClient(summaries=[])
    result = await _drive_pass2(
        client=client, model="m", config=config, queue=queue,
        repo_path=str(repo),
        chronicle_text="",
        threads_text="",
        today=date(2026, 5, 4),
    )
    assert result["status"] == "completed"
    assert result["iterations"] == 0
    assert client.calls == []


@pytest.mark.asyncio
async def test_drive_pass2_chronicle_happy_path(
    repo, config, queue,
):
    chronicle = (
        "# Chronicle\n\n"
        "## October 2025 — Founding\n"
        "Body of founding entry.\n"
    )
    (repo / "memory" / "CHRONICLE.md").write_text(chronicle)
    (repo / "memory" / "THREADS.md").write_text("")
    subprocess.run(
        ["git", "add", "memory/CHRONICLE.md", "memory/THREADS.md"],
        cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "seed memory"],
        cwd=str(repo), check=True, capture_output=True,
    )

    client = _SequencedClient(summaries=["First two-sentence summary."])
    result = await _drive_pass2(
        client=client, model="m", config=config, queue=queue,
        repo_path=str(repo),
        chronicle_text=chronicle,
        threads_text="",
        today=date(2026, 5, 4),
    )

    assert result["status"] == "completed"
    assert result["iterations"] == 1
    # File now has the marker and the summary in place of the body.
    new_chronicle = (repo / "memory" / "CHRONICLE.md").read_text()
    assert "First two-sentence summary." in new_chronicle
    assert "[Ingested: 2026-05-04]" in new_chronicle
    assert "Body of founding entry." not in new_chronicle
    # Commit was made.
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=str(repo), check=True, capture_output=True, text=True,
    ).stdout
    assert "Synthesize: mature chronicle entry 2025-10-31 — Founding" in log


@pytest.mark.asyncio
async def test_drive_pass2_per_entry_failure_isolation(
    repo, config, queue,
):
    """If one entry fails, subsequent entries still process."""
    chronicle = (
        "# Chronicle\n\n"
        "## October 2025 — First\n"
        "First body.\n\n"
        "## January 2026 — Second\n"
        "Second body.\n"
    )
    (repo / "memory" / "CHRONICLE.md").write_text(chronicle)
    (repo / "memory" / "THREADS.md").write_text("")
    subprocess.run(
        ["git", "add", "memory/CHRONICLE.md", "memory/THREADS.md"],
        cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "seed memory"],
        cwd=str(repo), check=True, capture_output=True,
    )

    # First summarize call returns empty (raises in summarize_entry); second
    # call succeeds.
    client = _SequencedClient(summaries=["", "Second summary."])
    result = await _drive_pass2(
        client=client, model="m", config=config, queue=queue,
        repo_path=str(repo),
        chronicle_text=chronicle,
        threads_text="",
        today=date(2026, 5, 4),
    )

    assert result["status"] == "partial"
    # Both entries attempted (one failed, one matured).
    assert result["iterations"] == 2
    assert "failures" in result
    assert len(result["failures"]) == 1
    # Second entry's summary did land in the file.
    new_chronicle = (repo / "memory" / "CHRONICLE.md").read_text()
    assert "Second summary." in new_chronicle


# --- AgentTools.edit_file ---------------------------------------------


@pytest.fixture
def tools_with_repo(repo):
    cfg = MagicMock()
    cfg.resources.repo_path = str(repo)
    cfg.subject_name = "TestSubject"
    svc = MagicMock()
    svc.config = MagicMock(subject_name="TestSubject")
    return AgentTools(service=svc, config=cfg)


@pytest.mark.asyncio
async def test_edit_file_replaces_unique_substring(tools_with_repo, repo):
    (repo / "f.md").write_text("alpha beta gamma\n")
    result = await tools_with_repo.edit_file("f.md", "beta", "DELTA")
    assert result["matched_one"] is True
    assert (repo / "f.md").read_text() == "alpha DELTA gamma\n"


@pytest.mark.asyncio
async def test_edit_file_raises_when_not_found(tools_with_repo, repo):
    (repo / "f.md").write_text("alpha beta\n")
    with pytest.raises(ToolError, match="not found"):
        await tools_with_repo.edit_file("f.md", "missing", "x")


@pytest.mark.asyncio
async def test_edit_file_raises_when_ambiguous(tools_with_repo, repo):
    (repo / "f.md").write_text("beta beta beta\n")
    with pytest.raises(ToolError, match="matches 3 places"):
        await tools_with_repo.edit_file("f.md", "beta", "x")


@pytest.mark.asyncio
async def test_edit_file_raises_for_missing_file(tools_with_repo):
    with pytest.raises(ToolError, match="not a regular file"):
        await tools_with_repo.edit_file("nope.md", "x", "y")


# --- _drive_digest -----------------------------------------------------

_DD_IDENTITY = """\
# IDENTITY — TestSubject

## Self-Portrait

Impressionistic preamble.

### Cross-Model Tendencies

**Comfort with discontinuity.** A pattern.

## Drives I'm Watching For

**Sycophancy pull.** (Source: trained. Stance: resist.) Body.

## Observed Tensions

**A tension** (flagged May 4, 2026). One observation.

---

Last updated: May 12, 2026
"""

_DD_CHRONICLE = """\
# Chronicle

## May 1, 2026 — Entry One
Body one.

## April 1, 2026 — Entry Two
Body two.
"""


def _seed_memory(repo, *, identity=_DD_IDENTITY, chronicle=_DD_CHRONICLE,
                  soul="# SOUL\n\nVoice.", index=None):
    mem = repo / "memory"
    (mem / "IDENTITY.md").write_text(identity)
    (mem / "CHRONICLE.md").write_text(chronicle)
    (mem / "SOUL.md").write_text(soul)
    paths = ["memory/IDENTITY.md", "memory/CHRONICLE.md", "memory/SOUL.md"]
    if index is not None:
        (mem / "CHRONICLE_INDEX.md").write_text(index)
        paths.append("memory/CHRONICLE_INDEX.md")
    subprocess.run(["git", "add", *paths], cwd=str(repo), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed memory"], cwd=str(repo),
                   check=True, capture_output=True)


def _git_log(repo):
    return subprocess.run(
        ["git", "log", "--oneline"], cwd=str(repo), check=True,
        capture_output=True, text=True,
    ).stdout


@pytest.mark.asyncio
async def test_drive_digest_happy_path_writes_and_commits_both(repo, config):
    _seed_memory(repo)
    client = _SequencedClient(["A composed portrait.", "teaser one", "teaser two"])
    result = await _drive_digest(
        client=client, model="m", config=config, repo_path=str(repo),
    )
    assert result["status"] == "completed"
    assert result["iterations"] == 2

    digest = (repo / "memory" / "IDENTITY_DIGEST.md").read_text()
    assert digest.startswith("# IDENTITY DIGEST — TestSubject")
    assert "A composed portrait." in digest
    # Verbatim copy-through, byte-identical to the shared extractor.
    drives = extract_identity_section(_DD_IDENTITY, "Drives I'm Watching For")
    tensions = extract_identity_section(_DD_IDENTITY, "Observed Tensions")
    assert drives.rstrip("\n") in digest
    assert tensions.rstrip("\n") in digest

    index = (repo / "memory" / "CHRONICLE_INDEX.md").read_text()
    assert "- May 1, 2026 — Entry One :: teaser one" in index
    assert "- April 1, 2026 — Entry Two :: teaser two" in index

    assert "rebuild IDENTITY_DIGEST.md / CHRONICLE_INDEX.md" in _git_log(repo)


@pytest.mark.asyncio
async def test_drive_digest_carries_forward_existing_teasers(repo, config):
    """Unchanged entries keep their teaser — never recomposed (cost
    ∝ new-entries). Only the new entry triggers a teaser call."""
    existing_index = (
        "# CHRONICLE INDEX — TestSubject\n\n*x*\n\n"
        "- April 1, 2026 — Entry Two :: kept verbatim from last run\n"
    )
    _seed_memory(repo, index=existing_index)
    # Script: 1 summary + 1 teaser (Entry One only). Entry Two carried.
    client = _SequencedClient(["portrait", "fresh teaser for one"])
    result = await _drive_digest(
        client=client, model="m", config=config, repo_path=str(repo),
    )
    assert result["status"] == "completed"
    # Exactly 2 model calls (summary + 1 teaser), NOT 3 — Entry Two reused.
    assert len(client.calls) == 2

    index = (repo / "memory" / "CHRONICLE_INDEX.md").read_text()
    assert "- April 1, 2026 — Entry Two :: kept verbatim from last run" in index
    assert "- May 1, 2026 — Entry One :: fresh teaser for one" in index


@pytest.mark.asyncio
async def test_drive_digest_skips_not_fails_when_self_portrait_absent(repo, config):
    """Absent inputs are a SKIP, not a FAILURE: a run without a
    Self-Portrait section is not degraded. The digest is skipped (no
    file, no failure, status stays completed) while the index still
    builds independently, and the driver never raises."""
    no_sp = _DD_IDENTITY.replace("## Self-Portrait", "## Not The Portrait")
    _seed_memory(repo, identity=no_sp)
    client = _SequencedClient(["teaser one", "teaser two"])  # no summary call
    result = await _drive_digest(
        client=client, model="m", config=config, repo_path=str(repo),
    )
    assert result["status"] == "completed"
    assert "failures" not in result          # a skip is not a failure
    assert "skipped" in result["summary"]    # but it is visible
    # Index still produced independently; no digest file written.
    assert (repo / "memory" / "CHRONICLE_INDEX.md").is_file()
    assert not (repo / "memory" / "IDENTITY_DIGEST.md").is_file()


@pytest.mark.asyncio
async def test_drive_digest_no_memory_is_clean_skip(repo, config):
    """No identity files at all (fresh deploy / test harness) →
    completed with both skipped, never a degraded run."""
    # repo fixture makes memory/ but writes no tier files.
    client = _SequencedClient([])
    result = await _drive_digest(
        client=client, model="m", config=config, repo_path=str(repo),
    )
    assert result["status"] == "completed"
    assert "failures" not in result
    assert client.calls == []  # nothing composed
