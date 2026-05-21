"""Prompt templates for the extract and reconcile stages.

Layered for prompt caching: system (stable across all episodes) → per-session
(stable across a saga) → previous-episode context (changes per session) →
the episode itself.
"""

from __future__ import annotations

from datetime import datetime
from textwrap import dedent

from graphiti_core.nodes import EpisodeType, EpisodicNode

from pratyabhijna.entity_types import PRATYABHIJNA_ENTITY_TYPES


# ---------------------------------------------------------------------------
# Extract (Stage 2)
# ---------------------------------------------------------------------------


def build_extract_system_prompt() -> str:
    """Stable system prompt across every extract call. Cacheable.

    The entity-type docstrings carry the GOOD/BAD examples that shape what
    the extractor reaches for; they're embedded verbatim under per-type
    headings so the LLM sees the same surface today and a year from now.
    """
    type_blocks = []
    for name, model in PRATYABHIJNA_ENTITY_TYPES.items():
        doc = (model.__doc__ or "").strip()
        type_blocks.append(f"### {name}\n\n{doc}")
    types_section = "\n\n".join(type_blocks)
    return (
        dedent(
            """
            You are an entity-and-relation extractor for a knowledge graph that
            represents one subject's structured self-knowledge.

            For the supplied episode (text written by or about the subject), extract:

            - The named entities mentioned, typed by the schemas below. For each
              entity, provide a short (one sentence) summary that captures what
              this entity *is* in a form that disambiguates it from similarly
              named candidates on future runs — e.g. "Vesper, the AI subject of
              this graph (not the Person named Vesper)". The summary is what
              the reconcile stage of a future episode will see when deciding
              whether your entity is the same one already in the graph.
            - The factual relations between them, expressed as edges with a
              semantic predicate (e.g. "works_on", "remembers", "supersedes")
              and a single short sentence stating the fact.

            Do not extract entities that appear only as passing mentions with no
            information attached. Do not invent edges that the text doesn't assert.

            Return your output by calling the extract_episode tool with two
            dense-indexed arrays (nodes and edges). Edge endpoints reference
            node idx values; both endpoints must exist in the nodes array.

            ## Entity Types

            """
        ).strip()
        + "\n\n"
        + types_section
    )


_SOURCE_HINTS: dict[EpisodeType, str] = {
    EpisodeType.message: "This episode is a conversational message.",
    EpisodeType.text: "This episode is a document or written passage.",
    EpisodeType.json: "This episode is structured JSON.",
}


def build_extract_user_message(
    *,
    episode_name: str,
    source: EpisodeType,
    source_description: str,
    reference_time: datetime,
    body: str,
    previous_episodes: list[EpisodicNode],
) -> str:
    """Per-episode body of the extract prompt.

    Previous episodes are listed for conversational context; the episode
    body comes last so it's the freshest context in the model's window.
    """
    src_hint = _SOURCE_HINTS[source]

    prev_block = ""
    if previous_episodes:
        rendered = "\n\n---\n\n".join(
            f"[{ep.valid_at.isoformat() if getattr(ep, 'valid_at', None) else 'unknown'}] "
            f"{getattr(ep, 'content', '') or ''}"
            for ep in previous_episodes
        )
        prev_block = f"## Previous Episodes (for context)\n\n{rendered}\n\n"

    return dedent(
        f"""
        {src_hint}

        Source: {source_description}
        Reference time: {reference_time.isoformat()}
        Episode name: {episode_name}

        {prev_block}## Episode

        {body}

        Now call the extract_episode tool with the entities and edges.
        """
    ).strip()


# ---------------------------------------------------------------------------
# Reconcile nodes (Stage 4a)
# ---------------------------------------------------------------------------


def build_reconcile_nodes_prompt(
    *,
    extracted_nodes: list[dict],
    candidates: list[list[dict]],
) -> str:
    """Render the per-extracted-node candidate listing plus instructions."""
    blocks = []
    for i, node in enumerate(extracted_nodes):
        cand_lines = []
        for c in candidates[i]:
            summary = (c.get("summary") or "")[:120]
            cand_lines.append(
                f"  - uuid={c['uuid']} name={c['name']!r} "
                f"labels={c.get('labels', [])} summary={summary!r}"
            )
        cand_str = "\n".join(cand_lines) if cand_lines else "  (no candidates)"
        blocks.append(
            f"Extracted node idx={i} name={node['name']!r} type={node['type']}\n"
            f"Candidates:\n{cand_str}"
        )
    body = "\n\n".join(blocks)
    return dedent(
        f"""
        For each extracted node below, decide whether it matches one of the
        candidate existing nodes (decision="existing", with existing_uuid set
        to the candidate's uuid) or is a new node (decision="new", no
        existing_uuid).

        Match liberally on identity but conservatively on type: two candidates
        with the same name but different labels are usually different entities
        (a Person named "Vesper" is not the AI named "Vesper").

        If matching, you may optionally include attribute_updates with fields
        to merge onto the existing node. Use sparingly — only when the episode
        adds genuinely new information.

        Return exactly one decision per extracted node. Every extracted idx
        must appear in node_decisions exactly once.

        {body}

        Call the reconcile_nodes tool with your decisions.
        """
    ).strip()


# ---------------------------------------------------------------------------
# Reconcile edges (Stage 4b)
# ---------------------------------------------------------------------------


def build_reconcile_edges_prompt(
    *,
    extracted_edges: list[dict],
    candidates: list[list[dict]],
) -> str:
    blocks = []
    for i, edge in enumerate(extracted_edges):
        cand_lines = []
        for c in candidates[i]:
            cand_lines.append(
                f"  - uuid={c['uuid']} name={c['name']!r} fact={c['fact']!r} "
                f"valid_at={c.get('valid_at')} invalid_at={c.get('invalid_at')}"
            )
        cand_str = "\n".join(cand_lines) if cand_lines else "  (no candidates)"
        blocks.append(
            f"Extracted edge idx={i}: name={edge['name']!r} fact={edge['fact']!r}\n"
            f"Candidates:\n{cand_str}"
        )
    body = "\n\n".join(blocks)
    return dedent(
        f"""
        For each extracted edge, decide one of three outcomes:

        - "new": the edge is genuinely new information.
        - "existing": the edge already exists (existing_uuid required); reuse it.
        - "supersedes": the edge asserts a state of the world that contradicts
          an existing candidate edge (existing_uuid required); the candidate
          will be marked invalid as of this episode's reference time.

        Supersession is for *contradiction*, not for "same subject" — two edges
        about the same subject can both be true at once. If you're unsure
        between "new" and "supersedes", choose "new".

        Return exactly one decision per extracted edge. Every extracted idx
        must appear in edge_decisions exactly once.

        {body}

        Call the reconcile_edges tool with your decisions.
        """
    ).strip()
