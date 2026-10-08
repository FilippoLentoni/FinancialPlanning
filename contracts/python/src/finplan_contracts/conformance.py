"""Contract conformance suite (``finplan-conformance conformance``).

Producer mode (the contract package build, CS-02/CS-09/CS-10):

* every fixture under ``fixtures/<schema>/valid/`` validates (schema + semantic
  checks) and every fixture under ``invalid/`` fails, with the error code recorded
  for it in ``conformance/cases.yaml`` when one is recorded;
* inventory (CS-02): every required schema from the coverage list exists, every
  published tool has a request and a response schema, every schema has at least
  one valid and one invalid fixture, and no fixture directory is orphaned;
* fixture hygiene (CS-09, flag part): every fixture carries ``"synthetic": true``
  at the top level or is listed in ``fixtures/README.md`` as an exception.

Consumer mode runs the same suite against the contract data bundled in the
installed package (the version a consumer pinned), and can additionally validate
the consumer's own documents with ``--documents DIR --schema NAME``.

Built-in checks added to both modes:

* fixture hygiene, credential/identifier part (CS-09): every fixture passes the
  identifier/secret leak scan (:mod:`.leak_scan`);
* domain neutrality (DOM-01): no core envelope schema carries a term from a registered
  domain adapter's deny-list (:mod:`.neutrality`).

Consumer mode additionally (CS-01, CS-10):

* ``--repo DIR`` runs the copied-``$id`` detector (:mod:`.copied_id`) over the
  consumer repository, so a vendored or hand-edited contract schema fails the build;
* ``--expect-version X`` fails when the installed package is not the pinned version.

Extension point: functions appended to :data:`EXTRA_FIXTURE_CHECKS` receive
``(root, fixture_paths)`` and return a list of :class:`Problem`. Built-in checks are
modules imported lazily; a missing module only disables its own check (reported as a
problem, so the suite never passes silently without it).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .schemas import SchemaStore, bundled_root, contracts_root
from .validate import validate

#: Coverage list of the contract-schemas spec ("Contract package coverage"), by schema name.
REQUIRED_SCHEMAS: dict[str, str] = {
    "identifiers": "identifiers",
    "input-snapshot": "input snapshot metadata",
    "plan": "plan",
    "plan-version": "plan version",
    "publication": "publication",
    "execution": "execution",
    "configuration": "configuration",
    "job-submission": "job submission",
    "job-status": "job status",
    "job-result": "job result",
    "artifact-ref": "trusted artifact reference",
    "error": "error envelope",
    "error-codes": "registered error codes",
    "capability": "capability description",
    "release-manifest": "release manifest",
    "domain-envelope": "domain envelope",
    "staged-output-manifest": "staged-output manifest",
    "production-strategy": "production-strategy document (1.1.0)",
    "excel-plan-template": "Excel plan template",
    "import-report": "import report",
    "tool-catalog": "tool catalog",
    "caller": "on-behalf-of caller block",
    "cost-estimate": "job cost-estimate block",
    "budget-allocation": "budget allocation",
    "idempotency": "idempotency fragment",
    "concurrency": "concurrency fragment",
}

#: Tools published by FinanceLambdasTool (its design D1); each needs a request/response pair.
PUBLISHED_TOOLS: tuple[str, ...] = (
    "describe_capabilities",
    "query_market_data",
    "get_plan",
    "get_plan_version",
    "list_plan_versions",
    "get_job_status",
    "get_experiment_result",
    "refresh_market_data",
    "submit_experiment",
    "create_override_version",
    "validate_plan_version",
    "publish_plan_version",
    # 1.1.0: the FinanceLambdasTool production-strategy tool: get, set, clear
    "production_strategy",
)

RESERVED_FIXTURE_DIRS = {"vectors"}
EXCEPTIONS_START = "<!-- synthetic-exceptions:start -->"
EXCEPTIONS_END = "<!-- synthetic-exceptions:end -->"


@dataclass(frozen=True)
class Problem:
    check: str
    subject: str
    message: str

    def __str__(self) -> str:
        return f"[{self.check}] {self.subject}: {self.message}"


@dataclass
class Report:
    root: Path
    mode: str
    version: str
    fixtures_checked: int = 0
    schemas_checked: int = 0
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "mode": self.mode,
            "contract_version": self.version,
            "schemas_checked": self.schemas_checked,
            "fixtures_checked": self.fixtures_checked,
            "ok": self.ok,
            "problems": [p.__dict__ for p in self.problems],
        }


ExtraCheck = Callable[[Path, list[Path]], list[Problem]]
EXTRA_FIXTURE_CHECKS: list[ExtraCheck] = []


# --------------------------------------------------------------------- loading
def load_cases(root: Path) -> dict[str, dict[str, Any]]:
    path = root / "conformance" / "cases.yaml"
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {c["fixture"]: c for c in data.get("cases", [])}


def synthetic_exceptions(root: Path) -> set[str]:
    """Fixture paths (relative to ``fixtures/``) exempt from the synthetic flag."""
    readme = root / "fixtures" / "README.md"
    if not readme.is_file():
        return set()
    text = readme.read_text(encoding="utf-8")
    if EXCEPTIONS_START not in text:
        return set()
    block = text.split(EXCEPTIONS_START, 1)[1].split(EXCEPTIONS_END, 1)[0]
    return set(re.findall(r"^\s*-\s*`([^`]+)`", block, flags=re.M))


def fixture_files(root: Path) -> list[Path]:
    fx = root / "fixtures"
    return sorted(p for p in fx.rglob("*.json")) if fx.is_dir() else []


# ---------------------------------------------------------------------- checks
def check_inventory(store: SchemaStore) -> list[Problem]:
    """CS-02: required schemas exist and every schema has valid and invalid fixtures."""
    problems: list[Problem] = []
    for name, label in REQUIRED_SCHEMAS.items():
        if name not in store:
            problems.append(Problem("CS-02", name, f"required schema missing ({label})"))
    for tool in PUBLISHED_TOOLS:
        kebab = tool.replace("_", "-")
        for part in ("request", "response"):
            if f"tools/{kebab}-{part}" not in store:
                problems.append(Problem("CS-02", f"tools/{kebab}-{part}", f"tool '{tool}' has no {part} schema"))
    for info in store:
        d = store.fixtures_dir(info.name)
        for outcome in ("valid", "invalid"):
            if not any((d / outcome).glob("*.json")):
                problems.append(Problem("CS-02", info.name, f"no {outcome} fixture under fixtures/{info.name}/{outcome}/"))
    fx = store.root / "fixtures"
    names = set(store.names())
    for outcome_dir in sorted(fx.rglob("*")) if fx.is_dir() else []:
        if outcome_dir.is_dir() and outcome_dir.name in ("valid", "invalid"):
            schema_name = outcome_dir.parent.relative_to(fx).as_posix()
            if schema_name not in names and schema_name.split("/")[0] not in RESERVED_FIXTURE_DIRS:
                problems.append(Problem("CS-02", schema_name, "fixture directory has no matching schema"))
    return problems


def check_fixtures(store: SchemaStore, cases: dict[str, dict[str, Any]]) -> tuple[int, list[Problem]]:
    """Valid fixtures validate; invalid fixtures fail (with the recorded code)."""
    problems: list[Problem] = []
    count = 0
    fx = store.root / "fixtures"
    for info in store:
        for outcome in ("valid", "invalid"):
            for path in sorted((store.fixtures_dir(info.name) / outcome).glob("*.json")):
                rel = path.relative_to(fx).as_posix()
                count += 1
                case = cases.get(rel, {})
                try:
                    doc = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    problems.append(Problem("CS-10", rel, f"not valid JSON: {exc.msg}"))
                    continue
                res = validate(doc, info.name, context=case.get("context"), store=store)
                if outcome == "valid" and not res.valid:
                    problems.append(Problem("CS-10", rel, "valid fixture fails: " + "; ".join(i.message for i in res.issues[:3])))
                elif outcome == "invalid":
                    if res.valid:
                        problems.append(Problem("CS-10", rel, "invalid fixture validates"))
                    elif case.get("code") and res.code != case["code"]:
                        problems.append(Problem("CS-10", rel, f"expected error code {case['code']}, got {res.code}"))
                if case and case.get("schema") not in (None, info.name):
                    problems.append(Problem("CS-10", rel, f"cases.yaml names schema {case.get('schema')}, fixture lives under {info.name}"))
                if case and case.get("expect") not in (None, outcome):
                    problems.append(Problem("CS-10", rel, f"cases.yaml expects {case.get('expect')}, fixture lives under {outcome}/"))
    for rel in sorted(set(cases) - {p.relative_to(fx).as_posix() for p in fixture_files(store.root)}):
        problems.append(Problem("CS-10", rel, "cases.yaml lists a fixture that does not exist"))
    return count, problems


def check_synthetic_flags(root: Path) -> list[Problem]:
    """CS-09 (flag part): every fixture is flagged synthetic or listed as an exception."""
    problems: list[Problem] = []
    exceptions = synthetic_exceptions(root)
    fx = root / "fixtures"
    for path in fixture_files(root):
        rel = path.relative_to(fx).as_posix()
        if rel in exceptions:
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            problems.append(Problem("CS-09", rel, "not valid JSON"))
            continue
        if not (isinstance(doc, dict) and doc.get("synthetic") is True):
            problems.append(Problem("CS-09", rel, 'fixture lacks "synthetic": true and is not listed in fixtures/README.md'))
    for rel in sorted(exceptions):
        if not (fx / rel).is_file():
            problems.append(Problem("CS-09", rel, "listed as a synthetic exception but does not exist"))
    return problems


def run_suite(root: Path | str | None = None, mode: str = "producer") -> Report:
    store = SchemaStore(root)
    report = Report(root=store.root, mode=mode, version=store.version, schemas_checked=len(store))
    cases = load_cases(store.root)
    report.problems.extend(check_inventory(store))
    count, problems = check_fixtures(store, cases)
    report.fixtures_checked = count
    report.problems.extend(problems)
    report.problems.extend(check_synthetic_flags(store.root))
    files = fixture_files(store.root)
    builtin = builtin_fixture_checks(report)
    for extra in [*builtin, *(c for c in EXTRA_FIXTURE_CHECKS if c not in builtin)]:
        report.problems.extend(extra(store.root, files))
    report.problems.extend(check_neutrality(store.root))
    return report


def builtin_fixture_checks(report: Report | None = None) -> list[ExtraCheck]:
    """Fixture checks shipped with the package (the CS-09 leak scan and the ENV-20 GPU approval rule)."""
    try:
        from .leak_scan import fixture_check
    except ImportError as exc:  # pragma: no cover - only in a broken install
        if report is not None:
            report.problems.append(Problem("CS-09", "leak_scan", f"fixture leak scan unavailable: {exc}"))
        return []
    from .gpu_rule import fixture_check as gpu_fixture_check

    return [fixture_check, gpu_fixture_check]


def check_neutrality(root: Path) -> list[Problem]:
    """DOM-01: core envelope schemas carry no domain adapter terms."""
    try:
        from .neutrality import check_root
    except ImportError as exc:  # pragma: no cover - only in a broken install
        return [Problem("DOM-01", "neutrality", f"domain-neutrality check unavailable: {exc}")]
    if not (root / "domains" / "registry.json").is_file():
        return [Problem("DOM-01", "domains/registry.json", "domain registry missing; cannot run the domain-neutrality check")]
    return [Problem("DOM-01", f"{p.schema}#{p.pointer}", f"{p.kind} '{p.name}' is domain-specific (deny term '{p.term}')") for p in check_root(root)]


def check_consumer_repo(repo: Path, store: SchemaStore) -> list[Problem]:
    """CS-01: the consumer repository holds no copied contract schema."""
    from .copied_id import scan_tree

    return [Problem("CS-01", f"{c.path}:{c.line}", f"{'declares contract $id' if c.rule == 'copied-id' else 'copies the content of'} {c.schema_id}; depend on the pinned package instead") for c in scan_tree(repo, store)]


def run_producer(root: Path | str | None = None) -> Report:
    return run_suite(contracts_root(root), mode="producer")


def run_consumer(
    root: Path | str | None = None,
    documents: Path | None = None,
    schema: str | None = None,
    repo: Path | None = None,
    expect_version: str | None = None,
) -> Report:
    """Run the suite against the installed package's bundled data (or ``root``).

    With ``documents`` and ``schema``, also validate every ``*.json`` under
    ``documents`` (the consumer's own requests or fixture-handling outputs). With
    ``repo``, run the copied-``$id`` detector over the consumer repository (CS-01).
    With ``expect_version``, fail unless the package data is that pinned version.
    """
    data_root = Path(root) if root is not None else (bundled_root() or contracts_root())
    report = run_suite(data_root, mode="consumer")
    if expect_version is not None and report.version != expect_version.strip():
        report.problems.append(Problem("CS-10", "VERSION", f"installed contract package is {report.version}, the build pins {expect_version.strip()}"))
    if repo is not None:
        report.problems.extend(check_consumer_repo(Path(repo), SchemaStore(data_root)))
    if documents is not None:
        if not schema:
            report.problems.append(Problem("CS-10", str(documents), "--documents requires --schema"))
            return report
        store = SchemaStore(data_root)
        for path in sorted(Path(documents).rglob("*.json")):
            report.fixtures_checked += 1
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                report.problems.append(Problem("CS-10", str(path), f"not valid JSON: {exc.msg}"))
                continue
            res = validate(doc, schema, store=store)
            if not res.valid:
                report.problems.append(Problem("CS-10", str(path), "; ".join(i.message for i in res.issues[:3])))
    return report


# ------------------------------------------------------------------------ CLI
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="finplan-conformance conformance", description="Run the contract conformance suite.")
    ap.add_argument("--mode", choices=("producer", "consumer"), default="producer")
    ap.add_argument("--root", help="contracts root (producer default: source checkout; consumer default: installed package data)")
    ap.add_argument("--documents", type=Path, help="consumer mode: directory of the consumer's own JSON documents to validate")
    ap.add_argument("--schema", help="consumer mode: schema for --documents")
    ap.add_argument("--repo", type=Path, help="consumer mode: consumer repository root to check for copied contract schemas (CS-01)")
    ap.add_argument("--expect-version", help="consumer mode: the pinned contract version; fail if the installed package differs")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)
    if args.mode == "producer":
        if args.repo or args.expect_version or args.documents:
            ap.error("--repo, --expect-version and --documents are consumer-mode options")
        report = run_producer(args.root)
    else:
        report = run_consumer(args.root, args.documents, args.schema, args.repo, args.expect_version)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        for p in report.problems:
            print(p)
        status = "PASS" if report.ok else "FAIL"
        print(f"{status}: {report.mode} conformance, contracts {report.version}, {report.schemas_checked} schemas, {report.fixtures_checked} fixtures, {len(report.problems)} problems")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
