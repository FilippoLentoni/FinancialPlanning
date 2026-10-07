"""Schema compatibility gate (``finplan-conformance compat``; CS-03, OWN-07).

Each build diffs the contract schemas against the previous published version (a
contracts root directory, or the raw schema tarball built by :mod:`.digests`) and
classifies every difference:

``additive`` (allowed in a minor release)
    a new optional property, a new schema, a new ``$defs`` entry, a new value in an
    enum the schema marks open (``x-finplan-open-enum``; ``x-finplan-known-values``) or
    in a registry enum (``x-finplan-registry``, and the registered error-code list,
    where "adding a code is a minor release");
``annotation`` (allowed in a patch release)
    ``title``, ``description``, ``$comment``, ``examples``, ``deprecated`` and the
    descriptions of registered error codes;
``breaking`` (needs a major release)
    everything else: removed schemas, properties or definitions, newly required or
    no-longer-required fields, type, format, pattern, bound, ``$ref``, ``const`` or
    default changes, values added to closed enums or removed from any enum, changed
    ``additionalProperties``/composition, changed semantic checks
    (``x-finplan-checks``) and other ``x-finplan-*`` keywords. The rule is a whitelist:
    a change not listed as additive or annotation is breaking.

The gate also validates every ``valid`` fixture of the previous release against the
new schemas ("a document produced under 1.2.0 validates with 1.3.0"); a failure is a
breaking change.

Version rules (semver of ``contracts/VERSION``):

* the new version must be greater than the previous one; an unchanged version with any
  change fails (published versions are immutable);
* a patch release may contain annotation changes only, a minor release additive and
  annotation changes, a major release anything, subject to the next two rules;
* a breaking change must not edit a schema of an already-served ``$id`` major in place:
  it is published under a new ``$id`` major (``core/v2/...``) while the previous major
  stays served. The one exception is an ``$id`` major equal to the new package major
  that was pre-released under a lower package major (``v1`` schemas shipped in 0.x
  becoming 1.0.0). Removing a previous major's schema is allowed only in a major release;
* a major release with breaking changes needs a migration record
  (``migrations/v<major>.yaml`` in the new root, or ``--migration``) naming
  ``from_major``, ``to_major``, the ``consumers`` that must move and the
  ``removal_criterion`` of the previous major;
* a schema ``$id`` major must not exceed ``max(1, package major)``.

Pre-1.0 versions follow the same rules (a breaking diff in 0.x needs a major bump)
unless ``--allow-zero-major-breaking`` is given.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .schemas import ID_BASE, NON_SCHEMA_DIRS, SchemaStore, contracts_root

BREAKING, ADDITIVE, ANNOTATION = "breaking", "additive", "annotation"
ANNOTATION_KEYS = frozenset({"title", "description", "$comment", "examples", "deprecated"})
NAMED_SUBSCHEMA_MAPS = ("properties", "$defs", "definitions", "patternProperties", "dependentSchemas")
SINGLE_SUBSCHEMAS = ("items", "additionalProperties", "unevaluatedProperties", "unevaluatedItems", "contains", "propertyNames", "not", "if", "then", "else")
LIST_SUBSCHEMAS = ("allOf", "anyOf", "oneOf", "prefixItems")
_SEMVER = re.compile(r"^(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)(?:-(?P<pre>[0-9A-Za-z.-]+))?$")


@dataclass(frozen=True)
class Change:
    schema: str
    pointer: str
    kind: str
    message: str

    def __str__(self) -> str:
        return f"{self.kind:10s} {self.schema}#{self.pointer}: {self.message}"


@dataclass
class CompatReport:
    old_version: str | None
    new_version: str
    bump: str
    changes: list[Change] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def required_bump(self) -> str:
        kinds = {c.kind for c in self.changes}
        return "major" if BREAKING in kinds else "minor" if ADDITIVE in kinds else "patch" if ANNOTATION in kinds else "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_version": self.old_version,
            "new_version": self.new_version,
            "bump": self.bump,
            "required_bump": self.required_bump,
            "ok": self.ok,
            "changes": [asdict(c) for c in self.changes],
            "problems": self.problems,
            "notes": self.notes,
        }


# ------------------------------------------------------------------ versions
def parse_version(v: str) -> tuple[int, int, int, str | None]:
    m = _SEMVER.match(v.strip())
    if not m:
        raise ValueError(f"not a semantic version: {v!r}")
    return int(m["major"]), int(m["minor"]), int(m["patch"]), m["pre"]


def classify_bump(old: str, new: str) -> str:
    o, n = parse_version(old), parse_version(new)
    if n[:3] == o[:3]:
        if n[3] == o[3]:
            return "none"
        # Pre-release ordering: a release (no pre) is greater than any of its pre-releases.
        if o[3] is not None and (n[3] is None or n[3] > o[3]):
            return "patch"
        return "downgrade"
    if n[:3] < o[:3]:
        return "downgrade"
    if n[0] > o[0]:
        return "major"
    if n[1] > o[1]:
        return "minor"
    return "patch"


# ---------------------------------------------------------------------- diff
def _ptr(parts: list[str]) -> str:
    return "/" + "/".join(p.replace("~", "~0").replace("/", "~1") for p in parts) if parts else "/"


def _is_open_enum(node: dict[str, Any]) -> bool:
    return node.get("x-finplan-open-enum") is True or node.get("x-finplan-registry") is True


def diff_schema(name: str, old: Any, new: Any, root_new: dict[str, Any] | None = None) -> list[Change]:
    """All differences between two versions of one schema (same ``$id``)."""
    root_new = root_new if root_new is not None else (new if isinstance(new, dict) else {})
    changes: list[Change] = []

    def add(parts: list[str], kind: str, msg: str) -> None:
        changes.append(Change(name, _ptr(parts), kind, msg))

    def registry_enum(parts: list[str]) -> bool:
        # The registered error-code list: "adding a code is a minor release".
        return "x-finplan-error-codes" in root_new and parts == ["$defs", "code"]

    def walk(o: Any, n: Any, parts: list[str]) -> None:
        if type(o) is not type(n):
            add(parts, BREAKING, "subschema changed form")
            return
        if not isinstance(o, dict):
            if o != n:
                add(parts, BREAKING, "subschema changed")
            return
        required_new = set(n.get("required", []) if isinstance(n.get("required"), list) else [])
        required_old = set(o.get("required", []) if isinstance(o.get("required"), list) else [])
        for key in sorted(set(o) | set(n)):
            here = parts + [key]
            if key not in n:
                ov = o[key]
                if key in ANNOTATION_KEYS:
                    add(here, ANNOTATION, f"annotation '{key}' removed")
                elif key == "required":
                    for r in sorted(required_old):
                        add(here, BREAKING, f"required field '{r}' made optional")
                else:
                    add(here, BREAKING, f"keyword '{key}' removed" + (f" ({len(ov)} entries)" if isinstance(ov, (list, dict)) else ""))
                continue
            if key not in o:
                if key in ANNOTATION_KEYS:
                    add(here, ANNOTATION, f"annotation '{key}' added")
                elif key == "required":
                    for r in sorted(required_new):
                        add(here, BREAKING, f"field '{r}' newly required")
                elif key == "properties":
                    for p in sorted(n[key]):
                        add(here + [p], BREAKING if p in required_new else ADDITIVE, f"property '{p}' added" + (" as required" if p in required_new else " (optional)"))
                elif key in ("$defs", "definitions"):
                    for d in sorted(n[key]):
                        add(here + [d], ADDITIVE, f"definition '{d}' added")
                elif key == "x-finplan-known-values" and _is_open_enum(n):
                    add(here, ADDITIVE, "known values registered for an open enum")
                elif key == "deprecated":
                    add(here, ANNOTATION, "marked deprecated")
                else:
                    add(here, BREAKING, f"keyword '{key}' added")
                continue
            ov, nv = o[key], n[key]
            if key in ANNOTATION_KEYS:
                if ov != nv:
                    add(here, ANNOTATION, f"annotation '{key}' changed")
            elif key == "properties" and isinstance(ov, dict) and isinstance(nv, dict):
                for p in sorted(set(ov) | set(nv)):
                    if p not in nv:
                        add(here + [p], BREAKING, f"property '{p}' removed")
                    elif p not in ov:
                        add(here + [p], BREAKING if p in required_new else ADDITIVE, f"property '{p}' added" + (" as required" if p in required_new else " (optional)"))
                    else:
                        walk(ov[p], nv[p], here + [p])
            elif key in NAMED_SUBSCHEMA_MAPS and isinstance(ov, dict) and isinstance(nv, dict):
                for d in sorted(set(ov) | set(nv)):
                    if d not in nv:
                        add(here + [d], BREAKING, f"'{key}' entry '{d}' removed")
                    elif d not in ov:
                        add(here + [d], ADDITIVE if key in ("$defs", "definitions") else BREAKING, f"'{key}' entry '{d}' added")
                    else:
                        walk(ov[d], nv[d], here + [d])
            elif key == "required":
                for r in sorted(required_new - required_old):
                    add(here, BREAKING, f"field '{r}' newly required")
                for r in sorted(required_old - required_new):
                    add(here, BREAKING, f"required field '{r}' made optional")
            elif key == "enum" and isinstance(ov, list) and isinstance(nv, list):
                removed = [v for v in ov if v not in nv]
                added = [v for v in nv if v not in ov]
                for v in removed:
                    add(here, BREAKING, f"enum value {json.dumps(v)} removed")
                for v in added:
                    if _is_open_enum(n) or registry_enum(parts):
                        add(here, ADDITIVE, f"enum value {json.dumps(v)} added to an open/registry enum")
                    else:
                        add(here, BREAKING, f"enum value {json.dumps(v)} added to a closed enum")
            elif key == "x-finplan-known-values" and isinstance(ov, list) and isinstance(nv, list):
                for v in ov:
                    if v not in nv:
                        add(here, BREAKING, f"known value {json.dumps(v)} removed")
                for v in nv:
                    if v not in ov:
                        add(here, ADDITIVE if _is_open_enum(n) else BREAKING, f"known value {json.dumps(v)} registered" + ("" if _is_open_enum(n) else " on a closed enum"))
            elif key == "x-finplan-error-codes" and isinstance(ov, dict) and isinstance(nv, dict):
                for code in sorted(set(ov) | set(nv)):
                    if code not in nv:
                        add(here + [code], BREAKING, f"error code {code} removed")
                    elif code not in ov:
                        add(here + [code], ADDITIVE, f"error code {code} registered")
                    else:
                        oe, ne = ov[code], nv[code]
                        for f in sorted(set(oe) | set(ne)):
                            if oe.get(f) != ne.get(f):
                                add(here + [code, f], ANNOTATION if f == "description" else BREAKING, f"error code {code} '{f}' changed")
            elif key in SINGLE_SUBSCHEMAS:
                walk(ov, nv, here)
            elif key in LIST_SUBSCHEMAS and isinstance(ov, list) and isinstance(nv, list):
                if len(ov) != len(nv):
                    add(here, BREAKING, f"'{key}' changed from {len(ov)} to {len(nv)} subschemas")
                else:
                    for i, (a, b) in enumerate(zip(ov, nv, strict=True)):
                        walk(a, b, here + [str(i)])
            elif ov != nv:
                add(here, BREAKING, f"keyword '{key}' changed")

    walk(old, new, [])
    return changes


# --------------------------------------------------------------------- roots
@dataclass(frozen=True)
class LoadedSchema:
    id: str
    name: str  # "<namespace>/v<major>/<name>"
    short: str  # "<name>" as used for fixture directories
    major: int
    schema: dict[str, Any]


def load_schemas(root: Path) -> dict[str, LoadedSchema]:
    """All schemas under ``<ns>/v<major>/`` keyed by ``$id``; several majors may coexist."""
    out: dict[str, LoadedSchema] = {}
    for ns_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name not in NON_SCHEMA_DIRS and not p.name.startswith(".")):
        for major_dir in sorted(p for p in ns_dir.iterdir() if p.is_dir() and re.fullmatch(r"v[0-9]+", p.name)):
            for path in sorted(major_dir.rglob("*.json")):
                short = path.relative_to(major_dir).with_suffix("").as_posix()
                schema = json.loads(path.read_text(encoding="utf-8"))
                expected = f"{ID_BASE}{ns_dir.name}/{major_dir.name}/{short}.json"
                if schema.get("$id") != expected:
                    raise ValueError(f"{path}: $id {schema.get('$id')!r} does not match its location (expected {expected!r})")
                out[expected] = LoadedSchema(expected, f"{ns_dir.name}/{major_dir.name}/{short}", short, int(major_dir.name[1:]), schema)
    return out


def read_version(root: Path) -> str:
    return (root / "VERSION").read_text(encoding="utf-8").strip()


def _materialize(path: Path, tmp: Path) -> Path:
    """Return a contracts root for ``path`` (a directory or a raw schema tarball)."""
    if path.is_dir():
        return path
    with tarfile.open(path) as tf:
        try:
            tf.extractall(tmp, filter="data")
        except TypeError:  # pragma: no cover - Python without extraction filters
            tf.extractall(tmp)  # noqa: S202
    for candidate in [tmp, *sorted(p for p in tmp.iterdir() if p.is_dir())]:
        if (candidate / "VERSION").is_file() and (candidate / "core").is_dir():
            return candidate
    raise FileNotFoundError(f"{path}: no contracts root (VERSION + core/) inside the archive")


def load_migration(new_root: Path, major: int, explicit: Path | None) -> tuple[dict[str, Any] | None, Path]:
    path = explicit or new_root / "migrations" / f"v{major}.yaml"
    if not path.is_file():
        return None, path
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}, path


def _fixture_upgrade_changes(old_root: Path, new_root: Path, report: CompatReport) -> list[tuple[Change, str]]:
    """Every valid fixture of the previous release must validate under the new schemas.

    Returns (change, schema $id) pairs. Needs the package validator, whose store holds one
    major per schema name; when the new root serves two majors of a name side by side the
    step is skipped with a note (the structural diff still runs).
    """
    from .conformance import load_cases
    from .validate import validate

    fx = old_root / "fixtures"
    if not fx.is_dir():
        return []
    try:
        new_store = SchemaStore(new_root)
    except ValueError as exc:
        report.notes.append(f"fixture upgrade check skipped: {exc}")
        return []
    cases = load_cases(old_root)
    out: list[tuple[Change, str]] = []
    for path in sorted(fx.rglob("valid/*.json")):
        name = path.parent.parent.relative_to(fx).as_posix()
        if name not in new_store:
            continue  # removed schemas are reported by the schema diff
        rel = path.relative_to(fx).as_posix()
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        res = validate(doc, name, context=cases.get(rel, {}).get("context"), store=new_store)
        if not res.valid:
            info = new_store.get(name)
            msg = res.issues[0].message if res.issues else "invalid"
            out.append((Change(f"{info.namespace}/v{info.major}/{info.name}", "/", BREAKING, f"previous release's valid fixture {rel} no longer validates ({msg})"), info.id))
    return out


def compare_roots(
    old: Path | str | None,
    new: Path | str | None = None,
    migration: Path | None = None,
    allow_zero_major_breaking: bool = False,
    check_fixtures: bool = True,
) -> CompatReport:
    new_root = contracts_root(new)
    new_schemas = load_schemas(new_root)
    new_version = read_version(new_root)
    nv = parse_version(new_version)
    if old is None:
        report = CompatReport(None, new_version, "initial")
        report.notes.append("no previous published version: nothing to compare (first release)")
        _check_id_majors(new_schemas, nv[0], report)
        return report
    with tempfile.TemporaryDirectory(prefix="finplan-compat-") as tmp:
        old_root = _materialize(Path(old), Path(tmp))
        old_schemas = load_schemas(old_root)
        old_version = read_version(old_root)
        ov = parse_version(old_version)
        report = CompatReport(old_version, new_version, classify_bump(old_version, new_version))

        breaking_in_place: dict[str, int] = {}
        for sid in sorted(set(old_schemas) | set(new_schemas)):
            if sid not in new_schemas:
                report.changes.append(Change(old_schemas[sid].name, "/", BREAKING, f"schema {sid} removed"))
            elif sid not in old_schemas:
                report.changes.append(Change(new_schemas[sid].name, "/", ADDITIVE, f"schema {sid} added"))
            else:
                changes = diff_schema(new_schemas[sid].name, old_schemas[sid].schema, new_schemas[sid].schema)
                report.changes.extend(changes)
                if any(c.kind == BREAKING for c in changes):
                    breaking_in_place[sid] = new_schemas[sid].major
        if check_fixtures:
            for change, sid in _fixture_upgrade_changes(old_root, new_root, report):
                report.changes.append(change)
                breaking_in_place.setdefault(sid, new_schemas[sid].major)

    _check_id_majors(new_schemas, nv[0], report)
    bump, need = report.bump, report.required_bump
    zero_exempt = allow_zero_major_breaking and ov[0] == 0 and nv[0] == 0
    if bump == "downgrade":
        report.problems.append(f"version {new_version} is lower than the previous published version {old_version}")
    elif bump == "none" and need != "none":
        report.problems.append(f"version {new_version} is already published and immutable; schema changes need a new version (at least {need})")
    elif bump == "patch" and need == "minor":
        report.problems.append(f"additive changes under a patch bump ({old_version} -> {new_version}); additive changes need a minor bump")
    elif bump in ("minor", "patch") and need == "major" and not zero_exempt:
        report.problems.append(f"breaking changes under a non-major bump ({old_version} -> {new_version}); release a new major version")
    if bump == "major" and need == "major":
        for sid, major in sorted(breaking_in_place.items()):
            if not (major == nv[0] and major > ov[0]):
                report.problems.append(
                    f"breaking change to {sid} edits served $id major v{major} in place; publish it as a new $id major (v{nv[0]}) and keep v{major} served until consumers migrate"
                )
        record, path = load_migration(new_root, nv[0], migration)
        if record is None:
            report.problems.append(f"major release with breaking changes needs a migration record at {path} (from_major, to_major, consumers, removal_criterion)")
        else:
            missing = [k for k in ("from_major", "to_major", "consumers", "removal_criterion") if record.get(k) in (None, "", [])]
            if missing:
                report.problems.append(f"migration record {path} is missing {', '.join(missing)}")
            elif record.get("to_major") != nv[0] or record.get("from_major") != ov[0]:
                report.problems.append(f"migration record {path} names {record.get('from_major')} -> {record.get('to_major')}, expected {ov[0]} -> {nv[0]}")
            elif not isinstance(record.get("consumers"), list):
                report.problems.append(f"migration record {path}: consumers must be a list")
    return report


def _check_id_majors(schemas: dict[str, LoadedSchema], package_major: int, report: CompatReport) -> None:
    limit = max(1, package_major)
    for info in schemas.values():
        if info.major > limit:
            report.problems.append(f"{info.id}: $id major v{info.major} exceeds the package major {package_major}")


# ----------------------------------------------------------------------- CLI
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="finplan-conformance compat", description="Schema compatibility gate against the previous published version (CS-03, OWN-07).")
    ap.add_argument("--old", type=Path, help="previous published version: contracts root directory or raw schema tarball")
    ap.add_argument("--new", type=Path, help="new contracts root (default: discovered)")
    ap.add_argument("--first-release", action="store_true", help="no previous version exists (required when --old is omitted)")
    ap.add_argument("--migration", type=Path, help="migration record for a breaking major release (default: <new>/migrations/v<major>.yaml)")
    ap.add_argument("--allow-zero-major-breaking", action="store_true", help="allow breaking changes between 0.x versions (semver initial development)")
    ap.add_argument("--no-fixture-upgrade", action="store_true", help="skip validating the previous release's valid fixtures with the new schemas")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.old is None and not args.first_release:
        ap.error("--old is required (or --first-release when no version was published yet)")
    report = compare_roots(args.old, args.new, args.migration, args.allow_zero_major_breaking, not args.no_fixture_upgrade)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        for c in report.changes:
            print(c)
        for n in report.notes:
            print(f"note: {n}")
        for p in report.problems:
            print(f"[CS-03] {p}")
        print(f"{'PASS' if report.ok else 'FAIL'}: compatibility gate {report.old_version or '(none)'} -> {report.new_version} ({report.bump} bump, changes need {report.required_bump})")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
