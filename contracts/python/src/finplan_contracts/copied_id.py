"""Copied-``$id`` detector for consumer repositories (``finplan-conformance copied-id``; CS-01).

Consumers depend on a published contract package version and must not vendor, copy
or hand-edit its schema definitions. The detector walks a consumer tree and fails on:

* ``copied-id``: any file that *declares* a schema ``$id`` in the contract namespace
  (``https://contracts.finplan.invalid/...``), whether in a JSON/YAML schema file or
  embedded in source code (``"$id": "..."`` / ``$id: ...``);
* ``copied-content``: a JSON or YAML document whose content equals a package schema
  once ``$id``/``$schema`` and annotations are removed (a copy with its ``$id`` renamed).
  YAML is read with a safe loader that keeps timestamps as text; documents that still hold
  non-JSON values, and files that do not parse, are skipped (they cannot be a copied schema), so
  planning metadata such as ``openspec`` ``.openspec.yaml`` files (``created: 2026-10-07``) never
  crash the scan.

Referencing a contract schema (``$ref`` to a contract ``$id``, or loading it from the
installed package) is the intended use and is not reported. Installed dependencies
(``node_modules``, virtualenvs, ``site-packages``) and build outputs are skipped, since
the pinned package itself legitimately contains the schemas.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from .leak_scan import DEFAULT_EXCLUDE_DIRS, iter_files, read_text_file
from .schemas import ID_BASE, SchemaStore

EXCLUDE_DIRS = DEFAULT_EXCLUDE_DIRS | {"site-packages", ".eggs", ".nox", "cdk.out"}
TEXT_SUFFIXES = frozenset({".json", ".yaml", ".yml", ".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx", ".md", ".txt", ".toml", ".cfg", ".ini", ".go", ".java", ".kt", ".rs", ".rb", ".sh"})
ANNOTATION_KEYS = frozenset({"$id", "$schema", "$comment", "title", "description", "examples"})

_DECL = re.compile(r"""["']?\$id["']?\s*[:=]\s*["']?(?P<id>""" + re.escape(ID_BASE) + r"""[^"'\s,}]*)""")


@dataclass(frozen=True)
class CopiedSchema:
    rule: str
    path: str
    line: int
    schema_id: str

    def __str__(self) -> str:
        what = "declares contract $id" if self.rule == "copied-id" else "copies the content of contract schema"
        return f"[CS-01] {self.path}:{self.line}: {what} {self.schema_id}; depend on the pinned contract package instead"


def _strip(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in sorted(node.items()) if k not in ANNOTATION_KEYS and not k.startswith("x-finplan-")}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def fingerprint(schema: Any) -> str:
    return hashlib.sha256(json.dumps(_strip(schema), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _json_compatible(node: Any) -> bool:
    """Whether ``node`` holds only JSON values (a YAML document may hold dates, sets, binary ...)."""
    if node is None or isinstance(node, (str, bool, int, float)):
        return True
    if isinstance(node, list):
        return all(_json_compatible(v) for v in node)
    if isinstance(node, dict):
        return all(isinstance(k, str) and _json_compatible(v) for k, v in node.items())
    return False


class _JsonYamlLoader(yaml.SafeLoader):
    """Safe YAML loader that keeps timestamps as strings (``created: 2026-10-07`` stays text), so
    YAML documents compare like the JSON a schema would be written in."""


_JsonYamlLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def package_fingerprints(store: SchemaStore | None = None) -> dict[str, str]:
    """fingerprint -> ``$id`` for every schema in the (installed) contract package."""
    store = store or SchemaStore()
    out: dict[str, str] = {}
    for info in store:
        stripped = _strip(info.schema)
        # Trivial schemas ({"type": "object"}) would match unrelated documents; require structure.
        if isinstance(stripped, dict) and len(json.dumps(stripped)) > 40:
            out[fingerprint(info.schema)] = info.id
    return out


def _parse(path: Path, text: str) -> list[Any]:
    """JSON-compatible documents of a JSON or YAML file; anything else (unparsable files, YAML
    holding non-JSON values) is skipped, because it cannot be a copied JSON Schema."""
    try:
        if path.suffix == ".json":
            docs = [json.loads(text)]
        elif path.suffix in (".yaml", ".yml"):
            docs = [d for d in yaml.load_all(text, Loader=_JsonYamlLoader) if d is not None]  # noqa: S506 - SafeLoader subclass
        else:
            return []
    except (json.JSONDecodeError, yaml.YAMLError, ValueError, RecursionError):
        return []
    return [d for d in docs if _json_compatible(d)]


def scan_tree(root: str | Path, store: SchemaStore | None = None, exclude_dirs: Iterable[str] = EXCLUDE_DIRS, exclude_globs: Iterable[str] = ()) -> list[CopiedSchema]:
    root = Path(root)
    prints = package_fingerprints(store)
    found: list[CopiedSchema] = []
    for path in iter_files(root, exclude_dirs, exclude_globs):
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        text = read_text_file(path)
        if text is None:
            continue
        rel = path.relative_to(root).as_posix() if root.is_dir() else str(path)
        declared = False
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in _DECL.finditer(line):
                found.append(CopiedSchema("copied-id", rel, lineno, m.group("id")))
                declared = True
        if declared:
            continue
        for doc in _parse(path, text):
            if isinstance(doc, dict):
                try:
                    sid = prints.get(fingerprint(doc))
                except (TypeError, ValueError):  # pragma: no cover - _parse keeps JSON values only
                    continue
                if sid:
                    found.append(CopiedSchema("copied-content", rel, 1, sid))
    return found


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="finplan-conformance copied-id", description="Detect contract schemas copied into a consumer repository (CS-01).")
    ap.add_argument("paths", nargs="*", default=["."], help="consumer repository roots to scan (default: .)")
    ap.add_argument("--root", help="contracts root to compare against (default: the installed package data)")
    ap.add_argument("--exclude", action="append", default=[], help="glob relative to the scanned root to skip (repeatable)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    store = SchemaStore(args.root)
    found: list[CopiedSchema] = []
    for p in args.paths:
        found.extend(scan_tree(p, store, exclude_globs=args.exclude))
    if args.json:
        print(json.dumps({"ok": not found, "copies": [asdict(c) for c in found]}, indent=2, sort_keys=True))
    else:
        for c in found:
            print(c)
        print(f"{'PASS' if not found else 'FAIL'}: copied-$id check, {len(found)} copies")
    return 0 if not found else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
