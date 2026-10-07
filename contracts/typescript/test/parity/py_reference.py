"""Python reference results for the TypeScript validator parity test (CS-10, both languages).

Validates every fixture, plus mutations of every valid fixture (each leaf deleted,
nulled, set to an integer, set to a wrong string / a ``pl_`` identifier / a storage
URI, objects emptied or given an unknown member, arrays emptied or duplicated),
with ``finplan_contracts.validate`` and writes, per document, the outcome, the
error code and the set of ``(code, pointer)`` issues. ``test/parity.test.mjs``
runs the TypeScript validator on the same documents and requires identical results.

All inputs are the synthetic package fixtures; nothing touches the network.

Usage: python -I py_reference.py --root <contracts root> --out <file.json>
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any, Iterator

import yaml

from finplan_contracts.schemas import SchemaStore
from finplan_contracts.validate import validate

Path_ = tuple[Any, ...]


def leaves(d: Any, path: Path_ = ()) -> Iterator[tuple[Path_, Any]]:
    if isinstance(d, dict):
        for k, v in d.items():
            yield from leaves(v, path + (k,))
    elif isinstance(d, list):
        for i, v in enumerate(d):
            yield from leaves(v, path + (i,))
    yield path, d


def set_at(d: Any, path: Path_, value: Any) -> Any:
    d = copy.deepcopy(d)
    cur = d
    for p in path[:-1]:
        cur = cur[p]
    cur[path[-1]] = value
    return d


def delete_at(d: Any, path: Path_) -> Any:
    d = copy.deepcopy(d)
    cur = d
    for p in path[:-1]:
        cur = cur[p]
    if isinstance(cur, dict):
        del cur[path[-1]]
    else:
        cur.pop(path[-1])
    return d


def mutations(doc: Any) -> Iterator[tuple[str, Any]]:
    for path, v in list(leaves(doc)):
        if not path:
            continue
        where = "/".join(map(str, path))
        yield f"{where}:del", delete_at(doc, path)
        yield f"{where}:null", set_at(doc, path, None)
        yield f"{where}:int", set_at(doc, path, 12)
        if isinstance(v, str):
            yield f"{where}:str", set_at(doc, path, "x")
            yield f"{where}:pl", set_at(doc, path, "pl_01J0000000000000000000000Q")
            yield f"{where}:s3", set_at(doc, path, "s3://example-bucket/k")
            # ECMA-262 `$` anchoring: a trailing newline must fail `^...$` in both languages
            yield f"{where}:nl", set_at(doc, path, v + "\n")
        if isinstance(v, dict):
            yield f"{where}:extra", set_at(doc, path, {**v, "zz_extra": 1})
            yield f"{where}:empty", set_at(doc, path, {})
        if isinstance(v, list):
            yield f"{where}:emptyl", set_at(doc, path, [])
            yield f"{where}:dup", set_at(doc, path, v + v)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    root = Path(args.root)
    store = SchemaStore(root)
    raw = yaml.safe_load((root / "conformance" / "cases.yaml").read_text(encoding="utf-8")) or {}
    cases = {c["fixture"]: c for c in raw.get("cases", [])}
    docs: list[tuple[str, str, Any, Any]] = []
    for info in store:
        for outcome in ("valid", "invalid"):
            for f in sorted((root / "fixtures" / info.name / outcome).glob("*.json")):
                rel = f.relative_to(root / "fixtures").as_posix()
                doc = json.loads(f.read_text(encoding="utf-8"))
                ctx = cases.get(rel, {}).get("context")
                docs.append((rel, info.name, doc, ctx))
                if outcome == "valid":
                    docs.extend((f"{rel}#{tag}", info.name, m, ctx) for tag, m in mutations(doc))
    out = []
    for ident, schema, doc, ctx in docs:
        r = validate(doc, schema, context=ctx, store=store)
        out.append({"id": ident, "schema": schema, "doc": doc, "context": ctx, "valid": r.valid, "code": r.code, "issues": sorted({(i.code, i.pointer) for i in r.issues})})
    Path(args.out).write_text(json.dumps(out), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
