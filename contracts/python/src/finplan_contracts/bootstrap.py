"""One-time pipeline bootstrap: pre-run plan, pre-checks and the source-stage dry run
(design D6, D11, D12; spec environment-promotion "One-time authenticated pipeline
bootstrap" ENV-12 and "Verified source connection" ENV-13; tasks 10.2 and 14.4).

The bootstrap runs with the operator's existing authenticated AWS CLI session. Every
AWS call goes through an injected boto3-style client, so tests use mocks and never
touch AWS; ``boto3`` is only imported by the CLI (optional ``aws`` extra).

Order of :func:`run_bootstrap` (it stops at the first failing step):

1. **Pre-run plan** (:func:`prerun`, task 14.4): refuses unless a synthesized cloud
   assembly (``cdk.out/manifest.json``) exists, then prints the exact stacks to
   deploy and a cost estimate computed from prices returned by the injected
   pricing client (no price is hard-coded; types without a pricing rule are listed
   as not estimated).
2. **Caller check**: the STS account must equal the account in the local untracked
   configuration, else the bootstrap stops before creating anything. A root caller
   is not refused: the bootstrap proceeds and prints the scoped/MFA-role
   recommendation.
3. **Region check**: the session region must equal the configured primary region.
4. **Connection check**: the configured existing CodeConnection must be ``AVAILABLE``.
5. **Scoped deploy roles**: every deploy action of the synthesized pipeline must run
   under a scoped deploy role declared by the bootstrap stacks, never the caller.
6. **Approval**: the operator confirms the shown stacks and estimate (the user's
   in-principle approval of 2026-10-07 still requires this step).
7. **Connection reference**: writes ``/finplan/shared/<repo>/config/codeconnection-ref``.
8. **Deploy** the bootstrap stacks (injected deployer; scoped roles + pipeline).
9. **Source-stage dry run**: with the inbound transition of the stage after Source
   disabled, start an execution; if the Source stage fetches ``main`` the transition
   is enabled (remaining stages enabled) and the result recorded; otherwise the
   bootstrap stops and asks the user to extend the GitHub App installation to the
   repository and rerun the dry run.

Local configuration (never committed): ``--config PATH``, else
``$FINPLAN_BOOTSTRAP_CONFIG``, else ``~/.finplan/bootstrap.json``; environment
variables ``FINPLAN_ACCOUNT_ID``, ``FINPLAN_PRIMARY_REGION`` and
``FINPLAN_CODECONNECTION_ARN`` override file values.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import ssm as _ssm

__all__ = [
    "BootstrapConfig",
    "BootstrapStop",
    "Clients",
    "Plan",
    "PricingRule",
    "DEFAULT_PRICING_RULES",
    "ROOT_RECOMMENDATION",
    "EXTEND_INSTALLATION_MESSAGE",
    "load_config",
    "prerun",
    "check_caller",
    "check_region",
    "check_connection",
    "check_deploy_roles",
    "write_connection_ref",
    "source_dry_run",
    "run_bootstrap",
    "main",
]

DEFAULT_CONFIG_PATH = Path("~/.finplan/bootstrap.json")
CONFIG_ENV = "FINPLAN_BOOTSTRAP_CONFIG"
ROOT_RECOMMENDATION = (
    "RECOMMENDATION: the bootstrap is running as the account root principal. This is accepted for the one-time "
    "bootstrap (design D11). After bootstrap, move the human operator to a scoped, MFA-protected role; "
    "deployments use only the scoped roles the bootstrap creates."
)
EXTEND_INSTALLATION_MESSAGE = (
    "The source-stage dry run could not fetch {repository} (branch main) through the configured CodeConnection. "
    "Extend the GitHub App installation of that connection to the repository {repository}, then rerun the dry run. "
    "Deploy stages stay disabled."
)


class BootstrapStop(RuntimeError):
    """The bootstrap stopped at ``step``; nothing after that step ran."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"[{step}] {message}")
        self.step = step
        self.message = message


