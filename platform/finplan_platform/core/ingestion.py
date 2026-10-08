"""The shared market-data ingestion operation (market-data-ingestion; tasks 6.1-6.9, 6.11-6.15; design P5).

:func:`run_ingestion` is the **one** implementation behind both triggers:

* on demand (``POST /v1/ingestions``: agent tools via FinanceLambdasTool, website, operators),
  ``ctx.trigger == "on_demand"``, request = contract ``refresh-market-data-request``
  (``idempotency_key`` required);
* the daily EventBridge Scheduler schedule, ``ctx.trigger == "scheduled"``, request
  ``{"dataset_id", "scheduled_time"}``; the idempotency key is derived deterministically as
  ``sched-<env>-<dataset with '/' as '_'>-<scheduled session date>`` so duplicate deliveries
  and retries return the original result (ING-09).

Pipeline inside one invocation (design P5)
------------------------------------------
1. idempotency check (:meth:`MetadataRepository.run_idempotent`; scope = caller principal,
   environment, operation ``ingest_market_data``);
2. budget pre-check (:mod:`.ingestion_budget`; ``BUDGET_EXCEEDED``, no provider call);
3. calendar resolution (holiday/weekend -> ``no_session`` without a provider call, referencing
   the latest committed snapshot; outside coverage -> ``PRECONDITION_FAILED``);
4. provider capability check (``PRECONDITION_FAILED`` ``provider_capability_unsupported``);
5. provider fetch, the raw response persisted to ``raw`` **before** parsing;
6. normalization to ``finance/v1/observation.json`` and 7. validation into quality flags and
   rejections (:mod:`.ingestion_normalize`; rejected records retained in ``raw``);
8. dedupe-persist curated observations (conditional create per key; a changed
   ``completed_daily`` value is kept as a revision and flagged ``source_revised``);
9. write-once snapshot payload and manifest (tagged ``snapshot-status=committed``);
10. one transaction: catalog row (``status`` ``committed``, trigger and caller recorded),
    idempotency record and audit event.

After the commit the snapshot is evaluated against the versioned approval rule
(:func:`.snapshots.settle_snapshot_status`): an audited conditional ``committed -> approved``
transition, then the ``snapshot-status=approved`` object tag that conditions the FinanceModel
read grant (ING-12). Failures before the commit leave no catalog row and no idempotency
record (provider throttling -> ``RATE_LIMITED`` retryable, no snapshot).

Result (contract ``tools/refresh-market-data-response``, validated by the router)::

    {"snapshot": <core/v1/input-snapshot record>, "input_snapshot_id": "snap_...",
     "requested_range": {...}, "coverage_complete": bool, "new_snapshot": bool,
     "content_checksum": "sha256:...", "trigger": "on_demand"|"scheduled", "synthetic": bool}

A no-session result has ``new_snapshot`` false, ``quality_flags`` ``["no_session"]`` and the
latest committed snapshot of the dataset (or ``input_snapshot_id`` null and no ``snapshot``,
which the contract allows since 0.2.0).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from finplan_contracts.canonical import canonicalize
from finplan_contracts.schemas import load_store
from finplan_contracts.validate import validate

from ..providers.base import FetchRequest, ProviderAdapter, ProviderThrottled, ProviderUnavailable
from . import upgrade as _upgrade
from .artifacts import (
    SNAPSHOT_STATUS_TAG,
    ArtifactExists,
    ArtifactStore,
    key_curated,
    key_raw_provider,
    key_snapshot_manifest,
    key_snapshot_payload,
    sha256_checksum,
)
from .audit import audit_event
from .calendar import SessionCalendar, calendar_for_provider, parse_date
from .clock import parse_timestamp, to_timestamp
from .config import EnvConfig, load_config
from .context import OperationContext
from .errors import PlatformError, from_validation
from .ingestion_budget import STATE_PARAMETER as BUDGET_STATE_PARAMETER
from .ingestion_budget import BudgetGate, SsmBudgetGate
from .ingestion_datasets import DatasetSpec, resolve_dataset
from .ingestion_normalize import NormalizedObservation, NormalizeResult, expected_sessions, normalize
from .repository import MetadataRepository, Mutation, PutNew
from .snapshots import APPROVAL_RULES, latest_snapshot, settle_snapshot_status

__all__ = [
    "MAX_SESSIONS_PER_REQUEST",
    "OPERATION",
    "PAYLOAD_NAME",
    "IngestionDeps",
    "IngestionOutcome",
    "default_deps",
    "deps_from_environment",
    "deps_from_services",
    "ingest",
    "run_ingestion",
    "scheduled_idempotency_key",
    "set_default_deps",
]

OPERATION = "ingest_market_data"
MAX_SESSIONS_PER_REQUEST = 600
PAYLOAD_NAME = "observations.json"
DATASET_VERSION = "daily-norm-v1"
DOMAIN = "finance"
DOMAIN_SCHEMA_VERSION = "1.0"


# ===================================================================== dependencies
@dataclass
class IngestionDeps:
    """Everything the operation needs, injected (Lambda: :func:`deps_from_environment`; tests: fakes)."""

    config: EnvConfig
    repo: MetadataRepository
    store: ArtifactStore
    provider: ProviderAdapter
    calendar: SessionCalendar
    budget_gate: BudgetGate | None = None
    contract_version: str = field(default_factory=lambda: load_store().version)

    def __post_init__(self) -> None:
        rule = self.config.ingest["approval"]["rule_version"]
        if rule not in APPROVAL_RULES:
            raise PlatformError.precondition(f"approval rule {rule!r} is not implemented", reason="unknown_approval_rule", known=sorted(APPROVAL_RULES))
        if self.calendar.synthetic and self.provider.describe().provider_id not in ("fixture", "mock"):
            raise PlatformError.precondition("the synthetic fixture calendar may be used only with the fixture provider and in tests", reason="synthetic_calendar")

    @property
    def settle_delay(self) -> timedelta:
        return timedelta(minutes=int(self.config.ingest["settle_delay_minutes"]))

    @property
    def dataset(self) -> DatasetSpec:
        from .ingestion_datasets import enabled_dataset

        return enabled_dataset(self.config)


@dataclass(frozen=True)
class IngestionOutcome:
    response: dict[str, Any]
    replayed: bool


_DEFAULT: IngestionDeps | None = None


def set_default_deps(deps: IngestionDeps | None) -> None:
    global _DEFAULT
    _DEFAULT = deps


def default_deps() -> IngestionDeps:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = deps_from_environment()
    return _DEFAULT


def deps_from_environment(environ: Mapping[str, str] | None = None) -> IngestionDeps:  # pragma: no cover - Lambda wiring
    """Lambda wiring: ``FINPLAN_ENV``, ``FINPLAN_BUCKET_{RAW,CURATED,SNAPSHOTS}``, ``FINPLAN_KMS_KEY_ARN``,
    ``FINPLAN_BUDGET_STATE_PARAMETER`` (default: the contract parameter)."""
    import boto3

    from .aws_clients import s3_client

    from ..providers.registry import provider_for_config

    e = dict(environ or os.environ)
    cfg = load_config(e["FINPLAN_ENV"])
    buckets = {role: e[f"FINPLAN_BUCKET_{role.upper()}"] for role in ("raw", "curated", "snapshots") if e.get(f"FINPLAN_BUCKET_{role.upper()}")}
    store = ArtifactStore(s3_client(), buckets, kms_key_id=e.get("FINPLAN_KMS_KEY_ARN") or None)
    repo = MetadataRepository(boto3.client("dynamodb"), cfg.env, idempotency_ttl_days=int(cfg.metadata["idempotency_ttl_days"]))
    gate = SsmBudgetGate(boto3.client("ssm"), e.get("FINPLAN_BUDGET_STATE_PARAMETER") or BUDGET_STATE_PARAMETER)
    return IngestionDeps(config=cfg, repo=repo, store=store, provider=provider_for_config(cfg), calendar=calendar_for_provider(cfg.provider), budget_gate=gate)


# ===================================================================== request planning
@dataclass(frozen=True)
class _Plan:
    dataset: DatasetSpec
    start: date
    end: date
    granularity: str
    idempotency_key: str
    request_body: dict[str, Any]
    requested_range: dict[str, str]
    no_session: bool = False
    session_status: str | None = None


def scheduled_idempotency_key(env: str, dataset_id: str, session_date: date) -> str:
    """``sched-<env>-<dataset>-<date>``; the dataset's ``/`` become ``_`` to fit the contract key pattern."""
    return f"sched-{env}-{dataset_id.replace('/', '_')}-{session_date.isoformat()}"


