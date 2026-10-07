"""Budget-state writer (task 9.2) and the budget pre-check (task 9.4; COST-05).

The writer turns AWS Budgets notifications into the shared ``budget-state`` flag; the pre-check
reads that flag and refuses with ``BUDGET_EXCEEDED`` before any provider call. Messages are
synthetic and carry no account identifier (``<account-id>``). The SNS text format below is the
documented AWS Budgets e-mail/SNS body; :func:`classify` also accepts percentage thresholds.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from finplan_contracts.budget import STATE_PARAMETER
from finplan_platform.core.budget import SsmBudgetStateReader, budget_precheck
from finplan_platform.core.errors import PlatformError
from finplan_platform.core.ingestion_budget import check_budget_state
from finplan_platform.handlers import budget_state

NOW = datetime(2026, 1, 12, 14, 30, tzinfo=UTC)


def _msg(alert_type: str, threshold: str, budgeted: str = "$50.00") -> str:
    return (
        "AWS Budget Notification January 12, 2026\nAWS Account <account-id>\n\nDear AWS Customer,\n\n"
        f"You requested that we alert you when the {alert_type} Cost associated with your finplan-shared-financialplanning-project-budget budget is greater than {threshold}.\n\n"
        f"Budget Name: finplan-shared-financialplanning-project-budget\nBudget Type: Cost\nBudgeted Amount: {budgeted}\nAlert Type: {alert_type}\nAlert Threshold: > {threshold}\n"
    )


def _event(message: str, subject: str = "AWS Budgets: finplan-shared-financialplanning-project-budget has exceeded your alert threshold") -> dict[str, Any]:
    return {"Records": [{"EventSource": "aws:sns", "Sns": {"Subject": subject, "Message": message}}]}


# ------------------------------------------------------------------ classification
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (_msg("ACTUAL", "$50.00"), "enforced"),
        (_msg("ACTUAL", "$40.00"), "alert"),  # 80%: alert only, no deny
        (_msg("ACTUAL", "$25.00"), "alert"),
        (_msg("FORECASTED", "$50.00"), "alert"),  # forecast above the cap alerts, never enforces
        (_msg("ACTUAL", "100%"), "enforced"),
        (_msg("ACTUAL", "80%"), "alert"),  # a percentage threshold is never compared with dollars
        ("Your budget action for finplan-shared-financialplanning-project-budget has been executed.", "enforced"),
        ("hello", None),
    ],
)
def test_classify_budget_notifications(message: str, expected: str | None) -> None:
    assert budget_state.classify(message) == expected


def test_writer_sets_enforced_on_the_cap_only(ssm: Any) -> None:
    out = budget_state.handler(_event(_msg("ACTUAL", "$40.00")), ssm=ssm, now=NOW)
    assert out == {"results": ["alert"]}
    with pytest.raises(ssm.exceptions.ParameterNotFound):
        ssm.get_parameter(Name=STATE_PARAMETER)
    budget_state.handler(_event(_msg("ACTUAL", "$50.00")), ssm=ssm, now=NOW)
    value = json.loads(ssm.get_parameter(Name=STATE_PARAMETER)["Parameter"]["Value"])
    assert value == {"enforced": True, "since": "2026-01-12T14:30:00Z", "source": "aws-budgets-notification", "state": "enforced"}


def test_writer_never_clears_the_flag(ssm: Any) -> None:
    budget_state.handler(_event(_msg("ACTUAL", "$50.00")), ssm=ssm, now=NOW)
    budget_state.handler(_event(_msg("ACTUAL", "$25.00")), ssm=ssm, now=NOW)
    assert json.loads(ssm.get_parameter(Name=STATE_PARAMETER)["Parameter"]["Value"])["state"] == "enforced"


def test_writer_refuses_any_other_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINPLAN_BUDGET_STATE_PARAMETER", "/finplan/shared/financialplanning/config/budget-allocation")
    with pytest.raises(ValueError):
        budget_state.handler(_event(_msg("ACTUAL", "$50.00")), ssm=object())


def test_writer_fits_inline_code_limit() -> None:
    from infra.stacks.tooling import INLINE_CODE_LIMIT, WRITER_SOURCE

    assert len(WRITER_SOURCE.read_bytes()) <= INLINE_CODE_LIMIT


# ------------------------------------------------------------------ COST-05 pre-check
@pytest.mark.parametrize("state", ["enforced", json.dumps({"state": "enforced"}), {"enforced": True}])
def test_precheck_refuses_while_enforced_COST_05(ctx: Any, state: Any) -> None:
    with pytest.raises(PlatformError) as ei:
        budget_precheck(ctx, "platform_infra", state=state)
    assert ei.value.code == "BUDGET_EXCEEDED" and ei.value.retryable is False
    assert ei.value.details["budget_category"] == "platform_infra"


def test_precheck_allows_without_flag_and_checks_category_COST_05(ctx: Any) -> None:
    budget_precheck(ctx, "platform_infra", state=None)
    budget_precheck(ctx, "platform_infra")  # no reader: treated as never enforced
    with pytest.raises(PlatformError) as ei:
        budget_precheck(ctx, "not_a_category", state=None)
    assert ei.value.code == "VALIDATION_FAILED"
    with pytest.raises(PlatformError) as ei:
        budget_precheck(ctx, "gpu", estimated_usd=1000.0, state=None)
    assert ei.value.code == "BUDGET_EXCEEDED"


def test_precheck_reads_the_flag_from_ssm_and_fails_closed_COST_05(ctx: Any, ssm: Any) -> None:
    reader = SsmBudgetStateReader(ssm)
    budget_precheck(ctx, "platform_infra", reader=reader)  # parameter absent
    budget_state.handler(_event(_msg("ACTUAL", "$50.00")), ssm=ssm, now=NOW)
    with pytest.raises(PlatformError) as ei:
        budget_precheck(ctx, "platform_infra", reader=reader)
    assert ei.value.code == "BUDGET_EXCEEDED"

    class Broken:
        def get_parameter(self, **_: Any) -> Any:
            raise TimeoutError("network")

    with pytest.raises(PlatformError) as ei:
        budget_precheck(ctx, "platform_infra", reader=SsmBudgetStateReader(Broken()))
    assert ei.value.code == "DEPENDENCY_UNAVAILABLE" and ei.value.retryable is True


@pytest.mark.parametrize("state", [None, "enforced", json.dumps({"state": "enforced"}), json.dumps({"state": "normal"})])
def test_precheck_agrees_with_the_ingestion_gate(ctx: Any, state: Any) -> None:
    def outcome(fn: Any) -> str | None:
        try:
            fn()
        except PlatformError as exc:
            return exc.code
        return None

    assert outcome(lambda: budget_precheck(ctx, "platform_infra", state=state)) == outcome(lambda: check_budget_state(ctx, state))


def test_capped_ingestion_makes_no_provider_call_and_no_snapshot_COST_05(ctx: Any, ssm: Any, s3: Any, buckets: dict[str, str], clock: Any) -> None:
    """Ingestion while capped: BUDGET_EXCEEDED, no provider call, no snapshot (spec scenario)."""
    from finplan_platform.core.artifacts import ArtifactStore
    from finplan_platform.core.calendar import calendar_for_provider
    from finplan_platform.core.config import load_config
    from finplan_platform.core.ingestion import IngestionDeps, ingest
    from finplan_platform.core.repository import MetadataRepository, create_tables
    from finplan_platform.providers.fixture import FixtureProvider

    from tests.fakes import FakeDynamoDB

    cfg = load_config("beta")
    ddb = FakeDynamoDB()
    create_tables(ddb, "beta")
    provider = FixtureProvider(dataset_id=cfg.dataset_id, instrument_id=str(cfg.dataset["instrument"]), clock=clock)
    calls: list[Any] = []
    real_fetch = provider.fetch

    def counting_fetch(*a: Any, **k: Any) -> Any:
        calls.append(a)
        return real_fetch(*a, **k)

    provider.fetch = counting_fetch  # type: ignore[method-assign]
    reader = SsmBudgetStateReader(ssm)
    deps = IngestionDeps(config=cfg, repo=MetadataRepository(ddb, "beta"), store=ArtifactStore(s3, buckets, clock=clock), provider=provider, calendar=calendar_for_provider(provider.describe().provider_id), budget_gate=lambda c: budget_precheck(c, "platform_infra", reader=reader))
    budget_state.handler(_event(_msg("ACTUAL", "$50.00")), ssm=ssm, now=NOW)
    req = {"dataset_id": cfg.dataset_id, "start_date": "2026-01-09", "end_date": "2026-01-09", "granularity": "daily", "idempotency_key": "capped-1"}
    with pytest.raises(PlatformError) as ei:
        ingest(ctx, req, deps=deps)
    assert ei.value.code == "BUDGET_EXCEEDED" and ei.value.retryable is False
    assert calls == []
    assert ddb.scan(TableName="finplan-beta-financialplanning-snapshot-catalog")["Items"] == []
    assert s3.list_objects_v2(Bucket=buckets["snapshots"]).get("KeyCount", 0) == 0
