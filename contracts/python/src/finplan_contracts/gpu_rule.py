"""GPU approval rule for job-status documents (design D11; spec environment-promotion
"GPU runs require explicit user approval", ENV-20; task 13.5).

Rule: a GPU job (``compute_class`` ``gpu``, or a cost estimate with
``budget_category`` ``gpu``) without a recorded user approval (an ``approval``
block with ``approved_by`` and ``approved_at``) cannot leave ``awaiting_approval``.
That covers its current ``state`` and every entry of its ``transitions`` history.
When an approval is recorded, it must not be later than the first transition out
of ``awaiting_approval``, and when it records ``approved_estimate_usd`` the cost
estimate's ``estimated_usd_upper_bound`` must not exceed it (the approval is of a
cost estimate).

Wiring (the conformance runner is owned by another module, so this module only
offers hooks):

* :func:`check_job_status` - the rule on one document, returning
  :class:`finplan_contracts.validate.ValidationIssue` objects;
* :func:`register_semantic_check` - registers the rule as the semantic check
  ``gpu_approval_required`` with :func:`finplan_contracts.validate.register_check`;
  add ``"gpu_approval_required"`` to ``x-finplan-checks`` of
  ``core/v1/job-status.json`` (and of ``tools/get-job-status-response``) and
  import this module from ``validate.py`` so the name resolves;
* :func:`fixture_check` - an :data:`finplan_contracts.conformance.EXTRA_FIXTURE_CHECKS`
  hook: valid job-status fixtures must satisfy the rule; :func:`register_conformance_hook`
  appends it.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "CHECK_NAME",
    "JOB_STATUS_SCHEMAS",
    "is_gpu_job",
    "has_recorded_approval",
    "violations",
    "check_job_status",
    "register_semantic_check",
    "fixture_check",
    "register_conformance_hook",
    "may_transition",
]

CHECK_NAME = "gpu_approval_required"
AWAITING = "awaiting_approval"
#: Fixture directories (schema names) holding job-status documents.
JOB_STATUS_SCHEMAS = ("job-status", "tools/get-job-status-response")


def is_gpu_job(doc: Any) -> bool:
    if not isinstance(doc, dict):
        return False
    if doc.get("compute_class") == "gpu":
        return True
    est = doc.get("cost_estimate")
    return isinstance(est, dict) and est.get("budget_category") == "gpu"


def has_recorded_approval(doc: Any) -> bool:
    appr = doc.get("approval") if isinstance(doc, dict) else None
    return isinstance(appr, dict) and bool(appr.get("approved_by")) and bool(appr.get("approved_at"))


def _ts(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def violations(doc: Any) -> list[tuple[str, str]]:
    """``(json_pointer, message)`` pairs for every breach of the GPU approval rule."""
    if not is_gpu_job(doc):
        return []
    out: list[tuple[str, str]] = []
    transitions = doc.get("transitions") if isinstance(doc.get("transitions"), list) else []
    if not has_recorded_approval(doc):
        if doc.get("state") not in (AWAITING, None):
            out.append(("/state", f"GPU job without a recorded user approval cannot leave {AWAITING} (state is {doc.get('state')!r})"))
        for i, t in enumerate(transitions):
            if isinstance(t, dict) and t.get("state") not in (AWAITING, None):
                out.append((f"/transitions/{i}/state", f"GPU job without a recorded user approval cannot transition to {t.get('state')!r}"))
        return out
    appr = doc["approval"]
    approved_at = _ts(appr.get("approved_at"))
    first_exit = next((t for t in transitions if isinstance(t, dict) and t.get("state") not in (AWAITING, None)), None)
    if approved_at and first_exit is not None:
        left_at = _ts(first_exit.get("at"))
        if left_at and left_at < approved_at:
            out.append(("/approval/approved_at", "GPU job left awaiting_approval before the recorded approval"))
    est = doc.get("cost_estimate")
    approved_usd = appr.get("approved_estimate_usd")
    if isinstance(est, dict) and isinstance(approved_usd, (int, float)) and isinstance(est.get("estimated_usd_upper_bound"), (int, float)):
        if est["estimated_usd_upper_bound"] > approved_usd + 1e-9:
            out.append(("/approval/approved_estimate_usd", "the cost estimate exceeds the approved estimate; a new approval is required"))
    return out


def may_transition(doc: Any, new_state: str) -> bool:
    """Whether a job may move to ``new_state`` under the rule (for job controllers)."""
    if not is_gpu_job(doc) or new_state == AWAITING:
        return True
    return has_recorded_approval(doc)


def check_job_status(doc: Any) -> list[Any]:
    """The rule as :class:`finplan_contracts.validate.ValidationIssue` objects (code VALIDATION_FAILED)."""
    from .validate import ValidationIssue

    return [ValidationIssue(code="VALIDATION_FAILED", pointer=p, message=f"{p}: {m}", field=p.rsplit("/", 1)[-1], keyword="x-finplan-gpu-approval") for p, m in violations(doc)]


def register_semantic_check() -> None:
    """Register :func:`check_job_status` as the semantic check ``gpu_approval_required``."""
    from .validate import CHECKS, register_check

    if CHECK_NAME not in CHECKS:
        register_check(CHECK_NAME)(lambda ctx: check_job_status(ctx.document))


def _fixture_schema(root: Path, path: Path) -> tuple[str, str] | None:
    rel = path.relative_to(root / "fixtures").as_posix()
    for name in JOB_STATUS_SCHEMAS:
        for kind in ("valid", "invalid"):
            if rel.startswith(f"{name}/{kind}/"):
                return name, kind
    return None


def fixture_check(root: Path, fixture_paths: Iterable[Path]) -> list[Any]:
    """Conformance hook: every *valid* job-status fixture satisfies the GPU approval rule.

    Invalid fixtures are not required to break this rule (they may fail for other
    reasons), so they are not checked here.
    """
    from .conformance import Problem

    problems = []
    root = Path(root)
    for p in fixture_paths:
        p = Path(p)
        try:
            where = _fixture_schema(root, p)
        except ValueError:
            continue
        if not where or where[1] != "valid":
            continue
        doc = json.loads(p.read_text(encoding="utf-8"))
        for pointer, msg in violations(doc):
            problems.append(Problem("gpu-approval", str(p.relative_to(root)), f"{pointer}: {msg}"))
    return problems


def register_conformance_hook() -> None:
    """Append :func:`fixture_check` to ``conformance.EXTRA_FIXTURE_CHECKS`` (idempotent)."""
    from . import conformance

    if fixture_check not in conformance.EXTRA_FIXTURE_CHECKS:
        conformance.EXTRA_FIXTURE_CHECKS.append(fixture_check)