def _served_major(version: str) -> int:
    return int(version.split(".")[0])


def _plan_on_demand(ctx: OperationContext, body: Any, deps: IngestionDeps) -> _Plan:
    if not isinstance(body, Mapping):
        raise PlatformError.validation("request body must be a JSON object", pointer="")
    doc = dict(body)
    granularity = doc.get("granularity")
    probe = dict(doc)
    if granularity == "intraday":  # reaches the capability check below instead of a schema error (ING-05)
        probe["granularity"] = "daily"
    res = validate(probe, "tools/refresh-market-data-request")
    if not res.valid:
        raise from_validation(res)
    served = sorted({_served_major(deps.contract_version), *_upgrade.ADDITIONAL_SERVED_MAJORS})
    if "contract_version" in doc and _served_major(str(doc["contract_version"])) not in served:
        raise PlatformError(
            "UNSUPPORTED_CONTRACT_VERSION",
            "the request's contract major is not served",
            requested=doc["contract_version"],
            served_majors=served,
        )
    dataset = resolve_dataset(deps.config, doc["dataset_id"])
    ids = doc.get("instrument_ids")
    if ids is not None and any(i != dataset.instrument_id for i in ids):
        raise PlatformError.validation("instrument_ids must name the dataset's instrument", pointer="/instrument_ids", allowed=[dataset.instrument_id])
    start, end = parse_date(doc["start_date"], "/start_date"), parse_date(doc["end_date"], "/end_date")
    if start > end:
        raise PlatformError.validation("start_date must not be after end_date", pointer="/start_date")
    caps = deps.provider.describe()
    if not caps.supports(dataset.dataset_id, str(granularity)):
        raise PlatformError.precondition(
            f"provider {caps.provider_id!r} does not declare {granularity!r} for this dataset",
            reason="provider_capability_unsupported",
            provider=caps.provider_id,
            requested_granularity=granularity,
            declared_granularities=list(caps.granularities),
        )
    deps.calendar.require_covered(start, end)
    sessions = deps.calendar.sessions_between(start, end)
    if len(sessions) > MAX_SESSIONS_PER_REQUEST:
        raise PlatformError.validation(f"at most {MAX_SESSIONS_PER_REQUEST} sessions per request", pointer="/end_date", sessions=len(sessions))
    return _Plan(
        dataset=dataset,
        start=start,
        end=end,
        granularity=str(granularity),
        idempotency_key=str(doc["idempotency_key"]),
        request_body=doc,
        requested_range={"start": start.isoformat(), "end": end.isoformat()},
        no_session=not sessions,
        session_status=None if sessions else deps.calendar.status(start) if start == end else "no_session_in_range",
    )


