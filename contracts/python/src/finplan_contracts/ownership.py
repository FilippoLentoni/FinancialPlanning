"""Ownership check of a synthesized CloudFormation template (``finplan-conformance ownership-check``).

OWN-01 (cross-repo-ownership, "Single owning repository per resource") and OWN-09 (CDK-generated helpers, below):

* every resource in the template must map to a row of ``ownership/matrix.yaml`` by its
  CloudFormation type plus its ``logical-role`` (cost-allocation tag, design D10);
  a resource with no matching row fails ("Resource missing from the matrix");
* the matched row's owner must be the repository whose template is checked; a resource
  owned by another repository fails and the report names the resource and its owner
  ("Resource declared by a non-owner");
* rows that no repository may declare (``owner: external`` or ``iac_declared: false``,
  for example the reused CodeConnection or the pre-existing Jev API key secret) fail
  whenever a template declares them.

ENV-16 (environment-promotion, "Account-level shared resources"): a resource tagged
``environment`` ``shared`` must map to a ``scope: shared`` row (account-level tooling,
budget, registry, image repositories, pre-existing credential secrets), so environment
data (snapshots, plans, run outputs) in a ``shared`` resource fails; a resource of a
``shared`` row must not carry another environment tag. When a repository owns two rows with the
resource's logical role (its account-level pipeline row and its per-environment pipeline-role row,
for example ``deploy-role``), the ``environment`` tag selects the row (:func:`select_row`).

How a resource's logical role is found, in order: its ``logical-role`` tag (``Tags`` as a
list of ``Key``/``Value`` or as a map), ``Metadata`` key ``logical-role`` (or
``finplan:logical-role``) for types that cannot be tagged, and otherwise the single
logical role of the template resources it references with ``Ref``/``Fn::GetAtt``
(a bucket policy inherits its bucket's role). The inherited role still has to list the
resource's type in its row.

CDK-generated helper resources (task 1.5, cross-repo-ownership "CDK-generated helper resources
are attributed, never ignored") are never skipped. A resource without its own ``logical-role``
is a helper when either:

* it matches an entry of the reviewed ``cdk_generated_helpers.allow_list`` in the matrix
  (resource type plus a logical-id or ``aws:cdk:path`` pattern, with a recorded reason and
  review), for example ``AWS::CDK::Metadata`` or a CDK custom-resource provider function and
  role; it is attributed to the repository whose template is checked; or
* its type is in ``cdk_generated_helpers.parent_attributed_types`` (for example an
  ``AWS::IAM::Policy`` attached to a role, an ``AWS::Lambda::Permission``, a
  ``Custom::LogRetention``) and every template resource it references with ``Ref``/``Fn::GetAtt``
  is owned by the checked repository (a matched matrix row or another attributed helper). It
  is attributed to its parent construct's row.

A helper with no parent in the template, a parent owned by another repository, or no
allow-list entry fails OWN-01. Every attribution is listed in the report (``helpers``).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from .schemas import contracts_root

ROLE_TAG = "logical-role"
OWNER_TAG = "owner-repo"
ENV_TAG = "environment"
CDK_PATH = "aws:cdk:path"
SHARED = "shared"
EXTERNAL = "external"


@dataclass(frozen=True)
class OwnershipProblem:
    rule: str
    logical_id: str
    resource_type: str
    message: str
    owner: str | None = None

    def __str__(self) -> str:
        return f"[{self.rule}] {self.logical_id} ({self.resource_type}): {self.message}"


@dataclass
class OwnershipReport:
    repo: str
    template: str
    checked: int = 0
    matched: dict[str, str] = field(default_factory=dict)  # logical id -> matrix row id
    helpers: dict[str, str] = field(default_factory=dict)  # logical id -> "allow-list:<entry>" | "parent:<row ids> via <logical ids>"
    problems: list[OwnershipProblem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "template": self.template,
            "resources_checked": self.checked,
            "matched": dict(sorted(self.matched.items())),
            "helpers": dict(sorted(self.helpers.items())),
            "ok": self.ok,
            "problems": [asdict(p) for p in self.problems],
        }


# ------------------------------------------------------------------- matrix
class Matrix:
    """The ownership matrix (``contracts/ownership/matrix.yaml``)."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.rows: list[dict[str, Any]] = list(data.get("resources", []))
        self.repositories: list[str] = list(data.get("repositories", []))
        self.by_id = {r["id"]: r for r in self.rows}
        helpers = data.get("cdk_generated_helpers") or {}
        self.parent_attributed_types: frozenset[str] = frozenset(helpers.get("parent_attributed_types") or [])
        self.helper_allow_list: list[dict[str, Any]] = list(helpers.get("allow_list") or [])
        problems = validate_helper_config(helpers)
        if problems:
            raise ValueError("invalid cdk_generated_helpers in the ownership matrix: " + "; ".join(problems))

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Matrix":
        p = Path(path) if path else contracts_root() / "ownership" / "matrix.yaml"
        return cls(yaml.safe_load(p.read_text(encoding="utf-8")))

    def owner_of(self, row_id: str) -> str:
        return self.by_id[row_id]["owner"]

    def rows_for_role(self, role: str) -> list[dict[str, Any]]:
        return [r for r in self.rows if role in (r.get("logical_roles") or [])]

    def rows_for_type(self, rtype: str) -> list[dict[str, Any]]:
        return [r for r in self.rows if rtype in (r.get("resource_types") or [])]

    @staticmethod
    def declarable(row: dict[str, Any]) -> bool:
        return row.get("owner") != EXTERNAL and row.get("iac_declared", True) is not False

    def allow_list_entry(self, logical_id: str, resource: dict[str, Any]) -> dict[str, Any] | None:
        """The reviewed allow-list entry a CDK-generated helper matches, if any."""
        rtype = resource.get("Type")
        cdk_path = (resource.get("Metadata") or {}).get(CDK_PATH)
        for entry in self.helper_allow_list:
            if rtype not in entry["resource_types"]:
                continue
            lid_pat = entry.get("logical_id_pattern")
            path_pat = entry.get("cdk_path_pattern")
            if lid_pat and re.fullmatch(lid_pat, logical_id):
                return entry
            if path_pat and isinstance(cdk_path, str) and re.search(path_pat, cdk_path):
                return entry
        return None

    def secret_names(self) -> set[str]:
        """Names of pre-existing secrets (rows with ``iac_declared: false``) that IaC must not declare."""
        names: set[str] = set()
        for r in self.rows:
            if r.get("iac_declared") is False:
                refs = r.get("reference")
                for ref in refs if isinstance(refs, list) else [refs or ""]:
                    if ref.startswith("secret name "):
                        names.add(ref[len("secret name ") :].strip())
        return names


