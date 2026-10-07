"""Tooling log groups and the pipeline store lifecycle (account-level stacks; OPS-owned).

* The budget-state writer and the four CodeBuild projects log to explicit log groups with 30-day
  retention that are deleted with the stack, and the ownership check attributes every group.
* The pipeline store keeps versioning and DeletionPolicy Retain and expires ``assets/``,
  ``bootstrap/`` and pipeline artifacts after 30 days, noncurrent versions after 7 and incomplete
  multipart uploads after 7, while the release ledger never expires.
"""

from __future__ import annotations

from typing import Any

from finplan_contracts import ownership

from infra.stacks.tooling import ASSET_PREFIX, BOOTSTRAP_PREFIX, PIPELINE_NAME, RELEASES_PREFIX, REPO, shared_name
from tests.unit.ops.conftest import resources_of, tags_of

WRITER_GROUP = f"/aws/lambda/{shared_name('budget-state-writer')}"
PROJECT_NAMES = [shared_name("pipeline-build-project")] + [shared_name("pipeline-build-project", f"{env}-stage") for env in ("beta", "gamma", "prod")]


def _groups(template: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Log group name -> (logical id, resource)."""
    return {r["Properties"]["LogGroupName"]: (lid, r) for lid, r in resources_of(template, "AWS::Logs::LogGroup").items()}


# ------------------------------------------------------------------ log groups
def test_every_tooling_log_group_has_30_day_retention_and_is_destroyed(tooling_template: dict[str, Any]) -> None:
    groups = _groups(tooling_template)
    assert set(groups) == {WRITER_GROUP, *(f"/aws/codebuild/{p}" for p in PROJECT_NAMES)}
    for name, (_, res) in groups.items():
        assert res["Properties"]["RetentionInDays"] == 30, name
        assert res["DeletionPolicy"] == "Delete" and res["UpdateReplacePolicy"] == "Delete", name


def test_budget_state_writer_logs_to_its_explicit_group(tooling_template: dict[str, Any]) -> None:
    lid, group = _groups(tooling_template)[WRITER_GROUP]
    fns = [r for r in resources_of(tooling_template, "AWS::Lambda::Function").values() if r["Properties"].get("FunctionName") == shared_name("budget-state-writer")]
    assert len(fns) == 1
    assert fns[0]["Properties"]["LoggingConfig"] == {"LogGroup": {"Ref": lid}}
    assert tags_of(group)["logical-role"] == "budget-state-writer"


def test_each_codebuild_project_logs_to_its_explicit_group(tooling_template: dict[str, Any]) -> None:
    groups = _groups(tooling_template)
    projects = {r["Properties"]["Name"]: r for r in resources_of(tooling_template, "AWS::CodeBuild::Project").values()}
    assert sorted(projects) == sorted(PROJECT_NAMES)
    for name, project in projects.items():
        lid, group = groups[f"/aws/codebuild/{name}"]
        assert project["Properties"]["LogsConfig"]["CloudWatchLogs"] == {"GroupName": {"Ref": lid}, "Status": "ENABLED"}, name
        tags = tags_of(group)
        assert tags["logical-role"] == "pipeline-build-project" and tags["environment"] == "shared" and tags["owner-repo"] == REPO, name


def test_ownership_check_attributes_every_log_group(tooling_template: dict[str, Any]) -> None:
    report = ownership.check_template(tooling_template, REPO, name="tooling")
    assert report.problems == [], [str(p) for p in report.problems]
    lids = {lid for lid, _ in _groups(tooling_template).values()}
    assert lids <= set(report.matched)
    rows = {report.matched[lid] for lid in lids}
    assert rows == {"project-budget", "pipeline-financialplanning"}


# ------------------------------------------------------------------ pipeline store
def _store(store_template: dict[str, Any]) -> dict[str, Any]:
    buckets = resources_of(store_template, "AWS::S3::Bucket")
    assert len(buckets) == 1
    return next(iter(buckets.values()))


def test_store_is_versioned_and_retained(store_template: dict[str, Any]) -> None:
    bucket = _store(store_template)
    assert bucket["Properties"]["VersioningConfiguration"] == {"Status": "Enabled"}
    assert bucket["DeletionPolicy"] == "Retain" and bucket["UpdateReplacePolicy"] == "Retain"


def test_store_lifecycle_expires_assets_bootstrap_and_artifacts(store_template: dict[str, Any]) -> None:
    rules = {r["Id"]: r for r in _store(store_template)["Properties"]["LifecycleConfiguration"]["Rules"]}
    assert all(r["Status"] == "Enabled" for r in rules.values())
    by_prefix = {r.get("Prefix"): r for r in rules.values() if r.get("Prefix")}
    for prefix in (ASSET_PREFIX, BOOTSTRAP_PREFIX, PIPELINE_NAME[:20] + "/"):
        assert by_prefix[prefix]["ExpirationInDays"] == 30, prefix
    assert by_prefix["cache/"]["ExpirationInDays"] == 14
    assert not any(p.startswith(RELEASES_PREFIX) for p in by_prefix), "the release ledger never expires"
    assert all("ExpirationInDays" not in r for r in rules.values() if not r.get("Prefix")), "no bucket-wide current-version expiry"


def test_store_expires_noncurrent_versions_and_aborts_multipart_uploads(store_template: dict[str, Any]) -> None:
    rules = _store(store_template)["Properties"]["LifecycleConfiguration"]["Rules"]
    noncurrent = [r for r in rules if "NoncurrentVersionExpiration" in r]
    multipart = [r for r in rules if "AbortIncompleteMultipartUpload" in r]
    assert len(noncurrent) == 1 and not noncurrent[0].get("Prefix")
    assert noncurrent[0]["NoncurrentVersionExpiration"] == {"NoncurrentDays": 7}
    assert len(multipart) == 1 and not multipart[0].get("Prefix")
    assert multipart[0]["AbortIncompleteMultipartUpload"] == {"DaysAfterInitiation": 7}
