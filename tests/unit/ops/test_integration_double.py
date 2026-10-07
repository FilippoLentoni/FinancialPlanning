"""The deployed integration suite against the local deployment double (tasks 11.1-11.3, offline part).

``tests/integration/lifecycle_suite.py`` is what the pipeline's beta and gamma stages run with SigV4
against the deployed plan API. Here the same flow runs through the in-process router (DynamoDB fake,
moto S3, fixture provider) as the stage role, proving every request it sends is valid and every
check it makes holds on the current code, before it ever reaches a deployment.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from finplan_platform.handlers.api import ApiApp

from tests.integration.lifecycle_suite import (
    BASE_CONTENT,
    CHANGED_CONTENT,
    IntegrationError,
    content_checksum,
    gamma_isolation_checks,
    run_key_from,
    run_lifecycle,
)
from tests.unit.ops.test_smoke_double import OPERATOR, LocalTransport, double  # noqa: F401 - fixture

DATASET = "finance/etf-daily/SPY"
TODAY = date(2026, 1, 12)


def _items(ddb: Any, table: str) -> list[dict[str, Any]]:
    return ddb.scan(TableName=f"finplan-beta-financialplanning-{table}")["Items"]


def test_lifecycle_suite_passes_on_the_double(double: ApiApp, ddb: Any) -> None:  # noqa: F811
    t = LocalTransport(double, OPERATOR)
    lines: list[str] = []
    r = run_lifecycle(t, run_key="rel-a-1", dataset_id=DATASET, today=TODAY, log=lines.append)
    assert r.checksum == content_checksum(CHANGED_CONTENT) != content_checksum(BASE_CONTENT)
    assert r.root_id != r.no_effect_id != r.plan_version_id
    assert r.execution_id.startswith("exe_") and r.publication_id.startswith("pub_")
    assert any("read-back verified" in line for line in lines)
    # every write stayed on this run's synthetic records
    writes = [p for m, p in t.calls if m == "POST"]
    assert all(p.startswith(("/v1/portfolios", "/v1/ingestions", f"/v1/plans/{r.plan_id}", "/v1/plan-versions/", f"/v1/publications/{r.publication_id}")) for p in writes)
    for table in ("portfolio", "plan-version", "publication", "execution"):
        assert _items(ddb, table) and all("synthetic" in json.dumps(i, default=str) for i in _items(ddb, table))


def test_each_run_uses_its_own_keys_and_records(double: ApiApp, clock: Any) -> None:  # noqa: F811
    first = run_lifecycle(LocalTransport(double, OPERATOR), run_key="rel-a-1", dataset_id=DATASET, today=TODAY, log=lambda _m: None)
    clock.advance(minutes=5)
    second = run_lifecycle(LocalTransport(double, OPERATOR), run_key="rel-a-2", dataset_id=DATASET, today=TODAY, log=lambda _m: None)
    assert {first.portfolio_id, first.plan_id, first.plan_version_id}.isdisjoint({second.portfolio_id, second.plan_id, second.plan_version_id})


def test_run_key_is_idempotency_key_safe() -> None:
    key = run_key_from("beta", "rel_2026.10.07+abc", "build:1234", "20261007120000")
    assert key == "beta-rel_2026-10-07-abc-build-1234-20261007120000"
    assert run_key_from(None, "") == "run" and len(run_key_from("x" * 200)) == 64


class Broken(LocalTransport):
    """A double whose deployed API lost a guarantee (here: live execution accepted)."""

    def call(self, method: str, path: str, body: Any = None) -> Any:
        if method == "POST" and path.endswith("/executions") and body and body.get("mode") == "live":
            return 201, {"execution_id": "exe_x", "mode": "live"}, {}
        return super().call(method, path, body)


def test_the_suite_fails_when_a_guarantee_breaks(double: ApiApp) -> None:  # noqa: F811
    with pytest.raises(IntegrationError, match="live execution"):
        run_lifecycle(Broken(double, OPERATOR), run_key="rel-b", dataset_id=DATASET, today=TODAY, log=lambda _m: None)


# ------------------------------------------------------------------ gamma isolation (moto stand-ins)
class _Denied(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _Client:
    def __init__(self, allow: bool, code: str) -> None:
        self.allow, self.code = allow, code

    def __getattr__(self, name: str) -> Any:
        def call(**kw: Any) -> Any:
            if name == "get_caller_identity":
                return {"Account": "<account-id>"}
            if self.allow or "/gamma/" in str(kw.get("Name", "")):
                return {}
            raise _Denied(self.code)

        return call


class _Session:
    def __init__(self, allow: bool) -> None:
        self.allow = allow

    def client(self, service: str) -> _Client:
        return _Client(self.allow, {"s3": "403", "ssm": "AccessDeniedException", "dynamodb": "AccessDeniedException"}.get(service, "AccessDenied"))


def test_gamma_isolation_checks_require_denials() -> None:
    assert gamma_isolation_checks(_Session(allow=False)) == ["AccessDeniedException", "AccessDeniedException", "AccessDeniedException", "403"]
    with pytest.raises(IntegrationError, match="isolation is broken"):
        gamma_isolation_checks(_Session(allow=True))