def validate_helper_config(helpers: dict[str, Any]) -> list[str]:
    """Each allow-list entry must be explicit and reviewed: id, resource types, a pattern, reason, review record."""
    problems: list[str] = []
    seen: set[str] = set()
    for i, entry in enumerate(helpers.get("allow_list") or []):
        eid = entry.get("id") if isinstance(entry, dict) else None
        label = eid or f"#{i}"
        if not isinstance(entry, dict) or not eid:
            problems.append(f"allow-list entry {label} has no id")
            continue
        if eid in seen:
            problems.append(f"allow-list entry {eid} is duplicated")
        seen.add(eid)
        if not entry.get("resource_types"):
            problems.append(f"allow-list entry {eid} lists no resource_types")
        if not (entry.get("logical_id_pattern") or entry.get("cdk_path_pattern")):
            problems.append(f"allow-list entry {eid} has neither logical_id_pattern nor cdk_path_pattern")
        for key in ("logical_id_pattern", "cdk_path_pattern"):
            if entry.get(key):
                try:
                    re.compile(entry[key])
                except re.error as exc:
                    problems.append(f"allow-list entry {eid} has an invalid {key}: {exc}")
        for key in ("reason", "reviewed"):
            if not str(entry.get(key) or "").strip():
                problems.append(f"allow-list entry {eid} has no {key}")
    return problems


# ----------------------------------------------------------------- template
def load_template(path: str | Path) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    if str(path).endswith((".yaml", ".yml")):
        return yaml.load(text, Loader=_cfn_loader())  # noqa: S506 - SafeLoader subclass
    return json.loads(text)


