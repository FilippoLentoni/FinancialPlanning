"""Per-environment platform configuration (task 1.3).

Configuration lives in ``config/<env>.json`` (beta, gamma, prod) plus
``config/shared.json`` (account-level defaults). It is read at synth time by the CDK
app and at run time by handlers (bundled with the function code). It contains only
settings, placeholders and SSM parameter *names*; never an account ID, ARN, bucket,
role or endpoint value (the leak scan runs over ``config/`` in the unit suite).

Validation happens in two layers:

1. :data:`ENV_CONFIG_SCHEMA` (JSON Schema 2020-12, platform-internal, deliberately not a
   contract schema and carrying no contract ``$id``);
2. semantic rules that JSON Schema cannot express cleanly, each tagged with the spec
   test it serves:

   * ING-02: ``ingest.schedule_time`` is ``09:00`` or ``09:30`` (schema enum; the build
     fails before deployment);
   * ING-10: phase 1 allows only the ``fixture`` provider; phase 2 allows ``fixture`` or
     ``yfinance``;
   * ING-13: the dataset is ``etf-daily`` with ``granularity`` ``daily``; intraday fails;
   * STO-06: beta and gamma must set every retention value; prod may leave
     ``raw``/``curated``/``snapshots``/``plans``/``outputs``/``reports`` unset (``null``),
     meaning no expiry;
   * environment isolation: consumer principal SSM keys and role-name patterns name the
     same environment.

``load_config(env)`` raises :class:`ConfigError` listing every problem.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from finplan_contracts import ssm as contract_ssm
from jsonschema import Draft202012Validator

__all__ = [
    "ENVIRONMENTS",
    "SCHEDULE_TIMES",
    "DEFAULT_SCHEDULE_TIME",
    "PHASE1_PROVIDERS",
    "PHASE2_PROVIDERS",
    "BUCKET_ROLES",
    "ENV_CONFIG_SCHEMA",
    "ConfigProblem",
    "ConfigError",
    "EnvConfig",
    "config_dir",
    "validate_config",
    "load_config",
    "load_all",
    "load_shared_config",
    "schedule_expression",
]

ENVIRONMENTS = ("beta", "gamma", "prod")
SCHEDULE_TIMES = ("09:00", "09:30")
DEFAULT_SCHEDULE_TIME = "09:00"
PHASE1_PROVIDERS = ("fixture",)
PHASE2_PROVIDERS = ("fixture", "yfinance")
#: The six logical bucket roles (design P2).
BUCKET_ROLES = ("raw", "curated", "snapshots", "plans", "outputs", "reports")
#: Retention keys that prod may leave unset (no expiry).
_EXPIRING_ROLES = tuple(f"{r}_days" for r in BUCKET_ROLES)
_CONSUMERS = (
    "financemodel_job",
    "financemodel_job_api",
    "tool_reader",
    "tool_submitter",
    "tool_plan_writer",
    "website_backend",
    "operator",
)

_days = {"type": "integer", "minimum": 1, "maximum": 3650}
_days_or_null = {"type": ["integer", "null"], "minimum": 1, "maximum": 3650}
_pos = {"type": "number", "exclusiveMinimum": 0}

ENV_CONFIG_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "FinancialPlanning platform environment configuration (platform-internal)",
    "type": "object",
    "additionalProperties": False,
    "required": ["environment", "region", "phase", "ingest", "retention", "metadata", "limits", "validation", "consumer_principals"],
    "properties": {
        "$comment": {"type": "string"},
        "environment": {"enum": list(ENVIRONMENTS)},
        "region": {"type": "string", "pattern": "^[a-z]{2}(-gov)?-[a-z]+-[0-9]$"},
        "phase": {"enum": [1, 2]},
        "ingest": {
            "type": "object",
            "additionalProperties": False,
            "required": ["schedule_time", "timezone", "provider", "provider_settings", "settle_delay_minutes", "dataset", "function_timeout_seconds", "approval"],
            "properties": {
                "schedule_time": {"enum": list(SCHEDULE_TIMES)},
                "timezone": {"const": "America/New_York"},
                "provider": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,31}$"},
                "provider_settings": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["min_request_interval_seconds", "max_attempts", "backoff_initial_seconds", "backoff_max_seconds", "jitter"],
                    "properties": {
                        "min_request_interval_seconds": {"type": "number", "minimum": 0},
                        "max_attempts": {"type": "integer", "minimum": 1, "maximum": 10},
                        "backoff_initial_seconds": _pos,
                        "backoff_max_seconds": _pos,
                        "jitter": {"type": "boolean"},
                    },
                },
                "settle_delay_minutes": {"type": "integer", "minimum": 0, "maximum": 1440},
                "function_timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 900},
                "dataset": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "instrument", "granularity", "currency", "adjustment_basis"],
                    "properties": {
                        "kind": {"type": "string"},
                        "instrument": {"type": "string", "pattern": "^[A-Z][A-Z0-9.]{0,9}$"},
                        "granularity": {"type": "string"},
                        "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
                        "adjustment_basis": {"enum": ["unadjusted", "split_adjusted", "split_and_dividend_adjusted"]},
                    },
                },
                "approval": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["rule_version", "rejected_records_max_ratio"],
                    "properties": {
                        "rule_version": {"type": "string", "pattern": "^[a-z0-9][a-z0-9.-]{0,63}$"},
                        "rejected_records_max_ratio": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                },
            },
        },
        "retention": {
            "type": "object",
            "additionalProperties": False,
            "required": [*_EXPIRING_ROLES, "staging_window_days", "noncurrent_version_days", "incomplete_multipart_days"],
            "properties": {
                **{k: _days_or_null for k in _EXPIRING_ROLES},
                "staging_window_days": _days,
                "noncurrent_version_days": _days,
                "incomplete_multipart_days": _days,
            },
        },
        "metadata": {
            "type": "object",
            "additionalProperties": False,
            "required": ["idempotency_ttl_days", "orphan_grace_hours", "point_in_time_recovery"],
            "properties": {
                "idempotency_ttl_days": {"type": "integer", "minimum": 7, "maximum": 30},
                "orphan_grace_hours": {"type": "integer", "minimum": 24, "maximum": 168},
                "point_in_time_recovery": {"const": True},
            },
        },
        "limits": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "excel_upload_max_bytes",
                "excel_upload_grant_ttl_seconds",
                "excel_max_uncompressed_bytes",
                "excel_max_compression_ratio",
                "download_grant_ttl_seconds",
                "page_size_max",
            ],
            "properties": {
                "excel_upload_max_bytes": {"type": "integer", "minimum": 1024, "maximum": 52428800},
                "excel_upload_grant_ttl_seconds": {"type": "integer", "minimum": 60, "maximum": 900},
                "excel_max_uncompressed_bytes": {"type": "integer", "minimum": 1024, "maximum": 268435456},
                "excel_max_compression_ratio": {"type": "number", "minimum": 1, "maximum": 1000},
                "download_grant_ttl_seconds": {"type": "integer", "minimum": 60, "maximum": 3600},
                "page_size_max": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
        "validation": {
            "type": "object",
            "additionalProperties": False,
            "required": ["ruleset_version", "weight_sum_tolerance"],
            "properties": {
                "ruleset_version": {"type": "string", "pattern": "^[a-z0-9][a-z0-9.-]{0,63}$"},
                "weight_sum_tolerance": {"type": "number", "exclusiveMinimum": 0, "maximum": 0.01},
            },
        },
        "consumer_principals": {
            "type": "object",
            "additionalProperties": False,
            "required": list(_CONSUMERS),
            "properties": {
                c: {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["ssm_key", "role_name_pattern"],
                    "properties": {
                        "ssm_key": {"type": "string"},
                        "role_name_pattern": {"type": "string", "pattern": "^finplan-(beta|gamma|prod)-[a-z0-9-]+\\*?$"},
                        "registered": {"type": "boolean"},
                        "$comment": {"type": "string"},
                    },
                }
                for c in _CONSUMERS
            },
        },
    },
}

_VALIDATOR = Draft202012Validator(ENV_CONFIG_SCHEMA)


@dataclass(frozen=True)
class ConfigProblem:
    pointer: str
    message: str
    test_id: str | None = None

    def __str__(self) -> str:
        tag = f" [{self.test_id}]" if self.test_id else ""
        return f"{self.pointer or '/'}: {self.message}{tag}"


class ConfigError(ValueError):
    def __init__(self, problems: list[ConfigProblem], source: str = "configuration") -> None:
        self.problems = problems
        super().__init__(f"invalid {source}:\n  " + "\n  ".join(str(p) for p in problems))


def _ptr(path: Any) -> str:
    return "".join(f"/{p}" for p in path)


_TEST_BY_FIELD = {"schedule_time": "ING-02", "timezone": "ING-02", "provider": "ING-10", "granularity": "ING-13", "kind": "ING-13"}


def validate_config(doc: Any, *, env: str | None = None) -> list[ConfigProblem]:
    """All problems of one environment configuration document (empty when valid)."""
    problems: list[ConfigProblem] = []
    for err in sorted(_VALIDATOR.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        last = err.absolute_path[-1] if err.absolute_path else None
        problems.append(ConfigProblem(_ptr(err.absolute_path), err.message, _TEST_BY_FIELD.get(str(last))))
    if not isinstance(doc, dict):
        return problems
    environment = doc.get("environment")
    if env is not None and environment != env:
        problems.append(ConfigProblem("/environment", f"file for {env!r} declares environment {environment!r}"))
    phase = doc.get("phase")
    ingest = doc.get("ingest") if isinstance(doc.get("ingest"), dict) else {}
    provider = ingest.get("provider")
    if phase == 1 and provider not in PHASE1_PROVIDERS:
        problems.append(ConfigProblem("/ingest/provider", f"phase 1 allows only the fixture provider, got {provider!r}", "ING-10"))
    elif phase == 2 and provider not in PHASE2_PROVIDERS:
        problems.append(ConfigProblem("/ingest/provider", f"phase 2 allows {list(PHASE2_PROVIDERS)}, got {provider!r}", "ING-10"))
    ds = ingest.get("dataset") if isinstance(ingest.get("dataset"), dict) else {}
    if ds:
        if ds.get("granularity") != "daily":
            problems.append(ConfigProblem("/ingest/dataset/granularity", f"only daily completed observations are allowed in phases 1 and 2, got {ds.get('granularity')!r}", "ING-13"))
        if ds.get("kind") != "etf-daily":
            problems.append(ConfigProblem("/ingest/dataset/kind", f"the only enabled dataset is etf-daily, got {ds.get('kind')!r}", "ING-13"))
    ps = ingest.get("provider_settings") if isinstance(ingest.get("provider_settings"), dict) else {}
    if ps and isinstance(ps.get("backoff_initial_seconds"), (int, float)) and isinstance(ps.get("backoff_max_seconds"), (int, float)):
        if ps["backoff_max_seconds"] < ps["backoff_initial_seconds"]:
            problems.append(ConfigProblem("/ingest/provider_settings/backoff_max_seconds", "must be >= backoff_initial_seconds"))
        timeout = ingest.get("function_timeout_seconds")
        attempts = ps.get("max_attempts")
        if isinstance(timeout, int) and isinstance(attempts, int):
            worst = sum(min(ps["backoff_max_seconds"], ps["backoff_initial_seconds"] * 2**i) for i in range(max(attempts - 1, 0)))
            worst += attempts * float(ps.get("min_request_interval_seconds") or 0)
            if worst >= timeout:
                problems.append(ConfigProblem("/ingest/provider_settings", f"worst-case retry time {worst:.1f}s does not fit inside the function timeout {timeout}s", "ING-17"))
    ret = doc.get("retention") if isinstance(doc.get("retention"), dict) else {}
    if environment in ("beta", "gamma"):
        for k in _EXPIRING_ROLES:
            if k in ret and ret[k] is None:
                problems.append(ConfigProblem(f"/retention/{k}", f"{environment} must expire all artifacts; set a retention value", "STO-06"))
    if isinstance(environment, str) and environment in ENVIRONMENTS:
        cps = doc.get("consumer_principals") if isinstance(doc.get("consumer_principals"), dict) else {}
        for name, ref in cps.items():
            if not isinstance(ref, dict):
                continue
            key = ref.get("ssm_key")
            if isinstance(key, str):
                errs = contract_ssm.validation_errors(key)
                if errs:
                    problems.append(ConfigProblem(f"/consumer_principals/{name}/ssm_key", "; ".join(errs)))
                elif contract_ssm.parse(key).environment != environment:
                    problems.append(ConfigProblem(f"/consumer_principals/{name}/ssm_key", f"must reference the {environment} environment"))
                elif ref.get("registered") is not False and contract_ssm.find_registered(key) is None:
                    problems.append(ConfigProblem(f"/consumer_principals/{name}/ssm_key", "is not a registered contract SSM key; mark it registered: false (proposed) or use the registered key"))
            pat = ref.get("role_name_pattern")
            if isinstance(pat, str) and not pat.startswith(f"finplan-{environment}-"):
                problems.append(ConfigProblem(f"/consumer_principals/{name}/role_name_pattern", f"must name a {environment} principal (finplan-{environment}-...)"))
    return problems


@dataclass(frozen=True)
class EnvConfig:
    """A validated environment configuration (read-only view over the JSON document)."""

    data: Mapping[str, Any] = field(repr=False)

    @property
    def env(self) -> str:
        return str(self.data["environment"])

    @property
    def region(self) -> str:
        return str(self.data["region"])

    @property
    def phase(self) -> int:
        return int(self.data["phase"])

    @property
    def ingest(self) -> Mapping[str, Any]:
        return self.data["ingest"]

    @property
    def provider(self) -> str:
        return str(self.data["ingest"]["provider"])

    @property
    def schedule_time(self) -> str:
        return str(self.data["ingest"]["schedule_time"])

    @property
    def dataset(self) -> Mapping[str, Any]:
        return self.data["ingest"]["dataset"]

    @property
    def dataset_id(self) -> str:
        """``finance/etf-daily/<instrument>`` (design P6)."""
        return f"finance/{self.dataset['kind']}/{self.dataset['instrument']}"

    @property
    def retention(self) -> Mapping[str, Any]:
        return self.data["retention"]

    def retention_days(self, role: str) -> int | None:
        """Expiry in days for a bucket role, or ``None`` (no expiry, prod only)."""
        if role not in BUCKET_ROLES:
            raise KeyError(role)
        return self.data["retention"][f"{role}_days"]

    @property
    def metadata(self) -> Mapping[str, Any]:
        return self.data["metadata"]

    @property
    def limits(self) -> Mapping[str, Any]:
        return self.data["limits"]

    @property
    def validation(self) -> Mapping[str, Any]:
        return self.data["validation"]

    @property
    def consumer_principals(self) -> Mapping[str, Mapping[str, Any]]:
        return self.data["consumer_principals"]

    def principal_pattern(self, consumer: str) -> str:
        return str(self.data["consumer_principals"][consumer]["role_name_pattern"])


def config_dir() -> Path:
    """``config/`` of the repository (or ``FINPLAN_CONFIG_DIR``; Lambda bundles copy it)."""
    import os

    env = os.environ.get("FINPLAN_CONFIG_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "config"
        if (cand / "beta.json").is_file():
            return cand
    raise FileNotFoundError("config directory not found; set FINPLAN_CONFIG_DIR")


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_config(env: str, directory: str | Path | None = None) -> EnvConfig:
    if env not in ENVIRONMENTS:
        raise ValueError(f"unknown environment {env!r}")
    path = Path(directory or config_dir()) / f"{env}.json"
    doc = _read(path)
    problems = validate_config(doc, env=env)
    if problems:
        raise ConfigError(problems, source=path.name)
    return EnvConfig(doc)


def load_all(directory: str | Path | None = None) -> dict[str, EnvConfig]:
    return {e: load_config(e, directory) for e in ENVIRONMENTS}


def load_shared_config(directory: str | Path | None = None) -> dict[str, Any]:
    """Account-level defaults (``config/shared.json``)."""
    doc = _read(Path(directory or config_dir()) / "shared.json")
    ceiling = doc.get("cost_ceiling_usd_default")
    if not isinstance(ceiling, (int, float)) or ceiling <= 0:
        raise ConfigError([ConfigProblem("/cost_ceiling_usd_default", "must be a positive number", "COST-01")], "shared.json")
    return doc


_TIME_RE = re.compile(r"^(?P<h>[0-9]{2}):(?P<m>[0-9]{2})$")


def schedule_expression(schedule_time: str) -> str:
    """EventBridge Scheduler cron for weekdays at the configured local time (ING-02).

    Only ``09:00`` and ``09:30`` are accepted; the time zone is set separately
    (``ScheduleExpressionTimezone`` ``America/New_York``) so DST never shifts it.
    """
    if schedule_time not in SCHEDULE_TIMES:
        raise ConfigError([ConfigProblem("/ingest/schedule_time", f"must be one of {list(SCHEDULE_TIMES)}, got {schedule_time!r}", "ING-02")])
    m = _TIME_RE.match(schedule_time)
    assert m is not None
    return f"cron({int(m.group('m'))} {int(m.group('h'))} ? * MON-FRI *)"
