"""Episode-body hash for Stage 0 idempotency."""

from datetime import datetime, timezone

from graphiti_core.nodes import EpisodeType

from pratyabhijna.add_episode.hash import episode_hash


def _base_args():
    return dict(
        group_id="vesper",
        source=EpisodeType.message,
        source_description="self",
        reference_time=datetime(2026, 5, 20, tzinfo=timezone.utc),
        body="Hello world",
    )


def test_hash_is_deterministic():
    args = _base_args()
    assert episode_hash(**args) == episode_hash(**args)


def test_hash_differs_by_body():
    args = _base_args()
    args["body"] = "A"
    h_a = episode_hash(**args)
    args["body"] = "B"
    h_b = episode_hash(**args)
    assert h_a != h_b


def test_hash_differs_by_reference_time():
    args = _base_args()
    h1 = episode_hash(**args)
    args["reference_time"] = datetime(2026, 5, 21, tzinfo=timezone.utc)
    h2 = episode_hash(**args)
    assert h1 != h2, "same body at different times must produce distinct hashes"


def test_hash_differs_by_group_id():
    args = _base_args()
    h1 = episode_hash(**args)
    args["group_id"] = "other"
    h2 = episode_hash(**args)
    assert h1 != h2


def test_hash_differs_by_source_description():
    args = _base_args()
    h1 = episode_hash(**args)
    args["source_description"] = "different"
    h2 = episode_hash(**args)
    assert h1 != h2


def test_hash_differs_by_source_type():
    args = _base_args()
    h_message = episode_hash(**args)
    args["source"] = EpisodeType.text
    h_text = episode_hash(**args)
    assert h_message != h_text


def test_hash_format_is_sha256_hex():
    h = episode_hash(**_base_args())
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)
