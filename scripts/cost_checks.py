#!/usr/bin/env python3
"""Build-stage cost checks over synthesized CloudFormation templates (task 9.3; COST-03, COST-04).

Spec platform-cost-guardrails:

* **Cost-allocation tagging** (COST-03): every resource that supports tags carries the contract
  cost tag keys ``project``, ``owner-repo``, ``environment`` and ``logical-role``
  (:data:`finplan_contracts.ssm.COST_ALLOCATION_TAG_KEYS`; ``run-id`` applies to SageMaker jobs
  only). A taggable resource without them fails the build, and the finding names the resource.
  A type is taggable when its CDK L1 class carries a tag manager, when the template gives it a
  ``Tags``/``ResourceTags`` property, or when it is in :data:`TAGGABLE_FALLBACK`.
* **No always-on compute in phase 1** (COST-04): the build fails on instances, NAT gateways,
  interface VPC endpoints, load balancers, provisioned-capacity databases and tables, always-on
  containers, model endpoints, provisioned Lambda concurrency, API caches and any GPU instance
  type (:data:`ALWAYS_ON_TYPES` plus the conditional rules in :func:`always_on_findings`).

Usage: ``python scripts/cost_checks.py cdk.out [more templates or directories]``; exit 1 on any
finding. Runs offline.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from finplan_contracts.ssm import COST_ALLOCATION_TAG_KEYS

__all__ = ["ALWAYS_ON_TYPES", "REQUIRED_TAG_KEYS", "Finding", "always_on_findings", "check_template", "is_taggable", "iter_templates", "main", "tag_findings"]

#: ``run-id`` is only for SageMaker jobs (contract D10).
REQUIRED_TAG_KEYS: tuple[str, ...] = tuple(k for k in COST_ALLOCATION_TAG_KEYS if k != "run-id")

#: Taggable types checked even when the CDK class lookup is unavailable.
TAGGABLE_FALLBACK = frozenset(
    {
        "AWS::S3::Bucket",
        "AWS::DynamoDB::Table",
        "AWS::Lambda::Function",
        "AWS::IAM::Role",
        "AWS::KMS::Key",
        "AWS::SQS::Queue",
        "AWS::SNS::Topic",
        "AWS::ApiGateway::RestApi",
        "AWS::ApiGateway::Stage",
        "AWS::CodeBuild::Project",
        "AWS::CodePipeline::Pipeline",
        "AWS::SSM::Parameter",
        "AWS::CloudWatch::Alarm",
        "AWS::Logs::LogGroup",
        "AWS::Events::Rule",
        "AWS::ECR::Repository",
        "AWS::CodeArtifact::Domain",
        "AWS::CodeArtifact::Repository",
        "AWS::SecretsManager::Secret",
        "AWS::StepFunctions::StateMachine",
        "AWS::EC2::Instance",
        "AWS::EC2::NatGateway",
        "AWS::SageMaker::Endpoint",
        "AWS::RDS::DBInstance",
        "AWS::RDS::DBCluster",
        "AWS::Budgets::Budget",
        "AWS::Budgets::BudgetsAction",
    }
)

#: Always-on or provisioned resources (COST-04): type -> what it is.
ALWAYS_ON_TYPES: dict[str, str] = {
    "AWS::EC2::Instance": "EC2 instance",
    "AWS::EC2::NatGateway": "NAT gateway",
    "AWS::EC2::EIP": "Elastic IP (charged while allocated)",
    "AWS::AutoScaling::AutoScalingGroup": "Auto Scaling group (instances)",
    "AWS::ECS::Service": "always-on container service",
    "AWS::EKS::Cluster": "EKS cluster",
    "AWS::EKS::Nodegroup": "EKS node group",
    "AWS::AppRunner::Service": "always-on container service",
    "AWS::Lightsail::Instance": "instance",
    "AWS::EMR::Cluster": "EMR cluster",
    "AWS::RDS::DBInstance": "provisioned-capacity database",
    "AWS::RDS::DBCluster": "provisioned-capacity database",
    "AWS::DocDB::DBCluster": "provisioned-capacity database",
    "AWS::DocDB::DBInstance": "provisioned-capacity database",
    "AWS::Neptune::DBCluster": "provisioned-capacity database",
    "AWS::Neptune::DBInstance": "provisioned-capacity database",
    "AWS::Redshift::Cluster": "provisioned-capacity warehouse",
    "AWS::ElastiCache::CacheCluster": "provisioned cache",
    "AWS::ElastiCache::ReplicationGroup": "provisioned cache",
    "AWS::ElastiCache::ServerlessCache": "cache with a minimum charge",
    "AWS::OpenSearchService::Domain": "search domain (instances)",
    "AWS::Elasticsearch::Domain": "search domain (instances)",
    "AWS::MSK::Cluster": "Kafka cluster",
    "AWS::ElasticLoadBalancingV2::LoadBalancer": "load balancer",
    "AWS::ElasticLoadBalancing::LoadBalancer": "load balancer",
    "AWS::SageMaker::Endpoint": "model endpoint",
    "AWS::SageMaker::EndpointConfig": "model endpoint configuration",
    "AWS::SageMaker::InferenceComponent": "model endpoint component",
    "AWS::SageMaker::NotebookInstance": "notebook instance",
    "AWS::Bedrock::ProvisionedModelThroughput": "provisioned model throughput",
}
_GPU = re.compile(r"(?i)^(ml\.)?(p[2-6][a-z0-9-]*|g[3-6][a-z0-9-]*|gr6[a-z0-9-]*|inf[12][a-z0-9-]*|trn[12][a-z0-9-]*|dl[12][a-z0-9-]*)\.[0-9a-z]+$")


@dataclass(frozen=True)
class Finding:
    rule: str  # COST-03 | COST-04
    template: str
    logical_id: str
    resource_type: str
    message: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.template}: {self.logical_id} ({self.resource_type}): {self.message}"


# ===================================================================== tagging (COST-03)
@cache
def _cdk_taggable(rtype: str) -> bool | None:
    """True/False from the CDK L1 class (it has a tag manager), None when unknown."""
    parts = rtype.split("::")
    if len(parts) != 3 or parts[0] != "AWS":
        return None
    try:
        mod = importlib.import_module(f"aws_cdk.aws_{parts[1].lower()}")
    except ImportError:
        return None
    cls = getattr(mod, f"Cfn{parts[2]}", None)
    if cls is None:
        return None
    return hasattr(cls, "tags")


def _tags(props: Mapping[str, Any]) -> dict[str, Any] | None:
    for key in ("Tags", "ResourceTags"):
        tags = props.get(key)
        if isinstance(tags, list):
            return {t.get("Key"): t.get("Value") for t in tags if isinstance(t, Mapping)}
        if isinstance(tags, Mapping):
            return dict(tags)
    return None


def is_taggable(resource: Mapping[str, Any]) -> bool:
    rtype = str(resource.get("Type", ""))
    if _tags(resource.get("Properties") or {}) is not None:
        return True
    known = _cdk_taggable(rtype)
    return bool(known) if known is not None else rtype in TAGGABLE_FALLBACK


def tag_findings(template: Mapping[str, Any], name: str = "<template>") -> list[Finding]:
    out: list[Finding] = []
    for lid, res in sorted((template.get("Resources") or {}).items()):
        if not isinstance(res, Mapping) or not is_taggable(res):
            continue
        tags = _tags(res.get("Properties") or {}) or {}
        missing = [k for k in REQUIRED_TAG_KEYS if not tags.get(k)]
        if missing:
            out.append(Finding("COST-03", name, lid, str(res.get("Type")), f"taggable resource is missing the cost-allocation tag(s) {', '.join(missing)}"))
    return out


# ===================================================================== always-on (COST-04)
def _walk(node: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(node, Mapping):
        for k, v in node.items():
            yield f"{path}/{k}", v
            yield from _walk(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}/{i}")


def always_on_findings(template: Mapping[str, Any], name: str = "<template>") -> list[Finding]:
    out: list[Finding] = []
    for lid, res in sorted((template.get("Resources") or {}).items()):
        if not isinstance(res, Mapping):
            continue
        rtype = str(res.get("Type", ""))
        props = res.get("Properties") or {}
        f = lambda msg, rtype=rtype, lid=lid: out.append(Finding("COST-04", name, lid, rtype, msg))  # noqa: E731
        if rtype in ALWAYS_ON_TYPES:
            f(f"{ALWAYS_ON_TYPES[rtype]} is not allowed in phase 1 (serverless and on-demand only)")
        if rtype == "AWS::EC2::VPCEndpoint" and str(props.get("VpcEndpointType", "Gateway")) != "Gateway":
            f("interface VPC endpoint (hourly charge) is not allowed in phase 1")
        if rtype == "AWS::DynamoDB::Table" and (props.get("BillingMode") != "PAY_PER_REQUEST" or props.get("ProvisionedThroughput")):
            f("provisioned-capacity table: BillingMode must be PAY_PER_REQUEST")
        if rtype == "AWS::DynamoDB::GlobalTable" and props.get("BillingMode") != "PAY_PER_REQUEST":
            f("provisioned-capacity global table: BillingMode must be PAY_PER_REQUEST")
        if rtype == "AWS::Kinesis::Stream" and (props.get("StreamModeDetails") or {}).get("StreamMode") != "ON_DEMAND":
            f("provisioned Kinesis stream (shard-hours) is not allowed in phase 1")
        if rtype in ("AWS::Lambda::Alias", "AWS::Lambda::Version") and props.get("ProvisionedConcurrencyConfig"):
            f("provisioned Lambda concurrency (always-on) is not allowed in phase 1")
        if rtype == "AWS::ApiGateway::Stage" and props.get("CacheClusterEnabled") in (True, "true"):
            f("API Gateway cache cluster (hourly charge) is not allowed in phase 1")
        for ptr, value in _walk(props):
            key = ptr.rsplit("/", 1)[-1]
            if isinstance(value, str) and "instancetype" in key.lower().replace("_", "") and _GPU.match(value):
                f(f"GPU instance type {value} at {ptr} (no GPU resources in phase 1)")
    return out


def check_template(template: Mapping[str, Any], name: str = "<template>") -> list[Finding]:
    return tag_findings(template, name) + always_on_findings(template, name)


# ===================================================================== files
def iter_templates(paths: Iterable[str | Path]) -> Iterator[Path]:
    for p in map(Path, paths):
        if p.is_dir():
            yield from sorted(p.rglob("*.template.json"))
        elif p.is_file():
            yield p


def check_paths(paths: Iterable[str | Path]) -> tuple[int, list[Finding]]:
    findings: list[Finding] = []
    n = 0
    for t in iter_templates(paths):
        n += 1
        findings += check_template(json.loads(t.read_text(encoding="utf-8")), str(t))
    return n, findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build-stage cost checks: cost-allocation tags (COST-03) and no always-on compute (COST-04).")
    ap.add_argument("paths", nargs="+", help="cloud assembly directories or template files")
    args = ap.parse_args(argv)
    n, findings = check_paths(args.paths)
    for fd in findings:
        print(f"FAIL {fd}", file=sys.stderr)
    if n == 0:
        print("FAIL no templates found", file=sys.stderr)
        return 1
    if findings:
        return 1
    print(f"ok: {n} template(s): every taggable resource carries {', '.join(REQUIRED_TAG_KEYS)}; no always-on, provisioned or GPU resources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
