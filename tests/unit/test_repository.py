"""Transactional repository: MDS-03 (atomic version create + head move), MDS-05 (idempotency),
MDS-04 (conditional transitions, immutable content), MDS-06 (audit events), MDS-01 (records conform).

Tests marked with ``repo_any`` run against both the in-memory fake and moto.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from finplan_contracts.validate import validate

from finplan_platform.core.audit import audit_event
from finplan_platform.core.errors import PlatformError
from finplan_platform.core.repository import (
    TABLES,
    Check,
    Cond,
    HeadMove,
    MetadataRepository,
    Mutation,
    PutConditional,
    PutNew,
    Transition,
    version_create_mutation,
)
from tests.fakes import FakeDynamoDB
from tests.fakes.records import plan_doc, portfolio_doc, version_doc


def _seed_plan(repo: MetadataRepository, ctx: Any, *, revision: int = 0) -> str:
    pf = portfolio_doc(ctx)
    pl = plan_doc(ctx, pf["portfolio_id"], revision=revision)
    repo.commit([PutNew("portfolio", doc=pf), PutNew("plan", doc=pl)], audit=[audit_event(ctx, record_id=pl["plan_id"], record_type="plan", operation="create_plan", new={"revision": revision})])
    return pl["plan_id"]


def _create_version(repo: MetadataRepository, ctx: Any, plan_id: str, expected: int, *, key: str, parent: str | None = None, content: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
    body = {"plan_id": plan_id, "expected_revision": expected, "parent_plan_version_id": parent, "content": content or {"x": 1}}
    holder: dict[str, Any] = {}

    def execute() -> Mutation:
        doc = version_doc(ctx, plan_id, parent_plan_version_id=parent)
        holder["doc"] = doc
        response = {"plan_version_id": doc["plan_version_id"], "checksum": doc["checksum"], "revision": expected + 1}
        return version_create_mutation(repo, ctx, plan_id=plan_id, expected_revision=expected, version_doc=doc, contract_version="0.1.0", response=response)

    out = repo.run_idempotent(ctx, operation="create_plan_version", idempotency_key=key, request_body=body, execute=execute)
    return out.response, out.replayed


# ------------------------------------------------------------------ MDS-03
def test_version_create_moves_head_and_writes_audit(repo_any: MetadataRepository, ctx: Any) -> None:
    plan_id = _seed_plan(repo_any, ctx)
    resp, replayed = _create_version(repo_any, ctx, plan_id, 0, key="k-root")
    assert not replayed
    plan = repo_any.require("plan", plan_id)
    assert plan.head()["revision"] == 1
    assert plan.view()["head"] == {"current_version_id": resp["plan_version_id"], "revision": 1}
    version = repo_any.require("plan_version", resp["plan_version_id"])
    assert version.doc["checksum"] == resp["checksum"]
    # MDS-01: committed records validate against the pinned contract schemas
    assert validate(version.doc, "plan-version").valid
    assert validate(plan.view(), "plan").valid
    events = repo_any.audit_events(plan_id)
    assert [e.operation for e in events] == ["create_plan", "create_plan_version"]
    assert events[-1].correlation_id == ctx.correlation_id
    assert events[-1].prior == {"revision": 0} and events[-1].new["revision"] == 1


def test_stale_expected_revision_is_conflict_and_commits_nothing(repo_any: MetadataRepository, ctx: Any) -> None:
    plan_id = _seed_plan(repo_any, ctx, revision=4)
    _create_version(repo_any, ctx, plan_id, 4, key="first")
    with pytest.raises(PlatformError) as ei:
        _create_version(repo_any, ctx, plan_id, 4, key="second")
    assert ei.value.code == "CONFLICT"
    assert ei.value.details["current_revision"] == 5 and ei.value.details["expected_revision"] == 4
    versions, _ = repo_any.query_index("plan_version", "plan-index", plan_id)
    assert len(versions) == 1
    assert repo_any.get_idempotency(_scope(ctx), "second") is None


def _scope(ctx: Any) -> Any:
    from finplan_platform.core.repository import IdempotencyScope

    return IdempotencyScope(ctx.caller.principal, ctx.env, "create_plan_version")


def test_concurrent_children_exactly_one_winner(ddb: FakeDynamoDB, ctx_factory: Any) -> None:
    """Two writers both read revision 4 and commit concurrently: one wins, one CONFLICT (MDS-03)."""
    repo = MetadataRepository(ddb, "beta")
    seed_ctx = ctx_factory()
    plan_id = _seed_plan(repo, seed_ctx, revision=4)
    barrier = threading.Barrier(2)
    # both transactions are released together only after both have prepared their writes
    ddb.before_transact_hooks.append(lambda _items: barrier.wait(timeout=5))
    results: list[Any] = []

    def worker(i: int) -> None:
        c = ctx_factory(f"arn:aws:iam::<account-id>:role/finplan-beta-financialplanning-writer-{i}")
        try:
            results.append(_create_version(repo, c, plan_id, 4, key=f"k{i}"))
        except PlatformError as exc:
            results.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    wins = [r for r in results if not isinstance(r, PlatformError)]
    errors = [r for r in results if isinstance(r, PlatformError)]
    assert len(wins) == 1 and len(errors) == 1
    assert errors[0].code == "CONFLICT"
    assert repo.require("plan", plan_id).head()["revision"] == 5
    versions, _ = repo.query_index("plan_version", "plan-index", plan_id)
    assert [v.id for v in versions] == [wins[0][0]["plan_version_id"]]


# ------------------------------------------------------------------ MDS-05
def test_retry_with_same_key_and_body_replays(repo_any: MetadataRepository, ctx: Any) -> None:
    plan_id = _seed_plan(repo_any, ctx)
    first, r1 = _create_version(repo_any, ctx, plan_id, 0, key="retry-key")
    again, r2 = _create_version(repo_any, ctx, plan_id, 0, key="retry-key")
    assert (r1, r2) == (False, True)
    assert again == first
    versions, _ = repo_any.query_index("plan_version", "plan-index", plan_id)
    assert len(versions) == 1
    record = repo_any.get_idempotency(_scope(ctx), "retry-key")
    assert record is not None and validate(record, "idempotency").valid


def test_same_key_different_body_is_key_reused(repo_any: MetadataRepository, ctx: Any) -> None:
    plan_id = _seed_plan(repo_any, ctx)
    _create_version(repo_any, ctx, plan_id, 0, key="reuse")
    with pytest.raises(PlatformError) as ei:
        _create_version(repo_any, ctx, plan_id, 1, key="reuse")
    assert ei.value.code == "IDEMPOTENCY_KEY_REUSED" and ei.value.retryable is False


def test_same_key_different_principal_is_independent(repo_any: MetadataRepository, ctx_factory: Any) -> None:
    a = ctx_factory("arn:aws:iam::<account-id>:role/finplan-beta-financialplanning-website-role")
    b = ctx_factory("arn:aws:iam::<account-id>:role/finplan-beta-financelambdastool-tool-role-plan-writer")
    plan_id = _seed_plan(repo_any, a)
    ra, _ = _create_version(repo_any, a, plan_id, 0, key="shared-key")
    rb, replayed = _create_version(repo_any, b, plan_id, 1, key="shared-key")
    assert not replayed and ra["plan_version_id"] != rb["plan_version_id"]


def test_missing_or_malformed_key_is_validation_failed(repo: MetadataRepository, ctx: Any) -> None:
    for bad in (None, "", "has space", "x" * 129):
        with pytest.raises(PlatformError) as ei:
            repo.run_idempotent(ctx, operation="op", idempotency_key=bad, request_body={}, execute=lambda: Mutation([], {}))
        assert ei.value.code == "VALIDATION_FAILED" and ei.value.details["pointer"] == "/idempotency_key"


def test_failed_idempotency_condition_commits_nothing(ddb: FakeDynamoDB, ctx: Any) -> None:
    """A concurrent duplicate commits first: the loser's version and head move are not committed (MDS-05)."""
    repo = MetadataRepository(ddb, "beta")
    plan_id = _seed_plan(repo, ctx)
    winner: dict[str, Any] = {}

    def racing_duplicate(items: list[dict[str, Any]]) -> None:
        # Fires once, inside the loser's commit, after its idempotency lookup missed:
        # a duplicate delivery with the same key and body commits first.
        ddb.before_transact_hooks.clear()
        winner["resp"], _ = _create_version(repo, ctx, plan_id, 0, key="dup")

    ddb.before_transact_hooks.append(racing_duplicate)
    resp, replayed = _create_version(repo, ctx, plan_id, 0, key="dup")
    assert replayed and resp == winner["resp"]
    versions, _ = repo.query_index("plan_version", "plan-index", plan_id)
    assert [v.id for v in versions] == [winner["resp"]["plan_version_id"]]
    assert repo.require("plan", plan_id).head()["revision"] == 1