# ===================================================================== configuration
@dataclass(frozen=True)
class BootstrapConfig:
    account_id: str
    primary_region: str
    repo: str
    codeconnection_arn: str
    github_repository: str  # owner/name
    pipeline_name: str
    source_stage: str = "Source"
    next_stage: str = "Build"

    @classmethod
    def from_mapping(cls, d: Mapping[str, Any]) -> "BootstrapConfig":
        repo = str(d.get("repo", "financialplanning")).lower()
        if repo not in _ssm.REPOS:
            raise ValueError(f"unknown repo {repo!r}")
        missing = [k for k in ("account_id", "primary_region", "codeconnection_arn") if not d.get(k)]
        if missing:
            raise ValueError(f"bootstrap configuration is missing {', '.join(missing)}")
        return cls(
            account_id=str(d["account_id"]),
            primary_region=str(d["primary_region"]),
            repo=repo,
            codeconnection_arn=str(d["codeconnection_arn"]),
            github_repository=str(d.get("github_repository", repo)),
            pipeline_name=str(d.get("pipeline_name", f"finplan-shared-{repo}-pipeline")),
            source_stage=str(d.get("source_stage", "Source")),
            next_stage=str(d.get("next_stage", "Build")),
        )


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def load_config(path: str | os.PathLike[str] | None = None, environ: Mapping[str, str] | None = None, repo_root: Path | None = None) -> BootstrapConfig:
    """Load the local untracked bootstrap configuration (see module docstring)."""
    environ = os.environ if environ is None else environ
    candidate = Path(path) if path else Path(environ[CONFIG_ENV]) if environ.get(CONFIG_ENV) else DEFAULT_CONFIG_PATH
    candidate = candidate.expanduser()
    data: dict[str, Any] = {}
    if candidate.is_file():
        if repo_root is None:
            from .schemas import contracts_root

            try:
                repo_root = contracts_root().parent
            except FileNotFoundError:
                repo_root = None
        if repo_root is not None and _inside(candidate, repo_root):
            raise ValueError(f"bootstrap configuration {candidate} is inside the repository; keep it local and untracked (for example ~/.finplan/bootstrap.json)")
        data = json.loads(candidate.read_text(encoding="utf-8"))
    for env_key, key in (("FINPLAN_ACCOUNT_ID", "account_id"), ("FINPLAN_PRIMARY_REGION", "primary_region"), ("FINPLAN_CODECONNECTION_ARN", "codeconnection_arn"), ("FINPLAN_REPO", "repo")):
        if environ.get(env_key):
            data[key] = environ[env_key]
    return BootstrapConfig.from_mapping(data)


# ===================================================================== reporting
@dataclass
class Step:
    name: str
    status: str  # ok | stopped | info
    message: str


@dataclass
class Report:
    steps: list[Step] = field(default_factory=list)
    plan: "Plan | None" = None
    caller_is_root: bool = False
    dry_run: dict[str, Any] | None = None
    completed: bool = False

    def add(self, name: str, status: str, message: str, out: Callable[[str], None]) -> None:
        self.steps.append(Step(name, status, message))
        out(f"[{status.upper()}] {name}: {message}")


# ===================================================================== pre-run plan (14.4)
@dataclass(frozen=True)
class PricingRule:
    """How to price one resource type: a pricing-API product query and a usage quantity.

    Only quantities (usage assumptions) live here; unit prices always come from the
    pricing client at run time.
    """

    service_code: str
    filters: Mapping[str, str]
    monthly_quantity: float
    unit: str
    note: str = ""


#: Usage assumptions per resource type (per month). Override with ``usage_rules``.
DEFAULT_PRICING_RULES: dict[str, PricingRule] = {
    "AWS::CodePipeline::Pipeline": PricingRule("AWSCodePipeline", {"productFamily": "DevOps"}, 300, "minute", "V2 action execution minutes"),
    "AWS::CodeBuild::Project": PricingRule("CodeBuild", {"productFamily": "Compute"}, 300, "minute", "build minutes"),
    "AWS::S3::Bucket": PricingRule("AmazonS3", {"productFamily": "Storage", "volumeType": "Standard"}, 5, "GB-Mo", "standard storage"),
    "AWS::KMS::Key": PricingRule("awskms", {"productFamily": "Encryption Key"}, 1, "Keys", "customer managed key"),
    "AWS::Lambda::Function": PricingRule("AWSLambda", {"group": "AWS-Lambda-Requests"}, 100000, "Requests", "invocations"),
    "AWS::SNS::Topic": PricingRule("AmazonSNS", {"productFamily": "Message Delivery"}, 1000, "Requests", "notifications"),
    "AWS::CodeArtifact::Repository": PricingRule("AWSCodeArtifact", {"productFamily": "Storage"}, 1, "GB-Mo", "package storage"),
    "AWS::Budgets::BudgetsAction": PricingRule("AWSBudgets", {"productFamily": "Budgets"}, 30, "Budget-Day", "action-enabled budget days"),
}
#: Types AWS does not bill by themselves (IAM, CloudFormation bookkeeping); listed, not priced.
NOT_BILLED_TYPES = {"AWS::IAM::Role", "AWS::IAM::Policy", "AWS::IAM::ManagedPolicy", "AWS::IAM::InstanceProfile", "AWS::CDK::Metadata", "AWS::CloudFormation::WaitConditionHandle"}


