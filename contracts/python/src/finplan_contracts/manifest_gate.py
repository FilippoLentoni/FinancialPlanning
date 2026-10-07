"""End-to-end compatibility gate over the release manifests of one environment
(design D3/D4; spec cross-repo-ownership "End-to-end validation requires compatible
releases in the same environment", OWN-08; task 8.4).

Input: the release manifests (``/finplan/<env>/<repo>/release/manifest`` values)
of the participating repositories in one environment, by default all four
repositories. The gate passes only when:

* every participating repository has exactly one manifest, and each manifest
  validates against ``core/v1/release-manifest.json``;
* every manifest is for the gated environment, and all are in the same region;
* the releases satisfy each other's compatibility ranges: for every ordered pair
  (consumer A, producer B) with B serving at least one contract major
  (non-empty ``served_contract_majors``), the major of A's pinned
  ``contract_version`` is one of B's ``served_contract_majors``.

A failing gate reports each incompatibility (consumer, pinned major, producer,
served majors) and blocks the consumer's promotion and the end-to-end suite.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import ssm as _ssm

__all__ = ["GateResult", "Incompatibility", "gate", "load_manifests", "contract_major", "main"]


@dataclass(frozen=True)
class Incompatibility:
    consumer: str
    pinned_contract_version: str
    pinned_major: int
    producer: str
    served_contract_majors: tuple[int, ...]

    def __str__(self) -> str:
        served = ", ".join(map(str, self.served_contract_majors)) or "none"
        return (
            f"{self.consumer} pins contract {self.pinned_contract_version} (major {self.pinned_major}) but "
            f"{self.producer} serves only major(s) {served}"
        )


@dataclass
class GateResult:
    environment: str
    compatible: bool
    problems: list[str] = field(default_factory=list)
    incompatibilities: list[Incompatibility] = field(default_factory=list)
    releases: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.compatible

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "compatible": self.compatible,
            "releases": self.releases,
            "problems": self.problems,
            "incompatibilities": [i.__dict__ | {"served_contract_majors": list(i.served_contract_majors)} for i in self.incompatibilities],
        }


_SEMVER_MAJOR = re.compile(r"^(0|[1-9][0-9]*)\.")


def contract_major(version: Any) -> int | None:
    if not isinstance(version, str):
        return None
    m = _SEMVER_MAJOR.match(version)
    return int(m.group(1)) if m else None


def gate(
    manifests: Iterable[Mapping[str, Any]],
    environment: str,
    participants: Sequence[str] | None = None,
    *,
    validate_schema: bool = True,
) -> GateResult:
    """Run the end-to-end compatibility gate (OWN-08)."""
    participants = tuple(participants or _ssm.REPOS)
    res = GateResult(environment=environment, compatible=False)
    if environment not in _ssm.ENVIRONMENTS:
        res.problems.append(f"unknown environment {environment!r}")
        return res
    by_repo: dict[str, Mapping[str, Any]] = {}
    for m in manifests:
        if not isinstance(m, Mapping):
            res.problems.append("manifest is not a JSON object")
            continue
        repo = m.get("repo")
        if validate_schema:
            from .validate import validate

            vr = validate(dict(m), "release-manifest")
            for issue in vr.issues:
                res.problems.append(f"{repo or '<unknown repo>'}: manifest invalid: {issue.message}")
        if m.get("environment") != environment:
            res.problems.append(f"{repo}: manifest is for environment {m.get('environment')!r}, not {environment!r}")
            continue
        if repo in by_repo:
            res.problems.append(f"{repo}: more than one manifest in {environment}")
            continue
        if repo not in participants:
            continue
        by_repo[str(repo)] = m
    for repo in participants:
        if repo not in by_repo:
            res.problems.append(f"{repo}: no release recorded in {environment} (dependency missing)")
    regions = {m.get("region") for m in by_repo.values()}
    if len(regions) > 1:
        res.problems.append(f"releases are in different regions: {', '.join(sorted(map(str, regions)))}")
    res.releases = {r: str(m.get("release_id")) for r, m in sorted(by_repo.items())}
    for consumer, cm in sorted(by_repo.items()):
        major = contract_major(cm.get("contract_version"))
        if major is None:
            res.problems.append(f"{consumer}: contract_version {cm.get('contract_version')!r} is not semver")
            continue
        if major == 0 and environment != "beta":
            # design (Risks): 0.x contracts stay in beta; gamma and prod require >= 1.0.0
            res.problems.append(f"{consumer}: contract_version {cm.get('contract_version')} is a 0.x pre-release, allowed only in beta; {environment} requires >= 1.0.0")
        for producer, pm in sorted(by_repo.items()):
            if producer == consumer:
                continue
            served = tuple(sorted(x for x in (pm.get("served_contract_majors") or []) if isinstance(x, int)))
            if not served:
                continue  # consumer-only release: serves no contract surface
            if major not in served:
                res.incompatibilities.append(Incompatibility(consumer, str(cm.get("contract_version")), major, producer, served))
    res.compatible = not res.problems and not res.incompatibilities
    return res


def load_manifests(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    """Load manifests from files and directories (``*.json`` inside)."""
    out = []
    for p in map(Path, paths):
        files = sorted(p.glob("*.json")) if p.is_dir() else [p]
        for f in files:
            out.append(json.loads(f.read_text(encoding="utf-8")))
    return out


def load_from_ssm(ssm_client: Any, environment: str, repos: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Read ``/finplan/<env>/<repo>/release/manifest`` with an injected (read-only) SSM client."""
    out = []
    for repo in repos or _ssm.REPOS:
        name = _ssm.build(environment, repo, "release", "manifest")
        try:
            resp = ssm_client.get_parameter(Name=name)
        except Exception as exc:  # noqa: BLE001 - client-specific not-found errors
            if "ParameterNotFound" in type(exc).__name__ or "ParameterNotFound" in str(exc):
                continue
            raise
        out.append(json.loads(resp["Parameter"]["Value"]))
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="finplan-conformance manifest-gate", description="End-to-end compatibility gate over the release manifests of one environment (OWN-08).")
    ap.add_argument("--env", required=True, choices=_ssm.ENVIRONMENTS)
    ap.add_argument("manifests", nargs="*", help="manifest JSON files or directories")
    ap.add_argument("--participants", help="comma-separated repositories (default: all four)")
    ap.add_argument("--from-ssm", action="store_true", help="read the manifests from SSM (requires boto3 and credentials; read-only)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    participants = [p.strip() for p in args.participants.split(",")] if args.participants else None
    if args.from_ssm:
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError:
            print("--from-ssm needs the optional 'aws' extra (boto3)", file=sys.stderr)
            return 2
        manifests = load_from_ssm(boto3.client("ssm"), args.env, participants)
    else:
        manifests = load_manifests(args.manifests)
    res = gate(manifests, args.env, participants)
    if args.json:
        print(json.dumps(res.to_dict(), indent=2))
    else:
        for p in res.problems:
            print(f"PROBLEM: {p}", file=sys.stderr)
        for i in res.incompatibilities:
            print(f"INCOMPATIBLE: {i}", file=sys.stderr)
        print(f"{'COMPATIBLE' if res.compatible else 'BLOCKED'}: {args.env} " + ", ".join(f"{r}={rid}" for r, rid in res.releases.items()))
    return 0 if res.compatible else 1
