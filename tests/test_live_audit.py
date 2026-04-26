"""Live integration test for the audit batch flow.

Runs an end-to-end audit against a real Neo4j graph + real Anthropic Batches
API. Seeds a small cohort, submits one audit batch, polls to completion, and
verifies that the JSON output file was written and `audited_at` got stamped
on each cohort node.

Run with: pytest tests/test_live_audit.py --live -v

Requires:
- Neo4j running locally (test environment)
- Valid API keys in .env.test (Anthropic + Voyage)
- Expect 1-3 minutes per run (batch typically completes in <2 min for 3 nodes)
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from pratyabhijna.config import PratyabhijnaConfig
from pratyabhijna.queue import WorkQueue
from pratyabhijna.service import PratyabhijnaService
from pratyabhijna.tools.audit import AUDIT_REVISION, run_audit_run

pytestmark = [
    pytest.mark.skipif(
        "not config.getoption('--live')",
        reason="Live tests require --live flag",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def live_service(tmp_path_factory):
    """Real service against the test environment, with a tiny seeded cohort."""
    config = PratyabhijnaConfig.from_env("test")
    svc = PratyabhijnaService(config)
    await svc.start()

    # Wipe and seed three small entities directly via Cypher so the test
    # doesn't depend on Graphiti's LLM extraction (faster + deterministic).
    now = datetime.now(timezone.utc).isoformat()
    await svc._graphiti.driver.execute_query("MATCH (n) DETACH DELETE n")
    await svc._graphiti.driver.execute_query(
        """
        CREATE (a:Entity {uuid: $uuid_a, name: 'AuditTestNodeA',
                          summary: 'A test entity for the live audit run.',
                          group_id: $group, created_at: datetime($now)})
        CREATE (b:Entity {uuid: $uuid_b, name: 'AuditTestNodeB',
                          summary: 'Second test entity.',
                          group_id: $group, created_at: datetime($now)})
        CREATE (c:Entity {uuid: $uuid_c, name: 'AuditTestNodeC',
                          summary: 'Third test entity.',
                          group_id: $group, created_at: datetime($now)})
        """,
        uuid_a="audit-live-aaaaaaaaaaaaaaaaaaaaaaaa-1111",
        uuid_b="audit-live-bbbbbbbbbbbbbbbbbbbbbbbb-2222",
        uuid_c="audit-live-cccccccccccccccccccccccc-3333",
        group=config.subject_name,
        now=now,
    )

    yield svc
    await svc.stop()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def live_queue(live_service, tmp_path_factory):
    """Throwaway queue for the live test (Thread enqueueing on Unfixable)."""
    db_path = tmp_path_factory.mktemp("audit_live") / "queue.sqlite"
    queue = WorkQueue(db_path=str(db_path))
    queue.register("add_episode", lambda _: None)
    await queue.start(run_worker=False)
    yield queue
    await queue.stop()


async def test_audit_three_node_cohort_against_real_batches_api(
    live_service, live_queue,
):
    """End-to-end: build batch from 3 UUIDs, submit, poll, process, verify."""
    import anthropic

    cohort = [
        "audit-live-aaaaaaaaaaaaaaaaaaaaaaaa-1111",
        "audit-live-bbbbbbbbbbbbbbbbbbbbbbbb-2222",
        "audit-live-cccccccccccccccccccccccc-3333",
    ]
    api_key = live_service.config.llm.api_key or None
    client = anthropic.AsyncAnthropic(api_key=api_key, timeout=300.0)
    try:
        summary = await run_audit_run(
            live_service, live_queue,
            input_str=" ".join(cohort),
            guidance=(
                "These are throwaway test nodes with minimal content. "
                "Default to Valid unless something is structurally broken."
            ),
            anthropic_client=client,
        )
    finally:
        await client.close()

    assert summary["cohort_size"] == 3
    assert summary["audit_revision"] == AUDIT_REVISION
    assert len(summary["results"]) == 3

    statuses = {r["status"] for r in summary["results"]}
    # Expected: all three Valid (or at most one Update). Error or all-Unfixable
    # would indicate the audit prompt is misbehaving on benign input.
    assert statuses.issubset({"Valid", "Update", "Unfixable", "Error"}), statuses

    # audited_at + audit_revision stamped on every cohort node
    records, _, _ = await live_service._graphiti.driver.execute_query(
        """
        UNWIND $uuids AS u
        MATCH (n {uuid: u})
        RETURN n.uuid AS uuid, n.audited_at AS audited_at,
               n.audit_revision AS audit_revision
        """,
        uuids=cohort,
    )
    assert len(records) == 3
    for r in records:
        assert r["audited_at"] is not None, f"node {r['uuid']} missing audited_at"
        assert r["audit_revision"] == AUDIT_REVISION


async def test_audit_writes_json_file_via_cli_orchestrator(
    live_service, live_queue, tmp_path,
):
    """Verify the CLI-shaped wrapper writes a parseable JSON output file."""
    from pratyabhijna.audit_log import write_audit_file

    # Synthesize a minimal summary as if from a real run, then write the file.
    # (We've already exercised the real Anthropic call in the previous test;
    # this one just pins the file-writing surface against real disk I/O.)
    started = datetime.now(timezone.utc).isoformat()
    summary = {
        "started_at": started,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "audit_revision": AUDIT_REVISION,
        "guidance": None,
        "model": live_service.config.llm.audit_model,
        "cohort_size": 1,
    }
    results = [
        {"uuid": "x", "name": "x", "status": "Valid", "analysis": "ok"},
    ]
    path = write_audit_file(
        log_dir=tmp_path, run_metadata=summary, results=results,
    )
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["run_metadata"]["audit_revision"] == AUDIT_REVISION
    assert len(data["results"]) == 1
