"""Shared fixtures and helpers for the plan API suites (API-owned).

Import the fixtures into a test module with ``from tests.api_support import *  # noqa: F403``
(pytest discovers fixtures in the module namespace). Everything is offline: the in-memory
DynamoDB fake (``ddb`` from ``tests/conftest.py``), moto S3 (``artifacts``), a frozen clock and
``config/beta.json``.

Principals are synthetic role ARNs with the ``<account-id>`` placeholder, named after the
configured role-name patterns:

=================  =========================================================  ============
client             role                                                       class
=================  =========================================================  ============
``website``        ``finplan-beta-financialplanning-website-backend``         website
``operator``       ``finplan-beta-financialplanning-operator``                operator
``platform``       ``finplan-beta-financialplanning-plan-api-handler-role``   platform
``reader``         ``finplan-beta-financelambdastool-tool-role-reader``        reader
``submitter``      ``finplan-beta-financelambdastool-tool-role-submitter``     submitter
``writer``         ``finplan-beta-financelambdastool-tool-role-plan-writer``   plan-writer
``fm_job``         ``finplan-beta-financemodel-job-execution-role``           financemodel-job
``fm_job_api``     ``finplan-beta-financemodel-job-api-handler-role``         financemodel-job-api
``gamma_reader``   ``finplan-gamma-financelambdastool-tool-role-reader``       (none: other env)
``gamma_platform`` ``finplan-gamma-financialplanning-plan-api-handler-role``  (none: other env)
=================  =========================================================  ============
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from finplan_contracts.schemas import load_store
from finplan_platform.core.artifacts import ArtifactStore, key_snapshot_manifest, key_snapshot_payload
from finplan_platform.core.audit import audit_event
from finplan_platform.core.clock import FrozenClock
from finplan_platform.core.config import load_config
from finplan_platform.core.context import Caller, OperationContext
from finplan_platform.core.ids import IdFactory
from finplan_platform.core.repository import MetadataRepository, PutNew
from finplan_platform.core.services import Services
from finplan_platform.handlers.api import ApiApp, LocalClient

from tests.fakes.records import CONFIGURATION_ID, DEFAULT_CONTENT, FOREIGN_MODEL_VERSION, FOREIGN_RUN_ID

__all__ = [
    "PRINCIPALS",
    "Clients",
    "Flow",
    "all_strings",
    "app",
    "clients",
    "content",
    "flow",
    "role",
    "seed_snapshot",
    "svc",
]


def role(name: str) -> str:
    return f"arn:aws:iam::<account-id>:role/{name}"


PRINCIPALS: dict[str, str] = {
    "website": role("finplan-beta-financialplanning-website-backend"),
    "operator": role("finplan-beta-financialplanning-operator"),
    "platform": role("finplan-beta-financialplanning-plan-api-handler-role"),
    "reader": role("finplan-beta-financelambdastool-tool-role-reader"),
    "submitter": role("finplan-beta-financelambdastool-tool-role-submitter"),
    "writer": role("finplan-beta-financelambdastool-tool-role-plan-writer"),
    "fm_job": role("finplan-beta-financemodel-job-execution-role"),
    "fm_job_api": role("finplan-beta-financemodel-job-api-handler-role"),
    "gamma_reader": role("finplan-gamma-financelambdastool-tool-role-reader"),
    "gamma_platform": role("finplan-gamma-financialplanning-plan-api-handler-role"),
    "stranger": role("some-unrelated-role"),
}


def content(**changes: Any) -> dict[str, Any]:
    """The synthetic default plan content (SPY 0.6 + cash 0.4), with top-level overrides."""
    out = copy.deepcopy(DEFAULT_CONTENT)
    out.update(changes)
    return out


def all_strings(doc: Any) -> Iterator[tuple[str, str]]:
    """(json-pointer, value) for every string in a document."""

    def walk(node: Any, ptr: str) -> Iterator[tuple[str, str]]:
        if isinstance(node, str):
            yield ptr, node
        elif isinstance(node, dict):
            for k, v in node.items():
                yield from walk(v, f"{ptr}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                yield from walk(v, f"{ptr}/{i}")

    yield from walk(doc, "")


# ===================================================================== fixtures
@pytest.fixture
def svc(ddb: Any, artifacts: ArtifactStore) -> Services:
    return Services(cfg=load_config("beta"), repo=MetadataRepository(ddb, "beta"), artifacts=artifacts)


@pytest.fixture
def app(svc: Services, clock: FrozenClock) -> ApiApp:
    return ApiApp(svc, clock=clock, ids=IdFactory(clock))


@dataclass
class Clients:
    app: ApiApp

    def __getattr__(self, name: str) -> LocalClient:
        if name in PRINCIPALS:
            return LocalClient(self.app, PRINCIPALS[name])
        raise AttributeError(name)

    def as_principal(self, arn: str, **kw: Any) -> LocalClient:
        return LocalClient(self.app, arn, **kw)

    @property
    def anonymous(self) -> LocalClient:
        return LocalClient(self.app, "")


@pytest.fixture
def clients(app: ApiApp) -> Clients:
    return Clients(app)


# ===================================================================== snapshots
def _payload_name() -> str:
    try:
        from finplan_platform.core.snapshots import PAYLOAD_NAME

        return PAYLOAD_NAME
    except ImportError:  # pragma: no cover - snapshots module absent
        return "observations.json"


def seed_snapshot(svc: Services, clock: FrozenClock, *, status: str = "approved", contract_version: str | None = None, doc_overrides: dict[str, Any] | None = None, payload: dict[str, Any] | None = None) -> str:
    """Commit a synthetic snapshot the way ingestion does (artifacts first, then the catalog row).

    Content comes from the pinned package fixtures (``input-snapshot`` approved ETF daily and
    ``snapshot-payload`` ETF daily, coverage 2026-01-02..2026-01-09, SPY only).
    """
    from finplan_contracts.canonical import canonicalize

    store = load_store()
    ctx = OperationContext(caller=Caller("arn:aws:iam::<account-id>:role/finplan-beta-financialplanning-ingestion-handler-role"), env="beta", correlation_id="cor_seedsnapshot", clock=clock, ids=IdFactory(clock), synthetic=True)
    doc = json.loads((store.fixtures_dir("input-snapshot") / "valid" / "approved-etf-daily.json").read_text())
    body = payload if payload is not None else json.loads((store.fixtures_dir("snapshot-payload") / "valid" / "etf-daily.json").read_text())
    sid = ctx.new_id("input_snapshot_id")
    manifest = {"input_snapshot_id": sid, "payload": "synthetic", "synthetic": True}
    m = svc.store.put_once("snapshots", key_snapshot_manifest(sid), canonicalize(manifest))
    p = svc.store.put_once("snapshots", key_snapshot_payload(sid, _payload_name()), canonicalize(body))
    doc.update(
        {
            "input_snapshot_id": sid,
            "status": status,
            "manifest_checksum": m.checksum,
            "created_at": ctx.now_ts(),
            "artifacts": [m.to_ref(ctx.new_id("artifact_id"), "snapshot_manifest", synthetic=True), p.to_ref(ctx.new_id("artifact_id"), "snapshot_payload", synthetic=True)],
        }
    )
    if status != "approved":
        doc.pop("approval_rule_version", None)
    doc.update(doc_overrides or {})
    ev = audit_event(ctx, record_id=sid, record_type="snapshot_catalog", operation="seed_snapshot", new={"status": status})
    svc.repo.commit([PutNew("snapshot_catalog", doc=doc, contract_version=contract_version or store.version)], audit=[ev])
    return sid


# ===================================================================== lifecycle helper
class Flow:
    """Drives the lifecycle through the HTTP router with a given client (default: website path)."""

    def __init__(self, clients: Clients, svc: Services, clock: FrozenClock) -> None:
        self.clients = clients
        self.svc = svc
        self.clock = clock
        self._n = 0

    def key(self, prefix: str = "k") -> str:
        self._n += 1
        return f"{prefix}-{self._n:04d}"

    def ok(self, resp: tuple[int, dict[str, Any], dict[str, str]], status: int | tuple[int, ...] = (200, 201)) -> dict[str, Any]:
        code, body, _ = resp
        expected = (status,) if isinstance(status, int) else status
        assert code in expected, (code, body)
        return body

    def portfolio(self, client: LocalClient | None = None) -> dict[str, Any]:
        c = client or self.clients.website
        return self.ok(c.post("/v1/portfolios", {"name": "Synthetic portfolio", "base_currency": "USD", "synthetic": True, "idempotency_key": self.key("pf")}))

    def plan(self, client: LocalClient | None = None, portfolio: dict[str, Any] | None = None) -> dict[str, Any]:
        c = client or self.clients.website
        pf = portfolio or self.portfolio(c)
        return self.ok(c.post(f"/v1/portfolios/{pf['portfolio_id']}/plans", {"name": "Synthetic plan", "expected_revision": pf["revision"], "idempotency_key": self.key("pl")}))

    def root_body(self, snapshot_id: str, *, expected_revision: int, body_content: dict[str, Any] | None = None, key: str | None = None) -> dict[str, Any]:
        return {
            "expected_revision": expected_revision,
            "idempotency_key": key or self.key("root"),
            "domain": "finance",
            "domain_schema_version": "1.0",
            "content": body_content if body_content is not None else content(),
            "input_snapshot_id": snapshot_id,
            "configuration_id": CONFIGURATION_ID,
            "model_version": FOREIGN_MODEL_VERSION,
            "run_id": FOREIGN_RUN_ID,
        }

    def child_body(self, parent_id: str, *, expected_revision: int, body_content: dict[str, Any] | None = None, key: str | None = None) -> dict[str, Any]:
        return {
            "parent_plan_version_id": parent_id,
            "expected_revision": expected_revision,
            "idempotency_key": key or self.key("child"),
            "domain": "finance",
            "domain_schema_version": "1.0",
            "content": body_content if body_content is not None else content(),
            "reason": "synthetic manual override",
        }

    def head(self, plan_id: str, client: LocalClient | None = None) -> dict[str, Any]:
        return self.ok((client or self.clients.website).get(f"/v1/plans/{plan_id}"))["plan"]

    def root(self, plan_id: str, snapshot_id: str, *, client: LocalClient | None = None, body_content: dict[str, Any] | None = None) -> dict[str, Any]:
        c = client or self.clients.website
        rev = self.head(plan_id)["head"]["revision"]
        return self.ok(c.post(f"/v1/plans/{plan_id}/versions", self.root_body(snapshot_id, expected_revision=rev, body_content=body_content)))

    def child(self, plan_id: str, parent_id: str, *, client: LocalClient | None = None, body_content: dict[str, Any] | None = None) -> dict[str, Any]:
        c = client or self.clients.website
        rev = self.head(plan_id)["head"]["revision"]
        return self.ok(c.post(f"/v1/plans/{plan_id}/versions", self.child_body(parent_id, expected_revision=rev, body_content=body_content)))

    def validate(self, plan_version_id: str, client: LocalClient | None = None) -> dict[str, Any]:
        c = client or self.clients.website
        return self.ok(c.post(f"/v1/plan-versions/{plan_version_id}/validate", {"idempotency_key": self.key("val")}))

    def publish(self, plan_id: str, plan_version_id: str, client: LocalClient | None = None) -> dict[str, Any]:
        c = client or self.clients.website
        rev = self.ok(self.clients.website.get(f"/v1/plans/{plan_id}"))["plan"]["publication_revision"]
        return self.ok(c.post(f"/v1/plans/{plan_id}/publications", {"plan_version_id": plan_version_id, "expected_revision": rev, "idempotency_key": self.key("pub")}))

    def validated_plan(self) -> tuple[dict[str, Any], str, dict[str, Any]]:
        """(plan, snapshot_id, validated root version response)."""
        snap = seed_snapshot(self.svc, self.clock)
        plan = self.plan()
        root = self.root(plan["plan_id"], snap)
        self.validate(root["plan_version_id"])
        return plan, snap, root


@pytest.fixture
def flow(clients: Clients, svc: Services, clock: FrozenClock) -> Flow:
    return Flow(clients, svc, clock)