def test_transaction_is_all_or_nothing(repo_any: MetadataRepository, ctx: Any) -> None:
    plan_id = _seed_plan(repo_any, ctx)
    doc = version_doc(ctx, plan_id)
    with pytest.raises(PlatformError) as ei:
        repo_any.commit(
            [PutNew("plan_version", doc=doc), HeadMove("plan", record_id=plan_id, expected_revision=0, set_attrs={"current_version_id": doc["plan_version_id"]}), Check("portfolio", record_id="pf_missing")]
        )
    assert ei.value.code == "NOT_FOUND"
    assert repo_any.get("plan_version", doc["plan_version_id"]) is None
    assert repo_any.require("plan", plan_id).head()["revision"] == 0


# ------------------------------------------------------------------ MDS-04
def _committed_version(repo: MetadataRepository, ctx: Any, status: str = "pending_validation") -> dict[str, Any]:
    plan_id = _seed_plan(repo, ctx)
    doc = version_doc(ctx, plan_id, status=status)
    repo.commit([PutNew("plan_version", doc=doc)])
    return doc


def test_allowed_transition_with_audit(repo_any: MetadataRepository, ctx: Any) -> None:
    doc = _committed_version(repo_any, ctx)
    rec = repo_any.transition(ctx, "plan_version", doc["plan_version_id"], to_status="validated", operation="validate_plan_version")
    assert rec.doc["status"] == "validated"
    assert repo_any.require("plan_version", doc["plan_version_id"]).attrs["status"] == "validated"
    events = repo_any.audit_events(doc["plan_version_id"])
    assert len(events) == 1 and events[0].prior == {"status": "pending_validation"} and events[0].new == {"status": "validated"}