def _plan_scheduled(ctx: OperationContext, body: Any, deps: IngestionDeps) -> _Plan:
    body = dict(body or {})
    dataset = resolve_dataset(deps.config, body.get("dataset_id") or deps.config.dataset_id)
    raw_time = body.get("scheduled_time")
    try:
        scheduled_at = parse_timestamp(str(raw_time)) if raw_time else ctx.now()
    except ValueError:
        raise PlatformError.validation("scheduled_time must be an RFC 3339 timestamp", pointer="/scheduled_time") from None
    cal = deps.calendar
    fire_date = cal.local_date(scheduled_at)
    cal.require_covered(fire_date)
    key = scheduled_idempotency_key(ctx.env, dataset.dataset_id, fire_date)
    request_body = {"trigger": "scheduled", "dataset_id": dataset.dataset_id, "scheduled_session_date": fire_date.isoformat(), "granularity": "daily"}
    status = cal.status(fire_date)
    if status not in ("regular", "early_close"):
        return _Plan(dataset, fire_date, fire_date, "daily", key, request_body, {"start": fire_date.isoformat(), "end": fire_date.isoformat()}, no_session=True, session_status=status)
    # the run fires before the open: ingest the most recent session completed at the scheduled time
    target = cal.latest_closed_session(scheduled_at).day
    return _Plan(dataset, target, target, "daily", key, request_body, {"start": target.isoformat(), "end": target.isoformat()}, session_status=status)