@dataclass
class StackPlan:
    name: str
    region: str | None
    template_file: str
    resource_types: dict[str, int]
    template: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass
class EstimateLine:
    resource_type: str
    count: int
    unit_price_usd: float
    quantity: float
    unit: str
    monthly_usd: float


@dataclass
class Plan:
    assembly_dir: Path
    stacks: list[StackPlan]
    estimate_lines: list[EstimateLine]
    not_estimated: list[str]
    not_billed: list[str]
    monthly_estimate_usd: float
    price_retrieved_at: str

    @property
    def stack_names(self) -> list[str]:
        return [s.name for s in self.stacks]

    def render(self) -> str:
        lines = ["Stacks to deploy (exact):"]
        for s in self.stacks:
            types = ", ".join(f"{t} x{n}" for t, n in sorted(s.resource_types.items()))
            lines.append(f"  - {s.name} (region {s.region or 'from configuration'}): {types}")
        lines.append(f"Cost estimate (monthly, USD; prices retrieved {self.price_retrieved_at} from the AWS Price List API):")
        for e in self.estimate_lines:
            lines.append(f"  - {e.resource_type} x{e.count}: {e.quantity:g} {e.unit} x {e.unit_price_usd:g} USD = {e.monthly_usd:.4f} USD")
        lines.append(f"  total estimate: {self.monthly_estimate_usd:.4f} USD per month")
        if self.not_estimated:
            lines.append("  not estimated (no pricing rule or no price returned): " + ", ".join(self.not_estimated))
        if self.not_billed:
            lines.append("  not billed by themselves: " + ", ".join(self.not_billed))
        return "\n".join(lines)


def _region_of(env: Any) -> str | None:
    if isinstance(env, str) and env.startswith("aws://"):
        region = env.rsplit("/", 1)[-1]
        return None if region.startswith("unknown") else region
    return None


def load_assembly(assembly_dir: str | os.PathLike[str]) -> list[StackPlan]:
    """Stacks of a synthesized CDK cloud assembly; refuses when it does not exist."""
    d = Path(assembly_dir)
    manifest = d / "manifest.json"
    if not d.is_dir() or not manifest.is_file():
        raise BootstrapStop("prerun", f"the bootstrap IaC is not synthesized: no cloud assembly at {d} (manifest.json missing); run the bootstrap synth first. Nothing was deployed.")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    stacks = []
    for art_id, art in sorted((data.get("artifacts") or {}).items()):
        if art.get("type") != "aws:cloudformation:stack":
            continue
        props = art.get("properties") or {}
        tfile = props.get("templateFile")
        if not tfile or not (d / tfile).is_file():
            raise BootstrapStop("prerun", f"stack {art_id}: template {tfile!r} is missing from the cloud assembly")
        template = json.loads((d / tfile).read_text(encoding="utf-8"))
        counts: dict[str, int] = {}
        for res in (template.get("Resources") or {}).values():
            t = res.get("Type", "?") if isinstance(res, dict) else "?"
            counts[t] = counts.get(t, 0) + 1
        stacks.append(StackPlan(props.get("stackName") or art_id, _region_of(art.get("environment")), tfile, counts, template))
    if not stacks:
        raise BootstrapStop("prerun", f"the cloud assembly at {d} contains no CloudFormation stacks")
    return stacks


def _unit_price(price_list: Sequence[Any], unit: str) -> float | None:
    """Lowest positive on-demand USD unit price whose unit matches ``unit`` (case-insensitive)."""
    best: float | None = None
    for item in price_list:
        prod = json.loads(item) if isinstance(item, str) else item
        for term in ((prod.get("terms") or {}).get("OnDemand") or {}).values():
            for dim in (term.get("priceDimensions") or {}).values():
                if str(dim.get("unit", "")).lower() != unit.lower():
                    continue
                try:
                    usd = float((dim.get("pricePerUnit") or {}).get("USD"))
                except (TypeError, ValueError):
                    continue
                if usd > 0 and (best is None or usd < best):
                    best = usd
    return best


