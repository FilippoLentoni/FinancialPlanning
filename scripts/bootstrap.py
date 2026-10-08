#!/usr/bin/env python3
"""One-time authenticated bootstrap of the FinancialPlanning tooling (tasks 9.1a, 10.5; run = task 10.6).

DO NOT RUN during spec or implementation work. The run is task 10.6: the user approved it in
principle on 2026-10-07; it happens only after this IaC is synthesized, and only after the exact
stacks and a cost estimate have been shown and confirmed by the operator. Runbook:
``docs/bootstrap.md``.

The sequence is the contract package's :func:`finplan_contracts.bootstrap.run_bootstrap` (never
re-implemented here); this entry point supplies the platform specifics:

1. **Assembly** (:func:`bootstrap_assembly`): refuses unless a synthesized cloud assembly exists
   (``cdk.out/manifest.json`` from ``scripts/synth.py``), then copies only the two account-level
   stacks, ``finplan-shared-financialplanning-pipeline-store`` and
   ``finplan-shared-financialplanning-tooling``, into ``cdk.out.bootstrap/``. The bootstrap never
   deploys an environment stack; the pipeline does that afterwards with its scoped deploy roles.
2. **Pre-run plan** (contract ``prerun``): prints the exact stacks with their resource types and a
   monthly cost estimate from the AWS Price List API (the injectable ``pricing`` client; no price is
   written anywhere in this repository).
3. **Caller, region, connection, scoped-role checks** (contract): the STS account must match the
   local untracked configuration (``~/.finplan/bootstrap.json``); a root caller proceeds and the
   scoped/MFA-role recommendation is printed; the region must be the primary region; the existing
   CodeConnection must be ``AVAILABLE``; every deploy action must use a scoped deploy role.
4. **Operator confirmation** of the shown stacks and estimate (interactive: type ``deploy``).
5. **Connection reference** written to ``/finplan/shared/financialplanning/config/codeconnection-ref``.
6. **Deploy** (:class:`ToolingDeployer`): first the account-level budget parameters - the cost
   ceiling ``/finplan/shared/financialplanning/config/cost-ceiling-usd`` (default from
   ``config/shared.json``) and the default allocation
   ``/finplan/shared/financialplanning/config/budget-allocation`` (:func:`ensure_budget_allocation`,
   COST-06: written only when absent, a user-set value is preserved, an allocation above the
   ceiling stops the bootstrap) - then ``npx aws-cdk@2 deploy --all`` of the filtered assembly with
   the human-supplied notification address and the published ``budget-enforced-role-names``
   (each repository's account-level ``shared`` list plus its beta, gamma and prod lists; contracts D16).
7. **Source-stage dry run** (contract): an execution that must fetch ``main`` before the stages
   after Source are enabled; on failure it stops with the extend-the-GitHub-App-installation message.

Every AWS client is injected (:class:`finplan_contracts.bootstrap.Clients` plus the deploy runner),
so the unit suite runs the whole sequence with mocks and no AWS call (PIPE-07).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from finplan_contracts import bootstrap as contract_bootstrap  # noqa: E402
from finplan_contracts import budget as contract_budget  # noqa: E402
from finplan_contracts import ssm as contract_ssm  # noqa: E402
from finplan_contracts.bootstrap import (  # noqa: E402
    BootstrapConfig,
    BootstrapStop,
    Clients,
    Plan,
)

from infra.stacks.tooling import (  # noqa: E402
    REPO,
    STORE_STACK_NAME,
    TOOLING_STACK_NAME,
)

__all__ = [
    "BOOTSTRAP_STACKS",
    "AllocationResult",
    "ToolingDeployer",
    "bootstrap_assembly",
    "deploy_parameters",
    "ensure_budget_allocation",
    "ensure_cost_ceiling",
    "main",
    "published_enforced_role_names",
    "run",
]

BOOTSTRAP_STACKS = (STORE_STACK_NAME, TOOLING_STACK_NAME)
DEFAULT_ASSEMBLY = ROOT / "cdk.out"
BOOTSTRAP_ASSEMBLY = ROOT / "cdk.out.bootstrap"
DRY_RUN_RECORD = Path("~/.finplan/financialplanning-source-dry-run.json")
BOOTSTRAP_WRITER = contract_ssm.Writer(REPO, "bootstrap")


# ===================================================================== assembly
def bootstrap_assembly(assembly: str | os.PathLike[str], out: str | os.PathLike[str]) -> Path:
    """A copy of the cloud assembly holding only the account-level stacks (see module docstring)."""
    src = Path(assembly)
    manifest_path = src / "manifest.json"
    if not manifest_path.is_file():
        raise BootstrapStop("prerun", f"the bootstrap IaC is not synthesized: no cloud assembly at {src} (run: uv run python scripts/synth.py). Nothing was deployed.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    arts = manifest.get("artifacts") or {}
    keep: dict[str, Any] = {}
    for art_id, art in arts.items():
        if art.get("type") == "aws:cloudformation:stack" and (art.get("properties") or {}).get("stackName") in BOOTSTRAP_STACKS:
            keep[art_id] = art
    names = {(a.get("properties") or {}).get("stackName") for a in keep.values()}
    missing = [n for n in BOOTSTRAP_STACKS if n not in names]
    if missing:
        raise BootstrapStop("prerun", f"the synthesized assembly does not contain the tooling stacks {missing}; nothing was deployed")
    for art_id, art in list(keep.items()):
        for dep in art.get("dependencies") or []:
            if dep in arts and arts[dep].get("type") == "cdk:asset-manifest":
                keep[dep] = arts[dep]
    dst = Path(out)
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True)
    files: set[str] = set()
    for art in keep.values():
        props = art.get("properties") or {}
        for key in ("templateFile", "file"):
            if props.get(key):
                files.add(props[key])
        if art.get("additionalMetadataFile"):
            files.add(art["additionalMetadataFile"])
        if art.get("type") == "cdk:asset-manifest":
            doc = json.loads((src / props["file"]).read_text(encoding="utf-8"))
            for asset in (doc.get("files") or {}).values():
                files.add(str((asset.get("source") or {}).get("path")))
            if doc.get("dockerImages"):
                raise BootstrapStop("prerun", "the tooling stacks must not contain container-image assets")
    for rel in sorted(files):
        s = src / rel
        if s.is_dir():
            shutil.copytree(s, dst / rel)
        elif s.is_file():
            shutil.copy2(s, dst / rel)
        else:
            raise BootstrapStop("prerun", f"{rel} is missing from the cloud assembly")
    out_manifest = {**{k: v for k, v in manifest.items() if k != "artifacts"}, "artifacts": {}}
    for art_id, art in keep.items():
        art = json.loads(json.dumps(art))
        art["dependencies"] = [d for d in art.get("dependencies") or [] if d in keep]
        props = art.get("properties") or {}
        if "additionalDependencies" in props:
            props["additionalDependencies"] = [d for d in props["additionalDependencies"] if d in keep]
        out_manifest["artifacts"][art_id] = art
    (dst / "manifest.json").write_text(json.dumps(out_manifest, indent=1) + "\n", encoding="utf-8")
    return dst


# ===================================================================== budget parameters (9.1a)
def _get(ssm: Any, name: str) -> str | None:
    try:
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except Exception as exc:
        if getattr(exc, "response", {}).get("Error", {}).get("Code") == "ParameterNotFound":
            return None
        raise


def _put_new(ssm: Any, name: str, value: str, description: str) -> None:
    decision = contract_ssm.check_write(name, BOOTSTRAP_WRITER)
    if not decision:
        raise BootstrapStop("budget-parameters", "; ".join(decision.reasons))
    ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=False, Description=description)


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def ensure_cost_ceiling(ssm: Any, default_usd: float) -> tuple[float, bool]:
    """(ceiling, written). Writes the default only when the parameter is absent."""
    name = contract_budget.CEILING_PARAMETER
    current = _get(ssm, name)
    if current is not None:
        problems = contract_ssm.validate_value(name, current)
        if problems:
            raise BootstrapStop("budget-parameters", f"{name}: " + "; ".join(problems))
        return float(current), False
    value = _number(default_usd)
    problems = contract_ssm.validate_value(name, value)
    if problems:
        raise BootstrapStop("budget-parameters", f"{name}: " + "; ".join(problems))
    _put_new(ssm, name, value, "Project cost ceiling in USD (written by the bootstrap; edit to raise it)")
    return float(value), True


@dataclass(frozen=True)
class AllocationResult:
    allocation: dict[str, float]
    written: bool


def ensure_budget_allocation(ssm: Any, ceiling_usd: float) -> AllocationResult:
    """COST-06: write the default allocation when absent, preserve a user-set value, reject a sum above the ceiling."""
    name = contract_budget.ALLOCATION_PARAMETER
    current = _get(ssm, name)
    if current is not None:
        try:
            allocation = json.loads(current)
        except json.JSONDecodeError as exc:
            raise BootstrapStop("budget-allocation", f"{name} is not JSON: {exc}") from None
        problems = contract_budget.allocation_problems(allocation, ceiling_usd)
        if problems:
            raise BootstrapStop("budget-allocation", f"{name} is invalid against the ceiling {ceiling_usd:g} USD (pre-flight checks refuse paid work with BUDGET_EXCEEDED until it is fixed): " + "; ".join(problems))
        return AllocationResult(dict(allocation), False)
    allocation = dict(contract_budget.DEFAULT_ALLOCATION)
    problems = contract_budget.allocation_problems(allocation, ceiling_usd)
    if problems:
        raise BootstrapStop("budget-allocation", f"the default allocation does not fit the ceiling {ceiling_usd:g} USD: " + "; ".join(problems))
    value = json.dumps(allocation, sort_keys=True)
    problems = contract_ssm.validate_value(name, value, cost_ceiling_usd=ceiling_usd)
    if problems:
        raise BootstrapStop("budget-allocation", "; ".join(problems))
    _put_new(ssm, name, value, "Budget category allocation in USD (default written by the bootstrap; user-editable)")
    return AllocationResult(allocation, True)


def published_enforced_role_names(ssm: Any) -> list[str]:
    """Role names every repository published for the budget action (contract D4, D16; read-only).

    Per repository: the account-level list ``/finplan/shared/<repo>/config/budget-enforced-role-names``
    (its tooling roles, written by that repository's bootstrap) and the beta, gamma and prod lists
    ``/finplan/<env>/<repo>/config/budget-enforced-role-names`` (written by its pipeline). Missing
    parameters are skipped; an invalid value stops the bootstrap.
    """
    names: list[str] = []
    for path in contract_ssm.budget_enforced_role_name_keys():
        value = _get(ssm, path)
        if value is None:
            continue
        problems = contract_ssm.validate_value(path, value)
        if problems:
            raise BootstrapStop("budget-roles", f"{path}: " + "; ".join(problems))
        parsed = json.loads(value) if value.strip().startswith("[") else [v.strip() for v in value.split(",")]
        names += [n for n in parsed if n]
    return sorted(dict.fromkeys(names))


def deploy_parameters(*, notification_email: str | None, enforced_roles: Sequence[str], dry_run_passed: bool, scope_to_project_tag: bool = False) -> dict[str, dict[str, str]]:
    params = {
        "SourceDryRunPassed": "true" if dry_run_passed else "false",
        "AdditionalEnforcedRoleNames": ",".join(enforced_roles),
        # true once the `project` cost-allocation tag is active; otherwise the budget sees the whole account
        "ScopeBudgetToProjectTag": "true" if scope_to_project_tag else "false",
    }
    if notification_email:
        params["NotificationEmail"] = notification_email
    return {TOOLING_STACK_NAME: params}


# ===================================================================== deploy
class ToolingDeployer:
    """The ``deployer`` callback of ``run_bootstrap``: budget parameters, then the CDK deploy."""

    def __init__(
        self,
        assembly: Path,
        ssm: Any,
        *,
        region: str,
        ceiling_default_usd: float,
        notification_email: str | None,
        dry_run_passed: bool,
        scope_to_project_tag: bool = False,
        runner: Callable[..., Any] = subprocess.run,
        out: Callable[[str], None] = print,
    ) -> None:
        self.assembly = assembly
        self.ssm = ssm
        self.region = region
        self.ceiling_default_usd = ceiling_default_usd
        self.notification_email = notification_email
        self.dry_run_passed = dry_run_passed
        self.scope_to_project_tag = scope_to_project_tag
        self.runner = runner
        self.out = out
        self.commands: list[list[str]] = []

    def command(self, enforced_roles: Sequence[str]) -> list[str]:
        cmd = ["npx", "--yes", "aws-cdk@2", "deploy", "--app", str(self.assembly), "--all", "--require-approval", "never", "--progress", "events"]
        for stack, params in deploy_parameters(notification_email=self.notification_email, enforced_roles=enforced_roles, dry_run_passed=self.dry_run_passed, scope_to_project_tag=self.scope_to_project_tag).items():
            for k, v in params.items():
                cmd += ["--parameters", f"{stack}:{k}={v}"]
        return cmd

    def __call__(self, stack_names: list[str]) -> None:
        if sorted(stack_names) != sorted(BOOTSTRAP_STACKS):
            raise BootstrapStop("deploy", f"the bootstrap deploys only {list(BOOTSTRAP_STACKS)}, got {stack_names}")
        ceiling, wrote_ceiling = ensure_cost_ceiling(self.ssm, self.ceiling_default_usd)
        self.out(f"[OK] cost ceiling: {ceiling:g} USD ({'default written' if wrote_ceiling else 'existing value kept'})")
        alloc = ensure_budget_allocation(self.ssm, ceiling)
        self.out(f"[OK] budget allocation: {json.dumps(alloc.allocation, sort_keys=True)} ({'default written' if alloc.written else 'user value preserved'})")
        roles = published_enforced_role_names(self.ssm)
        self.out(f"[OK] budget action: {len(roles)} published role name(s) plus the tooling roles")
        cmd = self.command(roles)
        self.commands.append(cmd)
        shown = [("NotificationEmail=<redacted>" if "NotificationEmail=" in c else c) for c in cmd]
        self.out("running: " + " ".join(shown))
        env = {**os.environ, "AWS_REGION": self.region, "AWS_DEFAULT_REGION": self.region, "CDK_DISABLE_VERSION_CHECK": "1"}
        proc = self.runner(cmd, cwd=str(ROOT), env=env)
        if int(getattr(proc, "returncode", 1)) != 0:
            raise BootstrapStop("deploy", "cdk deploy of the tooling stacks failed (CloudFormation rolls back automatically)")


# ===================================================================== orchestration
def _interactive_approve(plan: Plan) -> bool:  # pragma: no cover - interactive
    print("\nThe stacks and cost estimate above will be deployed under the user's in-principle approval of 2026-10-07.")
    return input("Type 'deploy' to deploy exactly these stacks, anything else to stop: ").strip() == "deploy"


def run(
    config: BootstrapConfig,
    clients: Clients,
    *,
    session_region: str | None,
    assembly: Path = DEFAULT_ASSEMBLY,
    bootstrap_dir: Path = BOOTSTRAP_ASSEMBLY,
    approve: Callable[[Plan], bool] = _interactive_approve,
    notification_email: str | None = None,
    scope_budget_to_project_tag: bool = False,
    ceiling_default_usd: float | None = None,
    runner: Callable[..., Any] = subprocess.run,
    record_path: Path | None = None,
    sleep: Callable[[float], None] | None = None,
    usage_rules: Mapping[str, Any] | None = None,
    out: Callable[[str], None] = print,
) -> contract_bootstrap.Report:
    """The full bootstrap (module docstring). Raises :class:`BootstrapStop` at the first failing step."""
    from finplan_platform.core.config import load_shared_config

    filtered = bootstrap_assembly(assembly, bootstrap_dir)
    shared = load_shared_config()
    record = Path(record_path).expanduser() if record_path else None
    passed = False
    if record and record.is_file():
        passed = json.loads(record.read_text(encoding="utf-8")).get("deploy_stages_enabled") is True
    deployer = ToolingDeployer(
        filtered,
        clients.ssm,
        region=config.primary_region,
        ceiling_default_usd=float(ceiling_default_usd if ceiling_default_usd is not None else shared["cost_ceiling_usd_default"]),
        notification_email=notification_email,
        dry_run_passed=passed,
        scope_to_project_tag=scope_budget_to_project_tag,
        runner=runner,
        out=out,
    )
    kwargs: dict[str, Any] = {}
    if sleep is not None:
        kwargs["sleep"] = sleep
    return contract_bootstrap.run_bootstrap(
        config,
        clients,
        session_region=session_region,
        assembly_dir=filtered,
        approve=approve,
        deployer=deployer,
        out=out,
        record_path=record,
        usage_rules=usage_rules,
        **kwargs,
    )


def _local_extra(config_path: str | None) -> dict[str, Any]:
    """Extra keys of the local untracked configuration (``budget_notification_email``,
    ``scope_budget_to_project_tag``)."""
    env = os.environ
    candidate = Path(config_path) if config_path else Path(env[contract_bootstrap.CONFIG_ENV]) if env.get(contract_bootstrap.CONFIG_ENV) else contract_bootstrap.DEFAULT_CONFIG_PATH
    candidate = candidate.expanduser()
    return json.loads(candidate.read_text(encoding="utf-8")) if candidate.is_file() else {}


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - the authenticated run is task 10.6
    ap = argparse.ArgumentParser(description="One-time authenticated bootstrap of the FinancialPlanning tooling stacks (task 10.6). Read docs/bootstrap.md first.")
    ap.add_argument("--config", help="local untracked configuration (default ~/.finplan/bootstrap.json)")
    ap.add_argument("--assembly", type=Path, default=DEFAULT_ASSEMBLY, help="synthesized cloud assembly (scripts/synth.py)")
    ap.add_argument("--record", type=Path, default=DRY_RUN_RECORD, help="where the source-stage dry-run result is recorded (outside the repository)")
    args = ap.parse_args(argv)
    try:
        config = contract_bootstrap.load_config(args.config, repo_root=ROOT)
    except (ValueError, OSError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    import boto3

    session = boto3.session.Session(region_name=config.primary_region)
    clients = Clients(
        sts=session.client("sts"),
        codeconnections=session.client("codeconnections"),
        ssm=session.client("ssm"),
        codepipeline=session.client("codepipeline"),
        pricing=session.client("pricing", region_name="us-east-1"),
    )
    args.record.expanduser().parent.mkdir(parents=True, exist_ok=True)
    try:
        extra = _local_extra(args.config)
        run(
            config,
            clients,
            session_region=session.region_name,
            assembly=args.assembly,
            notification_email=extra.get("budget_notification_email"),
            scope_budget_to_project_tag=extra.get("scope_budget_to_project_tag") is True,
            record_path=args.record,
        )
    except BootstrapStop as stop:
        print(f"[STOPPED] {stop.step}: {stop.message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