# ===================================================================== execution
def _compact(ts: datetime) -> str:
    return ts.strftime("%Y%m%dT%H%M%SZ")


@dataclass
class _Curated:
    checksum: str
    created: bool
    revised: bool


def _persist_curated(store: ArtifactStore, dataset_id: str, n: NormalizedObservation) -> _Curated:
    """Conditional create per dedupe key (dataset, instrument, session date, kind, source timestamp)."""
    content = {"dataset_id": dataset_id, "observation": n.observation, "source_ts": n.source_ts}
    body = canonicalize(content)
    checksum = sha256_checksum(body)
    prefix = f"{dataset_id}/{n.instrument_id}/{n.session_date}/{n.kind}/"
    existing = []
    for k, _modified in store.list_objects("curated", prefix):
        head = store.head("curated", k)
        existing.append(head.checksum if head else "")
    if checksum in existing:
        return _Curated(checksum, created=False, revised=False)
    key = key_curated(dataset_id, n.instrument_id, n.session_date, n.kind, _compact(parse_timestamp(n.source_ts)))
    try:
        store.put_once("curated", key, body)
    except ArtifactExists:
        # same source timestamp, different content: keep both under a content-qualified key
        store.put_once_or_verify("curated", key[: -len(".json")] + f"-r{checksum[7:19]}.json", body)
    return _Curated(checksum, created=True, revised=bool(existing) and n.kind == "completed_daily")


def _no_session_response(ctx: OperationContext, plan: _Plan, deps: IngestionDeps) -> dict[str, Any]:
    latest = latest_snapshot(deps.repo, plan.dataset.dataset_id)
    resp: dict[str, Any] = {
        "input_snapshot_id": latest.id if latest else None,
        "requested_range": plan.requested_range,
        "coverage_complete": False,
        "new_snapshot": False,
        "quality_flags": ["no_session"],
        "session_status": plan.session_status,
        "trigger": ctx.trigger,
        "synthetic": bool(deps.provider.describe().synthetic),
    }
    if latest is not None:
        resp["snapshot"] = latest.view()
    return resp


def _fetch(deps: IngestionDeps, plan: _Plan):
    try:
        return deps.provider.fetch(FetchRequest(plan.dataset.dataset_id, plan.dataset.instrument_id, plan.start, plan.end, plan.granularity))
    except ProviderThrottled as exc:
        raise PlatformError("RATE_LIMITED", "the market-data provider is rate-limiting requests; retry later", retryable=True, provider=deps.provider.describe().provider_id, attempts=exc.attempts) from None
    except ProviderUnavailable as exc:
        raise PlatformError("DEPENDENCY_UNAVAILABLE", "the market-data provider is unavailable; retry later", retryable=True, provider=deps.provider.describe().provider_id, attempts=exc.attempts) from None


