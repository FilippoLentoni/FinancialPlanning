"""OPS test fixtures (cost guardrails, pipeline, release, bootstrap, smoke; OPS-owned).

* ``ops_assembly``: the full cloud assembly (beta, gamma, prod, the store and tooling stacks with
  the pipeline), synthesized once per session offline with the pipeline's deployment synthesizer
  (:mod:`scripts.synth`); returns its directory;
* ``tooling_template`` / ``store_template``: the account-level templates as JSON;
* ``tooling_resolver``: :class:`infra.policy_sim.TemplateResolver` over the tooling template;
* ``ssm``: a moto SSM client (inside the ``s3`` fixture's mock).

Everything is offline; account identifiers are placeholders.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

STORE = "PipelineStore"
TOOLING = "Tooling"


@pytest.fixture(scope="session")
def ops_assembly(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from scripts.synth import synth

    return synth(tmp_path_factory.mktemp("ops") / "cdk.out")


def _template(assembly: Path, artifact: str) -> dict[str, Any]:
    return json.loads((assembly / f"{artifact}.template.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def tooling_template(ops_assembly: Path) -> dict[str, Any]:
    return _template(ops_assembly, TOOLING)


@pytest.fixture(scope="session")
def store_template(ops_assembly: Path) -> dict[str, Any]:
    return _template(ops_assembly, STORE)


@pytest.fixture(scope="session")
def tooling_resolver(tooling_template: dict[str, Any]) -> Any:
    from infra.policy_sim import TemplateResolver

    return TemplateResolver({"tooling": tooling_template})


@pytest.fixture
def ssm(s3: Any) -> Any:
    import boto3

    return boto3.client("ssm", region_name="us-east-2")


def resources_of(template: dict[str, Any], rtype: str) -> dict[str, dict[str, Any]]:
    return {lid: r for lid, r in template["Resources"].items() if r["Type"] == rtype}


def tags_of(resource: dict[str, Any]) -> dict[str, Any]:
    props = resource.get("Properties") or {}
    tags = props.get("Tags", props.get("ResourceTags")) or []
    return {t["Key"]: t["Value"] for t in tags} if isinstance(tags, list) else dict(tags)
