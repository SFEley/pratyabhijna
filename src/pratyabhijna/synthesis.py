"""Identity synthesis foundations.

Finding the subject node, checking context staleness, collecting
identity atoms, and computing deltas. These support the bootstrap
tool and will be extended by the synthesis agent in Phase 7.

The subject Person node stores three bootstrap tiers as separate
attributes: soul (constitutional), identity (interpretive), and
context (state, auto-rebuilt). Staleness applies only to the context
layer — soul and identity change through deliberate reflection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from graphiti_core.edges import EntityEdge
    from graphiti_core.nodes import EntityNode

    from pratyabhijna.service import PratyabhijnaService

IDENTITY_LABELS = {"Observation", "Drive", "Concept", "Question", "Thread"}

IDENTITY_FILES = {
    "soul": "SOUL.md",
    "identity": "IDENTITY.md",
    "user": "USER.md",
    "threads": "THREADS.md",
    "chronicle": "CHRONICLE.md",
}

# Files in the subject's repo that should NOT be candidates for
# graph ingestion. Bootstrap tier files are maintained by the
# synthesizer's bootstrap-update pass, not fed back as episodes.
# MEMORY.md is skipped out of caution — it has historically been
# an index rather than narrative content; the subject can ingest
# it deliberately if that changes.
BOOTSTRAP_RELATIVE_PATHS = frozenset(
    {
        "memory/SOUL.md",
        "memory/IDENTITY.md",
        "memory/USER.md",
        "memory/THREADS.md",
        "memory/CHRONICLE.md",
        "memory/MEMORY.md",
        "memory/SYNTHESIS.md",
    }
)


def read_identity_files(repo_path: str) -> dict[str, str | None]:
    """Read identity tier files from {repo_path}/memory/.

    Returns a dict keyed by tier name with file contents, or empty dict
    if repo_path is unconfigured or the memory directory doesn't exist.
    """
    if not repo_path:
        return {}
    memory_dir = Path(repo_path).expanduser().resolve() / "memory"
    if not memory_dir.is_dir():
        return {}
    result = {}
    for key, filename in IDENTITY_FILES.items():
        filepath = memory_dir / filename
        result[key] = filepath.read_text(encoding="utf-8").strip() if filepath.is_file() else None
    return result


async def get_subject_node(service: PratyabhijnaService) -> EntityNode | None:
    """Find the subject Person node by configured name."""
    return await service.get_entity_by_name(service.config.subject_name)


async def is_stale(node: EntityNode, service: PratyabhijnaService) -> bool:
    """Check whether the context layer needs rebuilding.

    Stale when any of:
    - Synthesis has never run (no ``context_rebuilt_at``)
    - The last rebuild is older than ``max_age_hours``
    - The identity delta since last rebuild exceeds ``max_delta_changes``

    Context-layer content itself lives in files (THREADS/CHRONICLE/USER)
    which we don't inspect here; staleness is judged from graph metadata
    and atom counts alone. The synthesizer decides what to do about it.
    """
    rebuilt_at_str = node.attributes.get("context_rebuilt_at")
    if rebuilt_at_str is None:
        return True

    rebuilt_at = _ensure_utc(datetime.fromisoformat(rebuilt_at_str))
    age_hours = (datetime.now(timezone.utc) - rebuilt_at).total_seconds() / 3600
    if age_hours >= service.config.synthesis.max_age_hours:
        return True

    delta = await get_subject_delta(service, node)
    return len(delta) >= service.config.synthesis.max_delta_changes


async def get_identity_atoms(
    service: PratyabhijnaService, node: EntityNode
) -> list[dict]:
    """Collect identity-typed edges connected to the subject node.

    Returns atoms for edges whose non-subject endpoint is an
    identity type (any label in IDENTITY_LABELS). This is the
    narrow view used by Pass 3 for narrative reweighting — the
    full set of "what is the subject" atoms.
    """
    edges = await service.get_edges_for_node(node.uuid)
    atoms = []
    for edge in edges:
        other_uuid = (
            edge.target_node_uuid
            if edge.source_node_uuid == node.uuid
            else edge.source_node_uuid
        )
        other_node = await service.get_entity_by_uuid(other_uuid)
        if not _is_identity_node(other_node):
            continue
        atoms.append(_edge_to_atom(edge, other_node))
    return atoms


async def get_subject_atoms(
    service: PratyabhijnaService, node: EntityNode
) -> list[dict]:
    """Collect every edge connected to the subject node, unfiltered.

    Includes Person/Place/Project/Event/Artifact relationships in
    addition to the identity-typed atoms. Used to compute the broad
    delta — anything new or changed in the subject's relational world
    since the prior synthesis run, not only changes in the subject's
    self-knowledge.
    """
    edges = await service.get_edges_for_node(node.uuid)
    atoms = []
    for edge in edges:
        other_uuid = (
            edge.target_node_uuid
            if edge.source_node_uuid == node.uuid
            else edge.source_node_uuid
        )
        other_node = await service.get_entity_by_uuid(other_uuid)
        if other_node is None:
            continue
        atoms.append(_edge_to_atom(edge, other_node))
    return atoms


async def get_subject_delta(
    service: PratyabhijnaService, node: EntityNode
) -> list[dict]:
    """Return atoms created since the start of the prior synthesis run.

    Reads ``synthesis_run_started_at`` as the reference timestamp, with
    ``context_rebuilt_at`` as the fallback for nodes from before the
    timestamp split landed. When neither attribute is set, every
    subject-connected atom is part of the delta.

    Scope: every edge connected to the subject, not just identity-
    typed edges. New people, places, projects, events and corrected
    facts surface here so the synthesizer can decide whether to thread
    them into CHRONICLE/THREADS — without requiring Vesper-as-subject
    to author the entry directly.
    """
    started_at_str = node.attributes.get(
        "synthesis_run_started_at"
    ) or node.attributes.get("context_rebuilt_at")
    all_atoms = await get_subject_atoms(service, node)
    if started_at_str is None:
        return all_atoms
    started_at = _ensure_utc(datetime.fromisoformat(started_at_str))
    return [a for a in all_atoms if _ensure_utc(a["created_at"]) > started_at]


def _is_identity_node(node: EntityNode) -> bool:
    """Whether a node's labels include an identity type."""
    return bool(set(node.labels) & IDENTITY_LABELS)