def _execute(ctx: OperationContext, plan: _Plan, deps: IngestionDeps) -> Mutation:
    if deps.budget_gate is not None:
        deps.budget_gate(ctx)  # BUDGET_EXCEEDED before any provider call (COST-05)
    if plan.no_session:
        return Mutation(ops=[], response=_no_session_response(ctx, plan, deps))

    caps = deps.provider.describe()
    cal = deps.calendar
    now = ctx.now()
    intraday_ok = "intraday" in caps.granularities and plan.granularity == "intraday"
    if not intraday_ok and not expected_sessions(cal, plan.start, plan.end, now=now, settle=deps.settle_delay, finality=caps.finality):
        last = cal.sessions_between(plan.start, plan.end)[-1].day
        ready = cal.session_close_utc(last) + (deps.settle_delay if caps.finality == "inferred" else timedelta(0))
        raise PlatformError.precondition(
            "no session in the requested range has completed yet",
            reason="session_not_completed",
            latest_session=last.isoformat(),
            completed_after=to_timestamp(ready),
        )

    # 5. fetch; persist the raw response before parsing
    raw = _fetch(deps, plan)
    retrieved_at = raw.retrieved_at
    dataset_id = plan.dataset.dataset_id
    raw_stored = deps.store.put_once("raw", key_raw_provider(caps.provider_id, dataset_id, _compact(retrieved_at), ctx.ids.ulid()), raw.body, raw.content_type)  # type: ignore[union-attr]
    try:
        records = deps.provider.parse(raw)
    except (ValueError, KeyError, TypeError):
        raise PlatformError("DEPENDENCY_UNAVAILABLE", "the provider response could not be parsed; it is retained for analysis", retryable=True, provider=caps.provider_id) from None

    # 6-7. normalize and validate
    synthetic = bool(caps.synthetic)
    norm: NormalizeResult = normalize(
        records,
        instrument_id=plan.dataset.instrument_id,
        start=plan.start,
        end=plan.end,
        calendar=cal,
        capabilities=caps,
        now=retrieved_at,
        settle_delay=deps.settle_delay,
        synthetic=synthetic,
    )
    rejections_checksum = None
    if norm.rejected:
        rej = {"kind": "rejected_records", "provider": caps.provider_id, "dataset_id": dataset_id, "raw_response_checksum": raw_stored.checksum, "records": norm.rejected, "synthetic": synthetic}
        stored = deps.store.put_once("raw", key_raw_provider(caps.provider_id, dataset_id, _compact(retrieved_at), ctx.ids.ulid() + "-rejected"), canonicalize(rej))  # type: ignore[union-attr]
        rejections_checksum = stored.checksum

    # 8. dedupe-persist curated observations
    flags = set(norm.flags)
    revised: list[dict[str, str]] = []
    curated_refs: list[dict[str, Any]] = []
    created_any = False
    for n in norm.observations:
        c = _persist_curated(deps.store, dataset_id, n)
        created_any |= c.created
        if c.revised:
            revised.append({"instrument_id": n.instrument_id, "session_date": n.session_date})
        curated_refs.append({"instrument_id": n.instrument_id, "session_date": n.session_date, "kind": n.kind, "source_ts": n.source_ts, "curated_checksum": c.checksum})
    if revised:
        flags.add("source_revised")
    if not created_any:
        flags.add("no_new_observations")
    details = norm.details()
    if revised:
        details["source_revised"] = revised

    # 9. write-once payload and manifest (object tag mirrors the catalog status)
    sid = ctx.new_id("input_snapshot_id")
    payload = {
        "dataset_id": dataset_id,
        "calendar": cal.exchange,
        "instruments": [plan.dataset.instrument(synthetic=synthetic)],
        "observations": [n.observation for n in norm.observations],
    }
    if synthetic:
        payload["synthetic"] = True
    tags = {SNAPSHOT_STATUS_TAG: "committed"}
    payload_art = deps.store.put_once("snapshots", key_snapshot_payload(sid, PAYLOAD_NAME), canonicalize(payload), tags=tags)
    lineage: dict[str, Any] = {
        "provider": caps.provider_id,
        "provider_library": caps.library,
        "library_version": caps.library_version,
        "retrieved_at": to_timestamp(retrieved_at),
        "calendar_version": cal.version,
    }
    completed = [n for n in norm.observations if n.kind == "completed_daily"]
    covered = sorted({n.session_date for n in (completed or norm.observations)})
    coverage = {"start": covered[0], "end": covered[-1]} if covered else dict(plan.requested_range)
    stamps = sorted(n.source_ts for n in norm.observations)
    source_timestamps = {"earliest": stamps[0], "latest": stamps[-1]} if stamps else {"earliest": to_timestamp(retrieved_at), "latest": to_timestamp(retrieved_at)}
    quality_flags = sorted(flags)
    summary = {"completed_daily": len(completed), "intraday_partial": len(norm.observations) - len(completed), "records_received": norm.row_count, "records_rejected": len(norm.rejected)}
    payload_ref = payload_art.to_ref(ctx.new_id("artifact_id"), "snapshot_payload", synthetic=synthetic if synthetic else None)
    manifest = {
        "manifest_version": "snapshot-manifest-v1",
        "input_snapshot_id": sid,
        "domain": DOMAIN,
        "domain_schema_version": DOMAIN_SCHEMA_VERSION,
        "dataset": {"dataset_id": dataset_id, "dataset_version": DATASET_VERSION},
        "adjustment_basis": plan.dataset.adjustment_basis,
        "currency": plan.dataset.currency,
        "lineage": lineage,
        "calendar": {"exchange": cal.exchange, "version": cal.version, "synthetic": cal.synthetic},
        "requested_range": plan.requested_range,
        "coverage": coverage,
        "source_timestamps": source_timestamps,
        "quality_flags": quality_flags,
        "quality_details": details,
        "observation_summary": summary,
        "observations": curated_refs,
        "payload": {"artifact_id": payload_ref["artifact_id"], "checksum": payload_art.checksum, "size_bytes": payload_art.size_bytes},
        "raw_response": {"checksum": raw_stored.checksum, "attempts": raw.attempts},
        "trigger": ctx.trigger,
        "created_at": ctx.now_ts(),
        "contract_version": deps.contract_version,
    }
    if rejections_checksum:
        manifest["rejected_records"] = {"checksum": rejections_checksum, "count": len(norm.rejected)}
    if synthetic:
        manifest["synthetic"] = True
    manifest_art = deps.store.put_once("snapshots", key_snapshot_manifest(sid), canonicalize(manifest), tags=tags)
    manifest_ref = manifest_art.to_ref(ctx.new_id("artifact_id"), "snapshot_manifest", synthetic=synthetic if synthetic else None)

    doc: dict[str, Any] = {
        "input_snapshot_id": sid,
        "domain": DOMAIN,
        "domain_schema_version": DOMAIN_SCHEMA_VERSION,
        "dataset": {"dataset_id": dataset_id, "dataset_version": DATASET_VERSION},
        "manifest_checksum": manifest_art.checksum,
        "source_timestamps": source_timestamps,
        "lineage": lineage,
        "coverage": coverage,
        "quality_flags": quality_flags,
        "status": "committed",
        "artifacts": [manifest_ref, payload_ref],
        "created_at": ctx.now_ts(),
        "quality_details": details,
        "observation_summary": summary,
    }
    if synthetic:
        doc["synthetic"] = True
    res = validate(doc, "input-snapshot")
    if not res.valid:  # pragma: no cover - a platform bug, never a client error
        raise PlatformError.internal("snapshot record failed contract validation", issues=[i.pointer for i in res.issues][:5])

    coverage_complete = bool(norm.observations) and not ({"missing_sessions", "rejected_records", "empty_response"} & flags)
    response = {
        "snapshot": doc,
        "input_snapshot_id": sid,
        "requested_range": plan.requested_range,
        "coverage_complete": coverage_complete,
        "new_snapshot": True,
        "content_checksum": payload_art.checksum,
        "trigger": ctx.trigger,
    }
    if synthetic:
        response["synthetic"] = True
    ev = audit_event(
        ctx,
        record_id=sid,
        record_type="snapshot_catalog",
        operation="commit_snapshot",
        prior=None,
        new={"status": "committed"},
        dataset_id=dataset_id,
        manifest_checksum=manifest_art.checksum,
        quality_flags=quality_flags,
        provider=caps.provider_id,
    )
    extra = {"trigger": ctx.trigger, "caller_principal": ctx.caller.principal, "correlation_id": ctx.correlation_id, "content_checksum": payload_art.checksum}
    return Mutation(ops=[PutNew("snapshot_catalog", doc=doc, contract_version=deps.contract_version, extra_attrs=extra)], response=response, audit=[ev])


