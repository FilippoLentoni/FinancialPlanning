"""Domain-neutrality check of the core envelope schemas (``finplan-conformance neutrality``; DOM-01).

The core envelope (``core/v<major>/*``, including ``core/v<major>/tools/*``) carries
identifiers, lineage, timestamps, checksums, status and errors only (design D9).
Domain terms belong to the domain adapters. The deny-list is owned by the adapters:
each registered domain in ``domains/registry.json`` lists its terms under
``neutrality_deny_terms`` (the finance adapter's registration entry); the check uses
the union over all registered domains, or an explicit ``--deny-list`` file (a JSON
array, or an object with ``neutrality_deny_terms``).

What is checked, in every core schema and every nested subschema:

* property names (``properties``, ``patternProperties`` keys are skipped as regexes),
  ``required`` and ``dependentRequired`` entries, and ``$defs``/``definitions`` names;
* string ``enum`` and ``const`` values, and ``x-finplan-known-values`` of open enums;
* ``$ref`` targets: a core schema must not reference a domain adapter namespace
  (the payload stays opaque in the envelope).

A name matches a deny term when it equals the term, or when its tokens (split on
``_``, ``-``, ``.`` and camelCase) contain the term's tokens as a contiguous run, so
``ticker``, ``ticker_symbol`` and ``primaryTicker`` all match ``ticker``.
Descriptions and titles are prose and are not checked.

Exempt names: :data:`EXEMPT_NAMES` lists core names fixed by the design whose tokens
overlap a deny term with a different, domain-neutral meaning; registered domains may
add more under ``neutrality_allow_names`` in ``domains/registry.json``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from .schemas import CORE_NAMESPACE, ID_PATTERN, contracts_root

NAME_KEYS = ("properties", "$defs", "definitions")
LIST_NAME_KEYS = ("required",)
SUBSCHEMA_KEYS_DICT = ("properties", "$defs", "definitions", "patternProperties", "dependentSchemas")
SUBSCHEMA_KEYS_SINGLE = ("items", "additionalProperties", "unevaluatedProperties", "unevaluatedItems", "contains", "propertyNames", "not", "if", "then", "else")
SUBSCHEMA_KEYS_LIST = ("allOf", "anyOf", "oneOf", "prefixItems")

#: Core names fixed by the design whose tokens overlap a deny term but are domain-neutral.
EXEMPT_NAMES: dict[str, str] = {
    "remaining_allocation_usd": "design D10 cost-estimate field: the remaining *budget category* allocation in USD, not a portfolio allocation",
}


@dataclass(frozen=True)
class NeutralityProblem:
    schema: str
    pointer: str
    kind: str
    name: str
    term: str

    def __str__(self) -> str:
        return f"[DOM-01] {self.schema}#{self.pointer}: {self.kind} '{self.name}' is domain-specific (deny term '{self.term}')"


def tokens(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return [t for t in re.split(r"[_\-.\s/:]+", spaced.lower()) if t]


def match_term(name: str, terms: Iterable[str], exempt: Iterable[str] = EXEMPT_NAMES) -> str | None:
    if name in set(exempt):
        return None
    toks = tokens(name)
    low = name.lower()
    for term in sorted(set(terms)):
        tterm = tokens(term)
        if low == term.lower():
            return term
        n = len(tterm)
        if n and any(toks[i : i + n] == tterm for i in range(len(toks) - n + 1)):
            return term
    return None


def load_allow_names(root: Path | None = None) -> set[str]:
    reg = json.loads((contracts_root(root) / "domains" / "registry.json").read_text(encoding="utf-8"))
    names = set(EXEMPT_NAMES)
    for d in reg.get("domains", []):
        names |= {str(n) for n in d.get("neutrality_allow_names", [])}
    return names


def load_deny_terms(root: Path | None = None, deny_list: Path | None = None) -> list[str]:
    if deny_list is not None:
        data = json.loads(Path(deny_list).read_text(encoding="utf-8"))
        terms = data.get("neutrality_deny_terms", []) if isinstance(data, dict) else data
        return sorted({str(t) for t in terms})
    reg = json.loads((contracts_root(root) / "domains" / "registry.json").read_text(encoding="utf-8"))
    terms: set[str] = set()
    for d in reg.get("domains", []):
        terms |= {str(t) for t in d.get("neutrality_deny_terms", [])}
    return sorted(terms)


def _ptr(parts: list[str]) -> str:
    return "/" + "/".join(p.replace("~", "~0").replace("/", "~1") for p in parts) if parts else ""


def _walk(node: Any, parts: list[str]) -> Iterator[tuple[list[str], dict[str, Any]]]:
    if not isinstance(node, dict):
        return
    yield parts, node
    for key in SUBSCHEMA_KEYS_DICT:
        sub = node.get(key)
        if isinstance(sub, dict):
            for name, child in sub.items():
                yield from _walk(child, parts + [key, name])
    for key in SUBSCHEMA_KEYS_SINGLE:
        if isinstance(node.get(key), dict):
            yield from _walk(node[key], parts + [key])
    for key in SUBSCHEMA_KEYS_LIST:
        if isinstance(node.get(key), list):
            for i, child in enumerate(node[key]):
                yield from _walk(child, parts + [key, str(i)])


def check_schema(name: str, schema: dict[str, Any], terms: Iterable[str], exempt: Iterable[str] = EXEMPT_NAMES) -> list[NeutralityProblem]:
    terms = list(terms)
    exempt = set(exempt)
    problems: list[NeutralityProblem] = []

    def hit(parts: list[str], kind: str, value: str) -> None:
        term = match_term(value, terms, exempt)
        if term:
            problems.append(NeutralityProblem(name, _ptr(parts), kind, value, term))

    for parts, node in _walk(schema, []):
        for key in NAME_KEYS:
            if isinstance(node.get(key), dict):
                for prop in node[key]:
                    hit(parts + [key, prop], "property" if key == "properties" else "definition", prop)
        for key in LIST_NAME_KEYS:
            if isinstance(node.get(key), list):
                for i, prop in enumerate(node[key]):
                    if isinstance(prop, str):
                        hit(parts + [key, str(i)], "required name", prop)
        dep = node.get("dependentRequired")
        if isinstance(dep, dict):
            for k, vals in dep.items():
                hit(parts + ["dependentRequired", k], "required name", k)
                for i, v in enumerate(vals if isinstance(vals, list) else []):
                    if isinstance(v, str):
                        hit(parts + ["dependentRequired", k, str(i)], "required name", v)
        for key in ("enum", "x-finplan-known-values"):
            if isinstance(node.get(key), list):
                for i, v in enumerate(node[key]):
                    if isinstance(v, str):
                        hit(parts + [key, str(i)], "enum value", v)
        if isinstance(node.get("const"), str):
            hit(parts + ["const"], "const value", node["const"])
        ref = node.get("$ref")
        if isinstance(ref, str):
            m = ID_PATTERN.match(ref.split("#", 1)[0])
            if m and m.group("ns") != CORE_NAMESPACE:
                problems.append(NeutralityProblem(name, _ptr(parts + ["$ref"]), "$ref to domain adapter", ref, m.group("ns")))
    return problems


def core_schemas(root: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    core = root / CORE_NAMESPACE
    for major_dir in sorted(p for p in core.iterdir() if p.is_dir() and re.fullmatch(r"v[0-9]+", p.name)):
        for path in sorted(major_dir.rglob("*.json")):
            yield f"{CORE_NAMESPACE}/{major_dir.name}/{path.relative_to(major_dir).as_posix()}", json.loads(path.read_text(encoding="utf-8"))


def check_root(root: Path | str | None = None, deny_list: Path | None = None, extra_schemas: Iterable[Path] = ()) -> list[NeutralityProblem]:
    r = contracts_root(root)
    terms = load_deny_terms(r, deny_list)
    exempt = load_allow_names(r)
    problems: list[NeutralityProblem] = []
    for name, schema in core_schemas(r):
        problems.extend(check_schema(name, schema, terms, exempt))
    for path in extra_schemas:
        problems.extend(check_schema(str(path), json.loads(Path(path).read_text(encoding="utf-8")), terms, exempt))
    return problems


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="finplan-conformance neutrality", description="Fail when a core envelope schema contains domain-specific terms (DOM-01).")
    ap.add_argument("--root", help="contracts root (default: discovered)")
    ap.add_argument("--deny-list", type=Path, help="deny-list file (default: the registered domain adapters' neutrality_deny_terms)")
    ap.add_argument("--schema", action="append", type=Path, default=[], help="additional envelope schema file to check (repeatable)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    problems = check_root(args.root, args.deny_list, args.schema)
    if args.json:
        print(json.dumps({"ok": not problems, "problems": [asdict(p) for p in problems]}, indent=2, sort_keys=True))
    else:
        for p in problems:
            print(p)
        print(f"{'PASS' if not problems else 'FAIL'}: domain-neutrality check, {len(problems)} problems")
    return 0 if not problems else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