@dataclass(frozen=True)
class IngestionCandidate:
    """A file in the subject's repo that is a candidate for ingestion.

    ``relative_path`` (repo-root-relative, forward slashes) doubles as
    the Episode ``name`` — that's how subsequent scans recognize the
    file has been ingested. ``reason`` distinguishes a never-seen file
    from one that was previously ingested but has since been edited.
    """

    relative_path: str
    absolute_path: Path
    mtime: datetime
    reason: str  # "new" | "stale"
    latest_episode_at: datetime | None  # None for "new"


async def scan_repo_for_ingestion_candidates(
    repo_path: str,
    service: PratyabhijnaService,
    *,
    max_age_days: int | None = 14,
) -> list[IngestionCandidate]:
    """Walk ``repo_path`` and return files needing ingestion.

    A file is a candidate when:

    - It is not a bootstrap tier file (SOUL/IDENTITY/USER/THREADS/
      CHRONICLE/MEMORY under ``memory/``).
    - It is not inside ``.git/`` or any other dot-prefixed path segment.
    - It is a regular file (not a symlink into the tree, not a dir).
    - Its mtime is within the lookback window (``max_age_days``), or
      the window is disabled.
    - No Episode with matching ``name`` exists (reason="new"), OR the
      matching Episode's ``created_at`` is older than the file's mtime
      (reason="stale", file was edited since last ingestion).

    Files whose latest Episode is at least as recent as the file are
    skipped silently.

    The window defaults to 14 days so runs don't re-scan a large
    archive on every invocation. Pass ``max_age_days=None`` to scan
    everything (useful for a from-scratch ingest).

    The scan does NOT write to the graph. It only identifies candidates;
    the caller decides what to actually ingest (and how — selection is
    the subject's judgment per the subskill).
    """
    root = Path(repo_path).expanduser().resolve()
    if not root.is_dir():
        return []

    cutoff: datetime | None = None
    if max_age_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    candidates: list[IngestionCandidate] = []
    for path in _iter_non_bootstrap_files(root):
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if cutoff is not None and mtime < cutoff:
            continue

        relative = path.relative_to(root).as_posix()
        episode = await service.get_latest_episode_by_name(relative)

        if episode is None:
            candidates.append(
                IngestionCandidate(
                    relative_path=relative,
                    absolute_path=path,
                    mtime=mtime,
                    reason="new",
                    latest_episode_at=None,
                )
            )
            continue

        episode_at = _ensure_utc(episode.created_at)
        if mtime > episode_at:
            candidates.append(
                IngestionCandidate(
                    relative_path=relative,
                    absolute_path=path,
                    mtime=mtime,
                    reason="stale",
                    latest_episode_at=episode_at,
                )
            )

    return candidates