def prerun(
    assembly_dir: str | os.PathLike[str],
    pricing_client: Any,
    *,
    region: str,
    usage_rules: Mapping[str, PricingRule] | None = None,
    now: Callable[[], datetime] | None = None,
    out: Callable[[str], None] = print,
) -> Plan:
    """Print the exact stacks and a cost estimate; refuse without a synthesized assembly (14.4)."""
    stacks = load_assembly(assembly_dir)
    rules = dict(DEFAULT_PRICING_RULES if usage_rules is None else usage_rules)
    totals: dict[str, int] = {}
    for s in stacks:
        for t, n in s.resource_types.items():
            totals[t] = totals.get(t, 0) + n
    lines: list[EstimateLine] = []
    not_estimated: list[str] = []
    not_billed: list[str] = []
    cache: dict[tuple[str, tuple[tuple[str, str], ...]], float | None] = {}
    for rtype, count in sorted(totals.items()):
        if rtype in NOT_BILLED_TYPES:
            not_billed.append(rtype)
            continue
        rule = rules.get(rtype)
        if rule is None:
            not_estimated.append(rtype)
            continue
        filters = tuple(sorted({**dict(rule.filters), "regionCode": region}.items()))
        key = (rule.service_code, filters)
        if key not in cache:
            resp = pricing_client.get_products(
                ServiceCode=rule.service_code,
                Filters=[{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in filters],
                FormatVersion="aws_v1",
                MaxResults=100,
            )
            cache[key] = _unit_price(resp.get("PriceList") or [], rule.unit)
        price = cache[key]
        if price is None:
            not_estimated.append(rtype)
            continue
        monthly = price * rule.monthly_quantity * count
        lines.append(EstimateLine(rtype, count, price, rule.monthly_quantity, rule.unit, monthly))
    retrieved = (now or (lambda: datetime.now(timezone.utc)))().strftime("%Y-%m-%dT%H:%M:%SZ")
    plan = Plan(Path(assembly_dir), stacks, lines, not_estimated, not_billed, sum(e.monthly_usd for e in lines), retrieved)
    out(plan.render())
    return plan


# ===================================================================== checks
def check_caller(sts_client: Any, config: BootstrapConfig) -> dict[str, Any]:
    ident = sts_client.get_caller_identity()
    account = str(ident.get("Account", ""))
    if account != config.account_id:
        raise BootstrapStop("caller", "the STS account does not match the account in the local bootstrap configuration; stopping before creating anything")
    return dict(ident)


def is_root(identity: Mapping[str, Any]) -> bool:
    return str(identity.get("Arn", "")).endswith(":root")


def check_region(session_region: str | None, config: BootstrapConfig) -> None:
    if session_region != config.primary_region:
        raise BootstrapStop("region", f"session region {session_region!r} is not the configured primary region {config.primary_region!r}")


def check_connection(connections_client: Any, config: BootstrapConfig) -> str:
    resp = connections_client.get_connection(ConnectionArn=config.codeconnection_arn)
    status = (resp.get("Connection") or {}).get("ConnectionStatus")
    if status != "AVAILABLE":
        raise BootstrapStop("connection", f"the configured CodeConnection is {status or 'unknown'}, not AVAILABLE")
    return status


def _resolve_role(value: Any, resources: Mapping[str, Any]) -> tuple[str | None, Mapping[str, Any] | None]:
    if isinstance(value, Mapping):
        if "Fn::GetAtt" in value:
            ga = value["Fn::GetAtt"]
            lid = ga[0] if isinstance(ga, list) else str(ga).split(".")[0]
            return lid, resources.get(lid)
        if "Ref" in value:
            return value["Ref"], resources.get(value["Ref"])
    return None, None


def check_deploy_roles(template: Mapping[str, Any], caller_arn: str | None = None) -> list[str]:
    """Deploy actions run under scoped deploy roles declared in the bootstrap stacks (ENV-12)."""
    resources = template.get("Resources") or {}
    problems = []
    for pid, res in sorted(resources.items()):
        if not isinstance(res, Mapping) or res.get("Type") != "AWS::CodePipeline::Pipeline":
            continue
        for stage in (res.get("Properties") or {}).get("Stages") or []:
            for a in stage.get("Actions") or []:
                if (a.get("ActionTypeId") or {}).get("Category") != "Deploy":
                    continue
                where = f"{pid}/{stage.get('Name')}/{a.get('Name')}"
                role = a.get("RoleArn")  # the role the pipeline assumes for the action (not the CloudFormation service role)
                if role is None:
                    problems.append(f"{where}: deploy action has no scoped deploy role")
                    continue
                if isinstance(role, str):
                    if caller_arn and role == caller_arn:
                        problems.append(f"{where}: deploy action uses the bootstrap caller's identity")
                    else:
                        problems.append(f"{where}: deploy role is a literal ARN, not a scoped role declared by the bootstrap stacks")
                    continue
                lid, rres = _resolve_role(role, resources)
                if not rres or rres.get("Type") != "AWS::IAM::Role":
                    problems.append(f"{where}: deploy role {lid!r} is not an IAM role declared by the bootstrap stacks")
                    continue
                tags = {t.get("Key"): t.get("Value") for t in (rres.get("Properties") or {}).get("Tags") or [] if isinstance(t, Mapping)}
                if tags.get("logical-role") != "deploy-role":
                    problems.append(f"{where}: role {lid} is not tagged logical-role=deploy-role")
                if not (rres.get("Properties") or {}).get("PermissionsBoundary"):
                    problems.append(f"{where}: deploy role {lid} has no permission boundary")
    return problems


def write_connection_ref(ssm_client: Any, config: BootstrapConfig) -> str:
    name = _ssm.build(_ssm.SHARED, config.repo, "config", "codeconnection-ref")
    decision = _ssm.check_write(name, _ssm.Writer(config.repo, "bootstrap"))
    if not decision:
        raise BootstrapStop("connection-ref", "; ".join(decision.reasons))
    ssm_client.put_parameter(
        Name=name,
        Value=config.codeconnection_arn,
        Type="String",
        Overwrite=True,
        Description="Reused GitHub CodeConnection for this repository's pipeline (written by the bootstrap)",
    )
    return name


def _stage_status(state: Mapping[str, Any], stage: str, execution_id: str) -> str | None:
    for st in state.get("stageStates") or []:
        if st.get("stageName") != stage:
            continue
        latest = st.get("latestExecution") or {}
        if latest.get("pipelineExecutionId") in (execution_id, None):
            return latest.get("status")
    return None


def source_dry_run(
    codepipeline_client: Any,
    config: BootstrapConfig,
    *,
    sleep: Callable[[float], None] = time.sleep,
    poll_seconds: float = 5.0,
    timeout_seconds: float = 600.0,
) -> dict[str, Any]:
    """Run an execution that stops after Source; enable the remaining stages on success (ENV-13)."""
    cp = codepipeline_client
    cp.disable_stage_transition(pipelineName=config.pipeline_name, stageName=config.next_stage, transitionType="Inbound", reason="finplan bootstrap: source-stage dry run")
    execution_id = cp.start_pipeline_execution(name=config.pipeline_name)["pipelineExecutionId"]
    waited = 0.0
    status = None
    while waited <= timeout_seconds:
        status = _stage_status(cp.get_pipeline_state(name=config.pipeline_name), config.source_stage, execution_id)
        if status in ("Succeeded", "Failed", "Stopped", "Cancelled"):
            break
        sleep(poll_seconds)
        waited += poll_seconds
    result = {
        "pipeline": config.pipeline_name,
        "execution_id": execution_id,
        "source_stage": config.source_stage,
        "status": status or "Unknown",
        "branch": "main",
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if status != "Succeeded":
        result["deploy_stages_enabled"] = False
        raise BootstrapStop("source-dry-run", EXTEND_INSTALLATION_MESSAGE.format(repository=config.github_repository))
    cp.enable_stage_transition(pipelineName=config.pipeline_name, stageName=config.next_stage, transitionType="Inbound")
    result["deploy_stages_enabled"] = True
    return result


# ===================================================================== orchestration
@dataclass
class Clients:
    sts: Any
    codeconnections: Any
    ssm: Any
    codepipeline: Any
    pricing: Any


def run_bootstrap(
    config: BootstrapConfig,
    clients: Clients,
    *,
    session_region: str | None,
    assembly_dir: str | os.PathLike[str],
    approve: Callable[[Plan], bool],
    deployer: Callable[[list[str]], None],
    out: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    record_path: str | os.PathLike[str] | None = None,
    usage_rules: Mapping[str, PricingRule] | None = None,
) -> Report:
    """Full bootstrap sequence (module docstring). Raises :class:`BootstrapStop` on the first failure."""
    report = Report()
    try:
        plan = prerun(assembly_dir, clients.pricing, region=config.primary_region, out=out, usage_rules=usage_rules)
        report.plan = plan
        report.add("prerun", "ok", f"{len(plan.stacks)} stack(s): {', '.join(plan.stack_names)}; estimate {plan.monthly_estimate_usd:.4f} USD/month", out)
        ident = check_caller(clients.sts, config)
        report.caller_is_root = is_root(ident)
        report.add("caller", "ok", "STS account matches the local configuration" + (" (root principal)" if report.caller_is_root else ""), out)
        if report.caller_is_root:
            out(ROOT_RECOMMENDATION)
        check_region(session_region, config)
        report.add("region", "ok", f"primary region {config.primary_region}", out)
        check_connection(clients.codeconnections, config)
        report.add("connection", "ok", "CodeConnection is AVAILABLE", out)
        role_problems = [p for s in plan.stacks for p in check_deploy_roles(s.template, ident.get("Arn"))]
        if role_problems:
            raise BootstrapStop("scoped-roles", "; ".join(role_problems))
        report.add("scoped-roles", "ok", "deploy actions use the scoped deploy roles", out)
        if not approve(plan):
            raise BootstrapStop("approval", "the operator did not confirm the shown stacks and cost estimate; nothing was deployed")
        report.add("approval", "ok", "operator confirmed the stacks and estimate", out)
        name = write_connection_ref(clients.ssm, config)
        report.add("connection-ref", "ok", f"wrote {name}", out)
        deployer(plan.stack_names)
        report.add("deploy", "ok", "bootstrap stacks deployed (scoped roles and pipeline)", out)
        report.dry_run = source_dry_run(clients.codepipeline, config, sleep=sleep)
        report.add("source-dry-run", "ok", "source stage fetched main; remaining stages enabled", out)
        if record_path:
            Path(record_path).expanduser().write_text(json.dumps(report.dry_run, indent=2) + "\n", encoding="utf-8")
        report.completed = True
        if report.caller_is_root:
            out(ROOT_RECOMMENDATION)
        return report
    except BootstrapStop as stop:
        report.add(stop.step, "stopped", stop.message, out)
        raise


# ===================================================================== CLI
def _boto3_clients(region: str | None) -> tuple[Clients, str | None]:  # pragma: no cover - needs AWS
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SystemExit("this command needs the optional 'aws' extra: pip install 'finplan-contracts[aws]'") from exc
    session = boto3.session.Session(region_name=region)
    return (
        Clients(
            sts=session.client("sts"),
            codeconnections=session.client("codeconnections"),
            ssm=session.client("ssm"),
            codepipeline=session.client("codepipeline"),
            pricing=session.client("pricing", region_name="us-east-1"),
        ),
        session.region_name,
    )


def main(argv: list[str], *, clients: Clients | None = None, session_region: str | None = None) -> int:
    ap = argparse.ArgumentParser(prog="finplan-conformance bootstrap-precheck", description="Bootstrap pre-run plan and read-only pre-checks (ENV-12, ENV-13).")
    ap.add_argument("--config", help="local untracked configuration (default ~/.finplan/bootstrap.json)")
    ap.add_argument("--region", help="session region (default: the CLI session's region)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("prerun", help="print the exact stacks and a cost estimate; refuse without a synthesized assembly")
    pr.add_argument("--assembly", default="cdk.out")
    sub.add_parser("check", help="read-only checks: caller account, region, connection AVAILABLE")
    args = ap.parse_args(argv)
    try:
        config = load_config(args.config)
    except (ValueError, OSError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if clients is None:  # pragma: no cover - real AWS session
        clients, session_region = _boto3_clients(args.region or config.primary_region)
    try:
        if args.cmd == "prerun":
            prerun(args.assembly, clients.pricing, region=config.primary_region)
            return 0
        ident = check_caller(clients.sts, config)
        print("[OK] caller: STS account matches the local configuration")
        if is_root(ident):
            print(ROOT_RECOMMENDATION)
        check_region(args.region or session_region, config)
        print(f"[OK] region: {config.primary_region}")
        check_connection(clients.codeconnections, config)
        print("[OK] connection: AVAILABLE")
        return 0
    except BootstrapStop as stop:
        print(f"[STOPPED] {stop.step}: {stop.message}", file=sys.stderr)
        return 1
