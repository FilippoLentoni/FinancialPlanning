"""``finplan-conformance`` entry point.

Each subcommand maps to ``finplan_contracts.<module>.main(argv: list[str]) -> int``.
Modules are imported lazily, so a missing or broken module only breaks its own
subcommand.
"""

from __future__ import annotations

import importlib
import sys

from . import __version__

#: subcommand -> module (relative to the package) providing ``main(argv) -> int``.
SUBCOMMANDS: dict[str, tuple[str, str]] = {
    "validate": ("validate", "Validate JSON documents against a contract schema."),
    "conformance": ("conformance", "Run the conformance suite (producer or consumer mode)."),
    "ownership-check": ("ownership", "Check a synthesized CloudFormation template against the ownership matrix."),
    "neutrality": ("neutrality", "Check core schemas for domain-specific terms."),
    "leak-scan": ("leak_scan", "Scan files for account IDs, ARNs, bucket names and secrets."),
    "copied-id": ("copied_id", "Detect copied contract $ids in a consumer repository."),
    "live-perm-scan": ("live_perms", "Scan IAM policies for live trading, payment or wallet permissions."),
    "compat": ("compat", "Schema compatibility gate against the previous release."),
    "digest": ("digests", "Compute SHA-256 digests of build outputs."),
    "manifest-gate": ("manifest_gate", "End-to-end compatibility gate over release manifests."),
    "pipeline-check": ("pipeline_check", "Check a synthesized pipeline template against the pipeline standard."),
    "bootstrap-precheck": ("bootstrap", "Bootstrap pre-checks (mocked or read-only)."),
    "budget": ("budget", "Budget allocation and pre-flight checks."),
    "ssm-path": ("ssm", "Build or check SSM parameter names."),
}


def usage() -> str:
    width = max(map(len, SUBCOMMANDS))
    lines = [f"finplan-conformance {__version__}", "", "usage: finplan-conformance <subcommand> [args...]", "", "subcommands:"]
    lines += [f"  {name.ljust(width)}  {desc}" for name, (_, desc) in SUBCOMMANDS.items()]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(usage())
        return 0 if args else 2
    if args[0] in ("-V", "--version"):
        print(__version__)
        return 0
    sub, rest = args[0], args[1:]
    if sub not in SUBCOMMANDS:
        print(f"unknown subcommand: {sub}\n\n{usage()}", file=sys.stderr)
        return 2
    module_name = SUBCOMMANDS[sub][0]
    try:
        module = importlib.import_module(f"{__package__}.{module_name}")
    except ImportError as exc:
        print(f"subcommand '{sub}' is unavailable: module finplan_contracts.{module_name} could not be imported ({exc})", file=sys.stderr)
        return 2
    entry = getattr(module, "main", None)
    if not callable(entry):
        print(f"subcommand '{sub}' is unavailable: finplan_contracts.{module_name} has no main(argv)", file=sys.stderr)
        return 2
    result = entry(rest)
    return int(result or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