# ===================================================================== entry points
def deps_from_services(svc: Any, ctx: OperationContext) -> IngestionDeps:
    """Ingestion deps from the plan API's ``Services`` (in-process ``POST /v1/ingestions``).

    ``svc`` provides ``config``/``cfg``, ``repo`` and ``store``; ``svc.extras`` may carry an
    ``ingestion_provider`` (tests), a ``budget_gate`` or an ``ssm`` client (Lambda: the budget
    state is read from SSM). The provider runs on the operation clock.
    """
    from ..providers.registry import provider_for_config

    cfg = getattr(svc, "config", None) or svc.cfg
    extras = dict(getattr(svc, "extras", {}) or {})
    provider = extras.get("ingestion_provider") or provider_for_config(cfg, clock=ctx.clock)
    gate = extras.get("budget_gate")
    if gate is None and extras.get("ssm") is not None:
        gate = SsmBudgetGate(extras["ssm"], extras.get("budget_state_parameter") or BUDGET_STATE_PARAMETER)
    calendar = extras.get("ingestion_calendar") or calendar_for_provider(provider.describe().provider_id)
    return IngestionDeps(config=cfg, repo=svc.repo, store=svc.store, provider=provider, calendar=calendar, budget_gate=gate)


def ingest(ctx: OperationContext, request: Any, *, deps: IngestionDeps | None = None, svc: Any = None) -> IngestionOutcome:
    """Run the ingestion operation and report whether the result was an idempotent replay."""
    if deps is None:
        deps = deps_from_services(svc, ctx) if svc is not None else default_deps()
    if ctx.env != deps.config.env:
        raise PlatformError.precondition("the operation context names another environment", reason="environment_mismatch")
    plan = _plan_scheduled(ctx, request, deps) if ctx.trigger == "scheduled" else _plan_on_demand(ctx, request, deps)
    outcome = deps.repo.run_idempotent(ctx, operation=OPERATION, idempotency_key=plan.idempotency_key, request_body=plan.request_body, execute=lambda: _execute(ctx, plan, deps))
    response = dict(outcome.response)
    if response.get("new_snapshot") and response.get("input_snapshot_id"):
        # approval (and a repair of the tag mirror on replays); the response shows the current status
        rec = settle_snapshot_status(ctx, deps, str(response["input_snapshot_id"]))
        response["snapshot"] = rec.view()
    return IngestionOutcome(response, outcome.replayed)


def run_ingestion(ctx: OperationContext, request: Any, *, deps: IngestionDeps | None = None, svc: Any = None) -> dict[str, Any]:
    """The single ingestion operation used by the API route and the scheduler handler.

    Pass ``deps`` (tests, the ingestion Lambda) or the plan API's ``svc`` (in-process route);
    with neither, the deps are built once from the Lambda environment.
    """
    return ingest(ctx, request, deps=deps, svc=svc).response