def test_invalid_to_validated_refused_and_nothing_changes(repo_any: MetadataRepository, ctx: Any) -> None:
    doc = _committed_version(repo_any, ctx, status="invalid")
    before = repo_any.require("plan_version", doc["plan_version_id"])
    with pytest.raises(PlatformError) as ei:
        repo_any.transition(ctx, "plan_version", doc["plan_version_id"], to_status="validated", operation="validate_plan_version")
    assert ei.value.code == "PRECONDITION_FAILED"
    assert repo_any.require("plan_version", doc["plan_version_id"]) == before
    assert repo_any.audit_events(doc["plan_version_id"]) == []


def test_stale_status_is_precondition_failed(repo: MetadataRepository, ctx: Any) -> None:
    doc = _committed_version(repo, ctx)
    stale = Transition("plan_version", record_id=doc["plan_version_id"], old_doc=doc, new_doc={**doc, "status": "validated"})
    repo.transition(ctx, "plan_version", doc["plan_version_id"], to_status="invalid", operation="validate_plan_version")
    with pytest.raises(PlatformError) as ei:
        repo.commit([stale])
    assert ei.value.code == "PRECONDITION_FAILED" and ei.value.details["current_status"] == "invalid"


def test_content_edit_is_immutable_record(repo_any: MetadataRepository, ctx: Any) -> None:
    doc = _committed_version(repo_any, ctx)
    edited = {**doc, "checksum": "sha256:" + "1" * 64}
    with pytest.raises(PlatformError) as ei:
        Transition("plan_version", record_id=doc["plan_version_id"], old_doc=doc, new_doc={**edited, "status": "validated"})
    assert ei.value.code == "IMMUTABLE_RECORD" and "checksum" in ei.value.details["fields"]
    with pytest.raises(PlatformError) as ei2:
        repo_any.transition(ctx, "plan_version", doc["plan_version_id"], to_status="validated", operation="x", changes={"content": {"allocation": {}}})
    assert ei2.value.code == "IMMUTABLE_RECORD"
    # re-putting a committed ID (an overwrite attempt) is refused too
    with pytest.raises(PlatformError) as ei3:
        repo_any.commit([PutNew("plan_version", doc=edited)])
    assert ei3.value.code == "IMMUTABLE_RECORD"
    assert repo_any.require("plan_version", doc["plan_version_id"]).doc["checksum"] == doc["checksum"]