def _iter_non_bootstrap_files(root: Path):
    """Yield regular files under ``root``, skipping bootstrap paths,
    dot-prefixed directories (including ``.git``), and dot-prefixed files.
    """
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # Skip any path with a dot-prefixed segment (e.g. .git, .cache)
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        relative = path.relative_to(root).as_posix()
        if relative in BOOTSTRAP_RELATIVE_PATHS:
            continue
        yield path


# --- Pass 2 maturation: chronicle / threads parsers ---
#
# Pass 2 is a deterministic Python driver, not an agent loop (since
# v0.14.3). The driver parses CHRONICLE.md and THREADS.md, identifies
# eligible entries, and per entry: enqueues the full text via remember(),
# asks the model only for the summary string, edits the file in place,
# and commits. The parsers below are the file-side half of that flow.

# Markdown headings: chronicle entries are level-2 (`## `); threads are
# level-3 (`### `) under level-2 section headers (`## Active Threads`,
# `## Recently Resolved`).
_CHRONICLE_HEADING = re.compile(r"^## (?!#)(.+)$", re.MULTILINE)
_THREAD_HEADING = re.compile(r"^### (.+)$", re.MULTILINE)
_SECTION_HEADING = re.compile(r"^## (?!#)(.+)$", re.MULTILINE)
_HR_LINE = re.compile(r"^-{3,}\s*$", re.MULTILINE)
_INGESTED_MARKER = re.compile(r"\[Ingested:\s*\d{4}-\d{2}-\d{2}\]")
_RESOLVED_MARKER = re.compile(
    r"^\*\*Resolved:\*\*\s+(.+?)\.?\s*$", re.MULTILINE
)

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


@dataclass(frozen=True)
class ChronicleEntry:
    """One `## `-bounded chronicle entry parsed from CHRONICLE.md.

    ``full_block`` is the literal substring (heading + body, ending at the
    next `## ` heading or EOF) — the driver uses it as ``old_string`` for
    the in-place edit. ``parsed_date`` is the entry's date; for ranges and
    month spans the *latest* date is used per the subskill rules.
    ``has_ingested_marker`` reflects whether the body already carries an
    ``[Ingested: YYYY-MM-DD]`` marker.
    """
    heading: str           # e.g. "October 2025 — Founding Conversations"
    title: str             # e.g. "Founding Conversations" (post — split)
    full_block: str        # literal substring including heading line
    body: str              # everything after the heading line
    parsed_date: date | None
    has_ingested_marker: bool


@dataclass(frozen=True)
class ResolvedThread:
    """One `### `-bounded thread under THREADS.md's `## Recently Resolved`."""
    heading: str
    title: str
    full_block: str
    body: str
    resolution_date: date | None
    has_ingested_marker: bool


