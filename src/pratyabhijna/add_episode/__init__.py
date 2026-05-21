"""In-house replacement for graphiti.add_episode.

See doc/in-house-add-episode-design.md for architecture and
doc/in-house-add-episode-plan.md for the implementation arc.
"""

from pratyabhijna.add_episode.pipeline import AddEpisodeResult, add_episode

__all__ = ["add_episode", "AddEpisodeResult"]
