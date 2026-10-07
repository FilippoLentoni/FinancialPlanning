"""Pipeline-structure check over a synthesized pipeline template (design D6; spec
environment-promotion "Standard pipeline stages" and "Immutable artifact
promotion", ENV-09; task 10.1).

Pipeline standard (each repository, CodePipeline V2 + CodeBuild):

1. **Source**: a CodeConnections source action on branch ``main``; the connection
   comes from ``/finplan/shared/<repo>/config/codeconnection-ref`` (never a literal
   ARN in the template).
2. **Build** (and test): unit tests, contract conformance, scans, ``cdk synth``,
   artifact digest, ``release_id``.
3. **Beta**: deploy plus integration-beta tests.
4. **Gamma**: deploy plus gamma tests.
5. **Manual approval**: a Manual approval action (the approver identity,
   timestamp and ``release_id`` are recorded with the prod release manifest).
6. **Prod**: deploy plus smoke tests on the synthetic prod portfolio.

Promotion is artifact-only: actions in every stage after Build consume only
artifacts produced by the Build stage (the cloud assembly plus container digests)
and never the source checkout, and no post-build CodeBuild project runs
``cdk synth``. Deploy actions run under a scoped deploy role (``RoleArn``).
Rollback is a pipeline variable ``rollback_to_release_id`` that redeploys the
stored assembly of a recorded release. A failing stage stops promotion
(CodePipeline semantics), so gamma failures stop before approval.

:func:`check_pipeline_template` returns findings; an empty list means the
template conforms.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

__all__ = ["STAGE_ORDER", "Finding", "classify_stage", "check_pipeline", "check_pipeline_template", "main"]

STAGE_ORDER = ("source", "build", "beta", "gamma", "approval", "prod")
ENV_STAGES = ("beta", "gamma", "prod")
SOURCE_PROVIDERS = {"CodeStarSourceConnection", "CodeConnections"}
ROLLBACK_VARIABLE = "rollback_to_release_id"
_LITERAL_ARN = re.compile(r"^arn:")


@dataclass(frozen=True)
class Finding:
    pipeline: str
    message: str

    def __str__(self) -> str:
        return f"{self.pipeline}: {self.message}"


def _actions(stage: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [a for a in stage.get("Actions") or [] if isinstance(a, Mapping)]


def _category(action: Mapping[str, Any]) -> str:
    return str((action.get("ActionTypeId") or {}).get("Category", ""))


def _provider(action: Mapping[str, Any]) -> str:
    return str((action.get("ActionTypeId") or {}).get("Provider", ""))


def classify_stage(stage: Mapping[str, Any]) -> str:
    """Classify a stage as source, build, beta, gamma, approval, prod or unknown."""
    name = str(stage.get("Name", "")).lower()
    actions = _actions(stage)
    cats = {_category(a) for a in actions}
    if actions and cats == {"Source"}:
        return "source"
    if "Approval" in cats:
        return "approval"
    for env in ENV_STAGES:
        if re.search(rf"(^|[^a-z]){env}([^a-z]|$)", name):
            return env
    if "Build" in cats or "Test" in cats or re.search(r"build|synth", name):
        return "build"
    return "unknown"


def _names(items: Any) -> list[str]:
    return [str(i.get("Name")) for i in items or [] if isinstance(i, Mapping) and i.get("Name")]


def _project_buildspec(project_ref: Any, resources: Mapping[str, Any]) -> str | None:
    if isinstance(project_ref, Mapping) and "Ref" in project_ref:
        res = resources.get(project_ref["Ref"])
        if isinstance(res, Mapping) and res.get("Type") == "AWS::CodeBuild::Project":
            spec = ((res.get("Properties") or {}).get("Source") or {}).get("BuildSpec")
            return spec if isinstance(spec, str) else json.dumps(spec) if spec is not None else None
    return None


def check_pipeline(logical_id: str, pipeline: Mapping[str, Any], resources: Mapping[str, Any] | None = None) -> list[Finding]:
    resources = resources or {}
    props = pipeline.get("Properties") or {}
    f = lambda m: Finding(logical_id, m)  # noqa: E731
    findings: list[Finding] = []
    stages = [s for s in props.get("Stages") or [] if isinstance(s, Mapping)]
    kinds = [classify_stage(s) for s in stages]

    # ---- stage order
    for k in STAGE_ORDER:
        if k not in kinds:
            label = "manual approval" if k == "approval" else k
            findings.append(f(f"missing {label} stage (required order: Source -> Build -> Beta -> Gamma -> ManualApproval -> Prod)"))
    for s, k in zip(stages, kinds, strict=True):
        if k == "unknown":
            findings.append(f(f"stage {s.get('Name')!r} is not part of the pipeline standard"))
    dup = sorted({k for k in kinds if kinds.count(k) > 1 and k != "unknown"})
    for k in dup:
        findings.append(f(f"stage kind {k} appears more than once"))
    present = [k for k in kinds if k in STAGE_ORDER]
    if present != [k for k in STAGE_ORDER if k in present]:
        findings.append(f(f"stages are out of order: {' -> '.join(s.get('Name', '?') for s in stages)}; required Source -> Build -> Beta -> Gamma -> ManualApproval -> Prod"))

    # ---- pipeline type and rollback variable
    if props.get("PipelineType") != "V2":
        findings.append(f("PipelineType must be V2"))
    if ROLLBACK_VARIABLE not in _names(props.get("Variables")):
        findings.append(f(f"pipeline variable {ROLLBACK_VARIABLE} is missing (rollback by release_id)"))

    # ---- source stage
    for s, k in zip(stages, kinds, strict=True):
        if k != "source":
            continue
        for a in _actions(s):
            cfg = a.get("Configuration") or {}
            if _provider(a) not in SOURCE_PROVIDERS:
                findings.append(f(f"source action {a.get('Name')!r} must use a CodeConnections source (got {_provider(a)!r})"))
            if cfg.get("BranchName") != "main":
                findings.append(f(f"source action {a.get('Name')!r} must track branch main"))
            conn = cfg.get("ConnectionArn")
            if isinstance(conn, str) and _LITERAL_ARN.match(conn):
                findings.append(f(f"source action {a.get('Name')!r} has a literal connection ARN; resolve /finplan/shared/<repo>/config/codeconnection-ref instead"))

    # ---- artifact-only promotion
    build_outputs: set[str] = set()
    source_outputs: set[str] = set()
    for s, k in zip(stages, kinds, strict=True):
        for a in _actions(s):
            outs = set(_names(a.get("OutputArtifacts")))
            if k == "build":
                build_outputs |= outs
            elif k == "source":
                source_outputs |= outs
    if "build" in kinds and not build_outputs:
        findings.append(f("build stage produces no output artifact (cloud assembly)"))
    seen_build = False
    for s, k in zip(stages, kinds, strict=True):
        if k == "build":
            seen_build = True
            continue
        if not seen_build or k in ("source", "unknown"):
            continue
        for a in _actions(s):
            ins = set(_names(a.get("InputArtifacts")))
            bad = ins - build_outputs
            if bad:
                what = "the source checkout" if bad & source_outputs else "artifacts not produced by the build stage"
                findings.append(f(f"stage {s.get('Name')!r} action {a.get('Name')!r} consumes {what} ({', '.join(sorted(bad))}); post-build stages consume build artifacts only"))
            if _category(a) in ("Build", "Test") and _provider(a) == "CodeBuild":
                spec = _project_buildspec((a.get("Configuration") or {}).get("ProjectName"), resources)
                if spec and re.search(r"\bcdk\s+synth\b", spec):
                    findings.append(f(f"stage {s.get('Name')!r} action {a.get('Name')!r} runs cdk synth; later stages never rebuild"))

    # ---- environment stages: deploy (scoped role) then tests
    for s, k in zip(stages, kinds, strict=True):
        if k not in ENV_STAGES:
            continue
        acts = _actions(s)
        deploys = [a for a in acts if _category(a) == "Deploy"]
        tests = [a for a in acts if _category(a) in ("Test", "Build", "Invoke")]
        if not deploys:
            findings.append(f(f"stage {s.get('Name')!r} has no deploy action"))
        for a in deploys:
            role = a.get("RoleArn")
            if not role:
                findings.append(f(f"deploy action {a.get('Name')!r} in stage {s.get('Name')!r} does not use a scoped deploy role (RoleArn)"))
            elif isinstance(role, str):
                findings.append(f(f"deploy action {a.get('Name')!r} in stage {s.get('Name')!r} uses a literal role ARN; reference the scoped deploy role declared by the bootstrap stacks"))
        last_deploy = max((int(a.get("RunOrder", 1)) for a in deploys), default=0)
        if not any(int(a.get("RunOrder", 1)) > last_deploy for a in tests):
            label = "smoke tests" if k == "prod" else f"{k} tests"
            findings.append(f(f"stage {s.get('Name')!r} has no {label} after its deploy action"))

    # ---- approval stage
    for s, k in zip(stages, kinds, strict=True):
        if k == "approval":
            for a in _actions(s):
                if _category(a) == "Approval" and _provider(a) != "Manual":
                    findings.append(f(f"approval action {a.get('Name')!r} must be a Manual approval"))
            if any(_category(a) != "Approval" for a in _actions(s)):
                findings.append(f(f"approval stage {s.get('Name')!r} must contain only the manual approval"))
    return findings


def check_pipeline_template(template: Mapping[str, Any]) -> list[Finding]:
    resources = template.get("Resources") or {}
    pipelines = {lid: r for lid, r in resources.items() if isinstance(r, Mapping) and r.get("Type") == "AWS::CodePipeline::Pipeline"}
    if not pipelines:
        return [Finding("<template>", "no AWS::CodePipeline::Pipeline resource found")]
    out: list[Finding] = []
    for lid, p in sorted(pipelines.items()):
        out += check_pipeline(lid, p, resources)
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="finplan-conformance pipeline-check", description="Check synthesized pipeline templates against the pipeline standard (ENV-09).")
    ap.add_argument("templates", nargs="+", help="synthesized CloudFormation template JSON files")
    args = ap.parse_args(argv)
    rc = 0
    for t in args.templates:
        findings = check_pipeline_template(json.loads(Path(t).read_text(encoding="utf-8")))
        for fd in findings:
            print(f"FAIL {t}: {fd}", file=sys.stderr)
        if findings:
            rc = 1
        else:
            print(f"PASS {t}")
    return rc