def _cfn_loader() -> type[yaml.SafeLoader]:
    class CfnLoader(yaml.SafeLoader):
        pass

    def construct(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Any:
        name = "Ref" if suffix == "Ref" else f"Fn::{suffix}"
        if isinstance(node, yaml.ScalarNode):
            value: Any = loader.construct_scalar(node)
            if suffix == "GetAtt" and isinstance(value, str):
                value = value.split(".", 1)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node, deep=True)
        else:
            value = loader.construct_mapping(node, deep=True)  # type: ignore[arg-type]
        return {name: value}

    CfnLoader.add_multi_constructor("!", construct)
    return CfnLoader


def tags_of(resource: dict[str, Any]) -> dict[str, str]:
    props = resource.get("Properties") or {}
    tags = props.get("Tags")
    if tags is None:
        tags = props.get("tags")
    out: dict[str, str] = {}
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, dict) and isinstance(t.get("Key"), str) and isinstance(t.get("Value"), str):
                out[t["Key"]] = t["Value"]
    elif isinstance(tags, dict):
        out = {k: v for k, v in tags.items() if isinstance(v, str)}
    return out


def _metadata_role(resource: dict[str, Any]) -> str | None:
    meta = resource.get("Metadata") or {}
    for key in (ROLE_TAG, f"finplan:{ROLE_TAG}"):
        if isinstance(meta.get(key), str):
            return meta[key]
    finplan = meta.get("finplan")
    if isinstance(finplan, dict) and isinstance(finplan.get(ROLE_TAG), str):
        return finplan[ROLE_TAG]
    return None