def _parse_chronicle_date(prefix: str) -> date | None:
    """Parse the date portion of a chronicle heading.

    Handles, in order:
    - Single date:        ``March 1, 2026``
    - Same-month range:   ``February 16-19, 2026``  / ``February 16–19, 2026``
    - Cross-month range:  ``March 18 - April 2, 2026``  / en-dash variant
    - Year-only month:    ``October 2025``
    - Year-only:          ``2025``

    For ranges and spans, returns the *latest* date. For a year-only
    month, returns the last day of that month. Returns ``None`` if the
    string can't be parsed confidently — the caller should skip and
    flag rather than guess.
    """
    s = prefix.strip().rstrip(",").strip()
    # Normalize en-dash and em-dash to ASCII hyphen for splitting.
    norm = s.replace("–", "-").replace("—", "-")

    # Cross-month range: "Month D - Month D, YYYY"
    m = re.fullmatch(
        r"([A-Za-z]+)\s+(\d{1,2})\s*-\s*([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})",
        norm,
    )
    if m:
        month_b, day_b, year = m.group(3).lower(), int(m.group(4)), int(m.group(5))
        if month_b in _MONTHS:
            try:
                return date(year, _MONTHS[month_b], day_b)
            except ValueError:
                return None
        return None

    # Same-month range: "Month D-D, YYYY"
    m = re.fullmatch(
        r"([A-Za-z]+)\s+(\d{1,2})\s*-\s*(\d{1,2}),\s*(\d{4})", norm
    )
    if m:
        month, day_b, year = m.group(1).lower(), int(m.group(3)), int(m.group(4))
        if month in _MONTHS:
            try:
                return date(year, _MONTHS[month], day_b)
            except ValueError:
                return None
        return None

    # Single date: "Month D, YYYY"
    m = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", norm)
    if m:
        month, day, year = m.group(1).lower(), int(m.group(2)), int(m.group(3))
        if month in _MONTHS:
            try:
                return date(year, _MONTHS[month], day)
            except ValueError:
                return None
        return None

    # Year-month only: "Month YYYY" → last day of that month
    m = re.fullmatch(r"([A-Za-z]+)\s+(\d{4})", norm)
    if m:
        month, year = m.group(1).lower(), int(m.group(2))
        if month in _MONTHS:
            mn = _MONTHS[month]
            # Last day of month: first of next month minus one.
            if mn == 12:
                last = date(year, 12, 31)
            else:
                last = date(year, mn + 1, 1) - timedelta(days=1)
            return last
        return None

    # Year only: "YYYY" → Dec 31 of that year
    m = re.fullmatch(r"\d{4}", norm)
    if m:
        return date(int(norm), 12, 31)

    return None


def _split_heading(text: str, sep: str = "—") -> tuple[str, str]:
    """Split a heading like 'March 1, 2026 — Title' into (date_part, title).

    Falls back to ASCII '--' or '-' if the em-dash isn't present.
    """
    for s in (f" {sep} ", " -- ", " - "):
        if s in text:
            left, right = text.split(s, 1)
            return left.strip(), right.strip()
    return text.strip(), ""


def parse_chronicle_entries(chronicle_text: str) -> list[ChronicleEntry]:
    """Walk CHRONICLE.md and return every `## `-bounded entry.

    Order is file order (i.e. newest-first as the chronicle is written
    that way) — callers that need chronological order should sort by
    ``parsed_date``.
    """
    if not chronicle_text:
        return []
    headings = list(_CHRONICLE_HEADING.finditer(chronicle_text))
    entries: list[ChronicleEntry] = []
    for i, m in enumerate(headings):
        heading_line = m.group(0)
        heading_text = m.group(1).strip()
        block_start = m.start()
        block_end = headings[i + 1].start() if i + 1 < len(headings) else len(chronicle_text)
        full_block = chronicle_text[block_start:block_end].rstrip("\n") + "\n"
        body = full_block[len(heading_line):].lstrip("\n").rstrip("\n")
        date_part, title = _split_heading(heading_text)
        parsed = _parse_chronicle_date(date_part)
        entries.append(
            ChronicleEntry(
                heading=heading_text,
                title=title or heading_text,
                full_block=full_block,
                body=body,
                parsed_date=parsed,
                has_ingested_marker=bool(_INGESTED_MARKER.search(body)),
            )
        )
    return entries


def extract_identity_section(identity_text: str, heading: str) -> str | None:
    """Return the verbatim `## `-bounded block for the named IDENTITY section.

    The block runs from the matching ``## <heading>`` line to just before
    the next `## ` heading (or EOF), with trailing newlines normalized to a
    single ``\\n`` — identical slicing to ``parse_chronicle_entries`` so the
    result is a literal substring of ``identity_text``, suitable for the
    digest's verbatim copy-through. `### ` subsections inside the section
    are *not* boundaries (``_SECTION_HEADING`` ignores them). Returns
    ``None`` if no `## ` heading matches ``heading`` exactly.
    """
    if not identity_text:
        return None
    headings = list(_SECTION_HEADING.finditer(identity_text))
    for i, m in enumerate(headings):
        if m.group(1).strip() == heading.strip():
            block_start = m.start()
            if i + 1 < len(headings):
                block = identity_text[block_start:headings[i + 1].start()]
            else:
                # Last section: trim a trailing document footer (the
                # `---` rule before `Last updated:`). IDENTITY.md uses
                # no in-section horizontal rules, so the first one after
                # the heading is the footer boundary.
                block = identity_text[block_start:]
                hr = _HR_LINE.search(block)
                if hr:
                    block = block[: hr.start()]
            return block.rstrip("\n") + "\n"
    return None


