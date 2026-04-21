"""The `communities` MCP tool.

Lists all Community nodes with summaries and member counts, or returns
full detail for a single named community with its member entities.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pratyabhijna.service import PratyabhijnaService


async def communities(
    service: PratyabhijnaService,
    name: str | None = None,
) -> dict:
    """List all communities or display a single community with its members.

    Args:
        service: The PratyabhijnaService instance.
        name: Optional community name. If omitted, returns the full list.

    Returns:
        List dict with count + communities array, or single community detail
        dict with members array, or error dict if name not found.
    """
    group_id = service.config.subject_name

    if name is None:
        nodes = await service.get_all_communities(group_id)
        if not nodes:
            return {"count": 0, "communities": []}
        uuids = [n.uuid for n in nodes]
        counts = await service.get_community_member_counts(uuids)
        return {
            "count": len(nodes),
            "communities": [
                {
                    "uuid": n.uuid,
                    "name": n.name,
                    "summary": n.summary,
                    "member_count": counts.get(n.uuid, 0),
                }
                for n in sorted(nodes, key=lambda n: n.name.lower())
            ],
        }

    node = await service.get_community_by_name(name, group_id)
    if node is None:
        return {
            "error": "not_found",
            "name": name,
            "message": f"No community named '{name}'",
        }
    members = await service.get_community_members(node.uuid)
    return {
        "uuid": node.uuid,
        "name": node.name,
        "summary": node.summary,
        "member_count": len(members),
        "members": members,
    }
