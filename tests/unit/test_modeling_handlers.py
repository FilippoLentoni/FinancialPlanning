"""modeling-sources and modeling-report Lambda handlers against moto (DynamoDB, S3, STS).

Provider payloads and the report read model are controlled fixtures: this proves the event
contracts, the CLI wiring, the immutable evidence/revisions and the stable S3 views, not live
collection or model performance.
"""
from __future__ import annotations

import json
from typing import Any, Iterator

import pytest
from moto import mock_aws

from finplan_platform.core.config import BUCKET_ROLES
from finplan_platform.core.modeling_evidence import ModelingEvidence, _id
from finplan_platform.core.repository import create_tables
from finplan_platform.handlers import modeling
from finplan_platform.providers import current_quotes, financial_research
from tests.unit.test_collect_modeling_sources import AT, supplied

REGION = "us-east-2"
EXPERIMENT = "beta-competition-v1"
VIEWS = f"modeling-views/v1/{EXPERIMENT}/"
DAY = "2026-10-12"


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[dict[str, Any]]:
    with mock_aws():
        import boto3

        create_tables(boto3.client("dynamodb", region_name=REGION), "beta")
        s3 = boto3.client("s3", region_name=REGION)
        names = {role: f"example-beta-{role}" for role in BUCKET_ROLES}
        for name in names.values():
            s3.create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": REGION})
        monkeypatch.setenv("FINPLAN_ENV", "beta")
        monkeypatch.setenv("AWS_REGION", REGION)
        monkeypatch.setenv("FINPLAN_BUCKET_REPORTS", names["reports"])
        monkeypatch.delenv("FINPLAN_KMS_KEY_ARN", raising=False)
        monkeypatch.setenv("FINPLAN_MODELING_TMP", str(tmp_path))
        yield {"s3": s3, "bucket": names["reports"], "tmp": tmp_path}


def keys(aws: dict[str, Any], prefix: str) -> list[str]:
    return sorted(o["Key"] for o in aws["s3"].list_objects_v2(Bucket=aws["bucket"], Prefix=prefix).get("Contents", []))


def body(aws: dict[str, Any], key: str) -> bytes:
    return aws["s3"].get_object(Bucket=aws["bucket"], Key=key)["Body"].read()


# ===================================================================== modeling-sources
def test_quotes_capture_then_immutable_reuse(aws, monkeypatch):
    calls = []
    monkeypatch.setattr(current_quotes, "collect_quotes", lambda symbols: calls.append(sorted(symbols)) or supplied())
    event = {"kind": "quotes", "slot": AT, "experiment": EXPERIMENT, "symbols": ["SPY"]}
    first = modeling.sources_handler(event)
    assert first["status"] == "captured" and first["slot"] == AT and first["kind"] == "quotes"
    iid = _id("mi", EXPERIMENT, "__valuation_quotes__", AT, "valuation_quotes")
    assert first["input_ids"] == [iid]
    stored = ModelingEvidence.from_ec2().read_input(iid)
    assert stored["payload"]["collection_owner"] == "financialplanning" and stored["payload"]["scheduled_slot"] == AT
    assert keys(aws, "modeling-evidence/v1/inputs/")
    monkeypatch.setattr(current_quotes, "collect_quotes", lambda *_: pytest.fail("retry fetched a replacement quote set"))
    again = modeling.sources_handler({**event, "slot": "2026-01-12T10:00:00-05:00"})  # same instant, other offset
    assert again["status"] == "reused_immutable_source" and again["input_ids"] == [iid]
    assert calls == [["SPY"]]
    assert list(aws["tmp"].iterdir()) == []  # per-invocation work directory removed