# ------------------------------------------------------------------ MDS-06
def test_audit_events_are_append_only(repo_any: MetadataRepository, ctx: Any) -> None:
    ev = audit_event(ctx, record_id="pub_X", record_type="publication", operation="publish", new={"status": "published"}, plan_version_id="pv_X", checksum="sha256:" + "a" * 64)
    repo_any.commit([], audit=[ev])
    with pytest.raises(PlatformError):
        repo_any.commit([], audit=[ev])  # the same event cannot be written twice (no overwrite)
    events = repo_any.audit_events("pub_X")
    assert len(events) == 1
    e = events[0]
    assert (e.caller["principal"], e.correlation_id, e.details["plan_version_id"]) == (ctx.caller.principal, ctx.correlation_id, "pv_X")
    assert e.at.endswith("Z")


def test_no_repository_api_updates_or_deletes_audit_items() -> None:
    import inspect

    import finplan_platform.core.repository as mod

    src = inspect.getsource(mod)
    assert "delete_item" not in src and "update_item" not in src
    assert TABLES["audit_event"].sort_key == "sk"


# ------------------------------------------------------------------ service errors
def test_throttling_maps_to_dependency_unavailable(ddb: FakeDynamoDB, ctx: Any) -> None:
    repo = MetadataRepository(ddb, "beta")
    ddb.fail_next("TransactWriteItems", "ThrottlingException")
    with pytest.raises(PlatformError) as ei:
        repo.commit([PutConditional("staged_output", item={"pk": "run_X", "doc": "{}"}, condition=Cond.not_exists())])
    assert ei.value.code == "DEPENDENCY_UNAVAILABLE" and ei.value.retryable


def test_page_tokens_are_opaque_and_stable(repo_any: MetadataRepository, ctx: Any) -> None:
    plan_id = _seed_plan(repo_any, ctx)
    ids = []
    for i in range(5):
        r, _ = _create_version(repo_any, ctx, plan_id, i, key=f"p{i}")
        ids.append(r["plan_version_id"])
        ctx.clock.advance(seconds=1)  # type: ignore[attr-defined]
    page1, tok = repo_any.query_index("plan_version", "plan-index", plan_id, limit=2)
    page2, tok2 = repo_any.query_index("plan_version", "plan-index", plan_id, limit=2, page_token=tok)
    page3, tok3 = repo_any.query_index("plan_version", "plan-index", plan_id, limit=2, page_token=tok2)
    got = [r.id for r in page1 + page2 + page3]
    assert got == list(reversed(ids))
    assert tok and "/" not in tok and "bucket" not in tok
    with pytest.raises(PlatformError) as ei:
        repo_any.query_index("plan_version", "plan-index", plan_id, page_token="!!notatoken")
    assert ei.value.code == "VALIDATION_FAILED"
    _ = tok3