def _references(node: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "Ref" and isinstance(v, str):
                refs.add(v)
            elif k == "Fn::GetAtt":
                if isinstance(v, list) and v and isinstance(v[0], str):
                    refs.add(v[0])
                elif isinstance(v, str):
                    refs.add(v.split(".", 1)[0])
            else:
                refs |= _references(v)
    elif isinstance(node, list):
        for item in node:
            refs |= _references(item)
    return refs


def _direct_role(resource: dict[str, Any]) -> str | None:
    return tags_of(resource).get(ROLE_TAG) or _metadata_role(resource)


def resolve_roles(resources: dict[str, Any]) -> dict[str, tuple[str | None, str]]:
    """logical id -> (logical role or None, how it was found: tag|metadata|inherited:<id>|none)."""
    out: dict[str, tuple[str | None, str]] = {}
    for lid, res in resources.items():
        if not isinstance(res, dict):
            continue
        tag_role = tags_of(res).get(ROLE_TAG)
        if tag_role:
            out[lid] = (tag_role, "tag")
            continue
        meta_role = _metadata_role(res)
        if meta_role:
            out[lid] = (meta_role, "metadata")
            continue
        out[lid] = (None, "none")
    for lid, res in resources.items():
        if not isinstance(res, dict) or out.get(lid, (None, ""))[0]:
            continue
        parents = sorted(r for r in _references(res.get("Properties") or {}) if r in resources and r != lid)
        roles = {out[p][0] for p in parents if out.get(p, (None, ""))[0]}
        if len(roles) == 1:
            role = roles.pop()
            src = next(p for p in parents if out.get(p, (None, ""))[0] == role)
            out[lid] = (role, f"inherited:{src}")
    return out


def select_row(rows: list[dict[str, Any]], environment: str | None) -> dict[str, Any]:
    """Pick the row of a repository's candidate rows for a resource's ``environment`` tag.

    A repository has at most two rows per logical role: its account-level pipeline row
    (``scope: shared``) and its per-environment pipeline-role row (``scope: environment``).
    ``environment`` ``shared`` selects the shared row, ``beta|gamma|prod`` the environment row;
    without a tag (or with a single candidate) the first row is used. The ENV-16 rules then
    apply to the selected row, so a wrongly tagged resource still fails.
    """
    if len(rows) > 1 and environment is not None:
        wanted = SHARED if environment == SHARED else "environment"
        for r in rows:
            if r.get("scope") == wanted:
                return r
    return rows[0]


def infer_repo(resources: dict[str, Any], repositories: Iterable[str]) -> str | None:
    owners = {tags_of(r).get(OWNER_TAG) for r in resources.values() if isinstance(r, dict)} - {None}
    owners &= set(repositories)
    return owners.pop() if len(owners) == 1 else None


# -------------------------------------------------------------------- check
def check_template(template: dict[str, Any], repo: str | None, matrix: Matrix | None = None, name: str = "<template>") -> OwnershipReport:
    matrix = matrix or Matrix.load()
    resources = template.get("Resources") or {}
    repo = repo or infer_repo(resources, matrix.repositories)
    report = OwnershipReport(repo=repo or "<unknown>", template=name)
    if repo is None:
        report.problems.append(OwnershipProblem("OWN-01", "<template>", "-", "cannot tell which repository owns this template: pass --repo or tag resources with owner-repo"))
        return report
    if repo not in matrix.repositories:
        report.problems.append(OwnershipProblem("OWN-01", "<template>", "-", f"unknown repository {repo!r}; known: {', '.join(matrix.repositories)}"))
        return report

    secret_names = matrix.secret_names()
    roles = resolve_roles(resources)
    failed: dict[str, str | None] = {}  # logical id -> owner named in its problem
    deferred: list[str] = []  # parent-attributed helpers, resolved after every other resource
    for lid in sorted(resources):
        res = resources[lid]
        if not isinstance(res, dict):
            continue
        rtype = str(res.get("Type", ""))
        report.checked += 1
        tags = tags_of(res)
        props = res.get("Properties") or {}

        def fail(rule: str, message: str, owner: str | None = None, lid: str = lid, rtype: str = rtype) -> None:
            report.problems.append(OwnershipProblem(rule, lid, rtype, message, owner))
            failed.setdefault(lid, owner)

        # A pre-existing secret referenced by name must never be declared (ENV-16, D1).
        if rtype == "AWS::SecretsManager::Secret" and isinstance(props.get("Name"), str) and props["Name"] in secret_names:
            row = next(r for r in matrix.rows if r.get("iac_declared") is False and props["Name"] in str(r.get("reference")))
            fail("OWN-01", f"declares the pre-existing secret of matrix row '{row['id']}' (owner {row['owner']}); it is referenced by name only and never declared in IaC", row["owner"])
            continue

        owner_tag = tags.get(OWNER_TAG)
        if owner_tag and owner_tag != repo:
            fail("OWN-01", f"tagged {OWNER_TAG}={owner_tag} but declared in the {repo} template", owner_tag)

        role, how = roles.get(lid, (None, "none"))
        if how not in ("tag", "metadata"):
            # CDK-generated helper: a reviewed allow-list entry, or attributed to its parent construct.
            entry = matrix.allow_list_entry(lid, res)
            if entry is not None:
                report.helpers[lid] = f"allow-list:{entry['id']}"
                continue
            if rtype in matrix.parent_attributed_types:
                deferred.append(lid)
                continue
        if role is None:
            typed_rows = matrix.rows_for_type(rtype)
            if typed_rows and not any(matrix.declarable(r) for r in typed_rows):
                r = typed_rows[0]
                fail("OWN-01", f"declares a resource of matrix row '{r['id']}', which is {r['owner']} and never declared in IaC", r["owner"])
            else:
                fail("OWN-01", f"no '{ROLE_TAG}' tag or metadata, so the resource has no entry in the ownership matrix; record an owner first")
            continue

        candidates = matrix.rows_for_role(role)
        if not candidates:
            fail("OWN-01", f"{ROLE_TAG} '{role}' has no entry in the ownership matrix; record an owner first")
            continue
        typed = [r for r in candidates if rtype in (r.get("resource_types") or [])]
        if not typed:
            ids = ", ".join(r["id"] for r in candidates)
            fail("OWN-01", f"type {rtype} is not listed for {ROLE_TAG} '{role}' (matrix row {ids}); the resource has no entry in the ownership matrix")
            continue
        own = [r for r in typed if r["owner"] == repo]
        if not own:
            row = typed[0]
            if not matrix.declarable(row):
                fail("OWN-01", f"declares matrix row '{row['id']}', which is {row['owner']} and never declared in IaC", row["owner"])
            else:
                owners = sorted({r["owner"] for r in typed})
                fail("OWN-01", f"matrix row '{row['id']}' ({role}) is owned by {', '.join(owners)}, not {repo}; only the owner may declare it", owners[0])
            continue
        row = select_row(own, tags.get(ENV_TAG))
        if not matrix.declarable(row):
            fail("OWN-01", f"declares matrix row '{row['id']}', which is never declared in IaC", row["owner"])
            continue
        report.matched[lid] = row["id"]

        env = tags.get(ENV_TAG)
        if env == SHARED and row.get("scope") != SHARED:
            fail("ENV-16", f"tagged {ENV_TAG}=shared but matrix row '{row['id']}' holds per-environment data (scope {row.get('scope')}); environment data must not live in a shared resource", row["owner"])
        elif row.get("scope") == SHARED and env not in (None, SHARED):
            fail("ENV-16", f"matrix row '{row['id']}' is an account-level shared resource but is tagged {ENV_TAG}={env}", row["owner"])
        elif row.get("scope") == SHARED and row.get("environment_data") is not False:
            fail("ENV-16", f"matrix row '{row['id']}' is shared but does not declare environment_data: false", row["owner"])

    _attribute_helpers(resources, deferred, report, failed, repo)
    return report


def _attribute_helpers(resources: dict[str, Any], deferred: list[str], report: OwnershipReport, failed: dict[str, str | None], repo: str) -> None:
    """Attribute parent-attributed CDK helpers to their parent construct's row, or fail them (OWN-01)."""
    pending = {lid: sorted(r for r in _references(resources[lid].get("Properties") or {}) if r in resources and r != lid) for lid in deferred}

    def fail(lid: str, message: str, owner: str | None = None) -> None:
        report.problems.append(OwnershipProblem("OWN-01", lid, str(resources[lid].get("Type", "")), message, owner))
        failed.setdefault(lid, owner)

    progress = True
    while pending and progress:
        progress = False
        for lid in sorted(pending):
            parents = pending[lid]
            if any(p in pending for p in parents):
                continue  # wait for helper parents to be attributed first
            progress = True
            del pending[lid]
            foreign = [p for p in parents if p in failed and p not in report.matched]
            if foreign:
                owners = sorted({o for p in foreign if (o := failed[p])})
                fail(lid, f"CDK-generated helper of {', '.join(foreign)}, which is not an attributed {repo} resource" + (f" (owner {', '.join(owners)})" if owners else "") + "; a helper belongs to the owner of its parent construct", owners[0] if owners else None)
                continue
            rows = sorted({report.matched[p] for p in parents if p in report.matched})
            if rows:
                via = [p for p in parents if p in report.matched]
                report.helpers[lid] = f"parent:{','.join(rows)} via {','.join(via)}"
                continue
            helper_parents = [p for p in parents if p in report.helpers]
            if helper_parents:
                report.helpers[lid] = f"parent:{','.join(helper_parents)} via helper"
                continue
            fail(lid, "CDK-generated helper with no parent construct in this template and no entry in the reviewed cdk_generated_helpers.allow_list; attribute it to an owned parent or record a reviewed allow-list entry")
    for lid in sorted(pending):
        fail(lid, "CDK-generated helper whose parents are only other unattributed helpers (reference cycle); attribute it to an owned parent or record a reviewed allow-list entry")


def check_file(path: str | Path, repo: str | None, matrix: Matrix | None = None) -> OwnershipReport:
    return check_template(load_template(path), repo, matrix, name=str(path))


# ------------------------------------------------------------------------ CLI
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="finplan-conformance ownership-check", description="Check synthesized CloudFormation templates against the ownership matrix (OWN-01, ENV-16).")
    ap.add_argument("templates", nargs="+", type=Path, help="synthesized template files (JSON or YAML), e.g. cdk.out/*.template.json")
    ap.add_argument("--repo", help="repository whose templates these are (default: from the owner-repo tags)")
    ap.add_argument("--matrix", type=Path, help="ownership matrix (default: the package's ownership/matrix.yaml)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    matrix = Matrix.load(args.matrix)
    reports = [check_file(t, args.repo, matrix) for t in args.templates]
    if args.json:
        print(json.dumps([r.to_dict() for r in reports], indent=2, sort_keys=True))
    else:
        for r in reports:
            for p in r.problems:
                print(f"{r.template}: {p}")
            for lid, how in sorted(r.helpers.items()):
                print(f"{r.template}: CDK helper {lid} attributed to {how}")
            print(f"{'PASS' if r.ok else 'FAIL'}: ownership check, {r.template} ({r.repo}), {r.checked} resources ({len(r.helpers)} CDK helpers attributed), {len(r.problems)} problems")
    return 0 if all(r.ok for r in reports) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