def test_quotes_default_to_configured_symbols_and_experiment(aws, monkeypatch):
    seen = []
    monkeypatch.setattr(current_quotes, "collect_quotes", lambda symbols: seen.append(sorted(symbols)) or supplied())
    out = modeling.sources_handler({"kind": "quotes", "slot": AT})
    assert out["experiment"] == EXPERIMENT
    assert seen == [["AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "TSLA", "VOO"]]


def test_research_is_captured_for_the_interim_cohort(aws, monkeypatch):
    payload = {"status": "succeeded", "available_at": AT, "news": [], "universe": None, "coverage": [], "workers": []}
    monkeypatch.setattr(financial_research, "collect", lambda output, **_: payload)
    out = modeling.sources_handler({"kind": "research", "slot": AT})
    assert out["status"] == "captured"
    assert out["input_ids"] == [_id("mi", EXPERIMENT + "-interim", "__research_source__", AT, "research_source")]
    stored = ModelingEvidence.from_ec2().read_input(out["input_ids"][0])["payload"]
    assert stored["evidence_purpose"] == "forward_financial_research" and stored["environment"] == "beta"


def test_research_failure_is_persisted_evidence_not_a_lambda_error(aws, monkeypatch):
    def boom(output, **_):
        raise OSError("controlled network failure")
    monkeypatch.setattr(financial_research, "collect", boom)
    out = modeling.sources_handler({"kind": "research", "slot": AT})
    stored = ModelingEvidence.from_ec2().read_input(out["input_ids"][0])["payload"]
    assert stored["status"] == "failed" and "controlled network failure" in stored["acquisition_failure"]


@pytest.mark.parametrize("event,match", [
    ({"kind": "news", "slot": AT}, "kind"),
    ({"kind": "quotes", "slot": "2026-01-12T15:00:30Z"}, "whole minute"),
    ({"kind": "quotes", "slot": "2026-01-12T15:00:00"}, "timezone"),
    ({"kind": "quotes"}, "ISO 8601"),
    ({"kind": "quotes", "slot": AT, "experiment": "bad experiment!"}, "experiment"),
])
def test_invalid_source_events_fail_before_any_collection(aws, monkeypatch, event, match):
    monkeypatch.setattr(current_quotes, "collect_quotes", lambda *_: pytest.fail("collected for an invalid event"))
    with pytest.raises(modeling.ModelingEventError, match=match):
        modeling.sources_handler(event)


def test_handlers_refuse_other_environments(aws, monkeypatch):
    monkeypatch.setenv("FINPLAN_ENV", "gamma")
    for handler, event in ((modeling.sources_handler, {"kind": "quotes", "slot": AT}), (modeling.report_handler, {"mode": "current"})):
        with pytest.raises(modeling.ModelingEventError, match="beta only"):
            handler(event)


@pytest.mark.parametrize("handler,event", [(modeling.sources_handler, {"action": "preflight"}), (modeling.report_handler, {"mode": "preflight"})])
def test_preflight_reads_only(aws, handler, event):
    out = handler(event)
    assert out["status"] == "ok" and out["experiment"] == EXPERIMENT and out["registry_present"] is False
    assert keys(aws, "") == []
    import boto3
    assert boto3.client("dynamodb", region_name=REGION).scan(TableName="finplan-beta-financialplanning-research-input")["Count"] == 0


# ===================================================================== modeling-report
@pytest.fixture
def report_cli(aws, monkeypatch):
    from scripts import modeling_report as cli
    from tests.unit.reporting.test_modeling_reports import report

    calls: list[dict[str, Any]] = []

    def aggregate(reader, **kwargs):
        calls.append(kwargs)
        data = report()
        data.update(experiment_id=kwargs["experiment_id"], mode="forward", finalized=kwargs["finalized"], start_day=kwargs["start_day"], end_day=kwargs["end_day"])
        if kwargs["start_day"] != kwargs["end_day"]:
            data["period"] = "weekly"
        return data

    monkeypatch.setattr(ModelingEvidence, "read_registry", lambda self, experiment: {"approaches": [{"approach_id": "controlled"}], "comparison_policy": {}})
    monkeypatch.setattr(cli, "aggregate", aggregate)
    monkeypatch.setattr(cli, "consolidate", lambda reports: reports[0])
    return calls


def test_current_report_publishes_immutable_revision_and_views(aws, report_cli):
    out = modeling.report_handler({"mode": "current", "slot": DAY + "T19:45:00Z"})
    assert out["status"] == "ok" and out["day"] == DAY and out["mode"] == "current"
    assert report_cli[0]["start_day"] == report_cli[0]["end_day"] == DAY and report_cli[0]["finalized"] is False
    rev = out["revision"]
    immutable = f"modeling/v1/{EXPERIMENT}/forward/{DAY}/{rev}/"
    assert keys(aws, immutable) == [immutable + n for n in ("manifest.json", "report.csv", "report.html", "report.json")]
    for prefix in (VIEWS + "latest/", VIEWS + f"daily/{DAY}/"):
        assert keys(aws, prefix) == [prefix + n for n in ("head.json", "manifest.json", "report.csv", "report.html", "report.json")]
        assert body(aws, prefix + "report.html") == body(aws, immutable + "report.html")
        head = json.loads(body(aws, prefix + "head.json"))
        assert head["revision"] == rev and head["immutable_prefix"] == immutable and head["finalized"] is False
    assert aws["s3"].head_object(Bucket=aws["bucket"], Key=VIEWS + "latest/report.html")["ContentType"].startswith("text/html")
    assert list(aws["tmp"].iterdir()) == []


def test_eod_finalizes_writes_review_and_is_not_regressed_by_a_later_current(aws, report_cli):
    eod = modeling.report_handler({"mode": "eod", "scheduled_time": DAY + "T21:00:00Z"})
    assert report_cli[-1]["finalized"] is True
    assert json.loads(body(aws, VIEWS + "latest/head.json"))["finalized"] is True
    assert keys(aws, VIEWS + "review/") == [VIEWS + f"review/{DAY}.json", VIEWS + f"review/{DAY}.md"]
    review = json.loads(body(aws, VIEWS + f"review/{DAY}.json"))
    assert review["experiment_id"] == EXPERIMENT and review["day"] == DAY
    assert body(aws, VIEWS + f"review/{DAY}.md").startswith(f"# {EXPERIMENT}".encode())
    assert keys(aws, f"modeling/v1/{EXPERIMENT}/review/{DAY}/") and eod["review"]["immutable_prefix"].startswith(f"modeling/v1/{EXPERIMENT}/review/{DAY}/")
    later = modeling.report_handler({"mode": "current", "day": DAY})
    assert later["views"]["written"] == [] and set(later["views"]["skipped"]) == {"latest", "period"}
    assert json.loads(body(aws, VIEWS + "latest/head.json"))["revision"] == eod["revision"]


def test_weekly_and_closed_week_windows(aws, report_cli):
    friday = "2026-10-16"
    weekly = modeling.report_handler({"mode": "weekly", "scheduled_time": friday + "T21:25:00Z"})
    closed = modeling.report_handler({"mode": "closed_week", "scheduled_time": friday + "T21:25:00Z"})
    assert (report_cli[0]["start_day"], report_cli[0]["end_day"]) == ("2026-10-10", "2026-10-16")
    assert (report_cli[1]["start_day"], report_cli[1]["end_day"], report_cli[1]["finalized"]) == ("2026-10-09", "2026-10-15", True)
    assert keys(aws, VIEWS + "weekly/2026-10-10_2026-10-16/") and keys(aws, VIEWS + "weekly/2026-10-09_2026-10-15/")
    assert json.loads(body(aws, VIEWS + "weekly/latest/head.json"))["revision"] == weekly["revision"]  # newer window wins
    assert closed["views"]["skipped"]["latest"]["kept_revision"] == weekly["revision"]
    assert keys(aws, VIEWS + "latest/") == []  # weekly reports never replace the daily latest


def test_report_evidence_error_fails_the_invocation(aws, monkeypatch):
    monkeypatch.setattr(ModelingEvidence, "read_registry", lambda self, experiment: None)
    with pytest.raises(RuntimeError, match="No persisted approach registry"):
        modeling.report_handler({"mode": "current", "day": DAY})
    assert keys(aws, VIEWS) == []


@pytest.mark.parametrize("event", [{"mode": "monthly"}, {"mode": "current", "day": "12-10-2026"}, {"mode": "eod", "slot": "2026-10-12T21:00:01Z"}])
def test_invalid_report_events(aws, event):
    with pytest.raises(modeling.ModelingEventError):
        modeling.report_handler(event)


def test_mutable_writes_are_limited_to_the_view_prefix(aws):
    store = ModelingEvidence.from_ec2().store
    with pytest.raises(ValueError, match="write-once"):
        store.put_mutable("reports", f"modeling/v1/{EXPERIMENT}/forward/x", b"{}")
    store.put_mutable("reports", VIEWS + "latest/x.json", b"{}")
    store.put_mutable("reports", VIEWS + "latest/x.json", b"{\"a\":1}")
    assert body(aws, VIEWS + "latest/x.json") == b"{\"a\":1}"