def eligible_chronicle_entries(
    chronicle_text: str,
    today: date,
    max_age_days: int = 14,
) -> list[ChronicleEntry]:
    """Return entries eligible for maturation, in chronological order.

    Eligibility (per the Pass 2 subskill):
    - Date parses confidently.
    - Date is more than ``max_age_days`` before ``today``.
    - No ``[Ingested: YYYY-MM-DD]`` marker present in the body.
    """
    cutoff = today - timedelta(days=max_age_days)
    eligible = [
        e for e in parse_chronicle_entries(chronicle_text)
        if e.parsed_date is not None
        and e.parsed_date < cutoff
        and not e.has_ingested_marker
    ]
    eligible.sort(key=lambda e: e.parsed_date)  # type: ignore[arg-type, return-value]
    return eligible


def parse_resolved_threads(threads_text: str) -> list[ResolvedThread]:
    """Walk THREADS.md and return every `### `-bounded thread under
    `## Recently Resolved`.

    Threads under `## Active Threads` are not eligible for Pass 2 — only
    those that have been moved to Recently Resolved.
    """
    if not threads_text:
        return []
    sections = list(_SECTION_HEADING.finditer(threads_text))
    if not sections:
        return []
    # Find the "Recently Resolved" section's bounds.
    target_idx = None
    for i, m in enumerate(sections):
        if m.group(1).strip().lower().startswith("recently resolved"):
            target_idx = i
            break
    if target_idx is None:
        return []
    section_start = sections[target_idx].end()
    section_end = (
        sections[target_idx + 1].start() if target_idx + 1 < len(sections)
        else len(threads_text)
    )
    section_text = threads_text[section_start:section_end]

    threads: list[ResolvedThread] = []
    headings = list(_THREAD_HEADING.finditer(section_text))
    for i, m in enumerate(headings):
        heading_line = m.group(0)
        heading_text = m.group(1).strip()
        block_start = m.start()
        block_end = headings[i + 1].start() if i + 1 < len(headings) else len(section_text)
        # full_block in absolute terms (offset within the original text)
        # so the driver can str.replace() it.
        absolute_block = threads_text[
            section_start + block_start : section_start + block_end
        ].rstrip("\n") + "\n"
        body = absolute_block[len(heading_line):].lstrip("\n").rstrip("\n")
        _, title = _split_heading(heading_text)
        # Resolution date from `**Resolved:** Month DD, YYYY`.
        resolution_date: date | None = None
        rm = _RESOLVED_MARKER.search(body)
        if rm:
            resolution_date = _parse_chronicle_date(rm.group(1).strip())
        threads.append(
            ResolvedThread(
                heading=heading_text,
                title=title or heading_text,
                full_block=absolute_block,
                body=body,
                resolution_date=resolution_date,
                has_ingested_marker=bool(_INGESTED_MARKER.search(body)),
            )
        )
    return threads


def eligible_resolved_threads(threads_text: str) -> list[ResolvedThread]:
    """Return resolved threads that haven't been ingested yet.

    Order is file order. No date filter — once a thread is in Recently
    Resolved, it's eligible.
    """
    return [
        t for t in parse_resolved_threads(threads_text)
        if not t.has_ingested_marker
    ]


# --- end Pass 2 parsers ---


def _ensure_utc(dt: datetime) -> datetime:
    """Normalize a datetime to UTC. Naive values are treated as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _edge_to_atom(edge: EntityEdge, other_node: EntityNode) -> dict:
    """Convert an edge + its identity-typed endpoint into an atom dict."""
    node_type = next(
        (label for label in other_node.labels if label in IDENTITY_LABELS),
        other_node.labels[0] if other_node.labels else "Unknown",
    )
    return {
        "fact": edge.fact,
        "edge_uuid": edge.uuid,
        "node_name": other_node.name,
        "node_type": node_type,
        "created_at": edge.created_at,
    }
