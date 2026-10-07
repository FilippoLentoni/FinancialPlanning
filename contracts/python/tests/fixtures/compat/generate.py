"""Regenerate the CS-03/OWN-07 compatibility-gate fixture pairs (SYNTHETIC).

Each case directory holds ``old/`` and ``new/`` contracts roots (VERSION + core/v<N>/...)
and ``case.json`` with the expected gate outcome. Run: python generate.py
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
ID = "https://contracts.finplan.invalid/core/v{major}/sample-record.json"
EXTRA_ID = "https://contracts.finplan.invalid/core/v1/sample-extra.json"


def base(major: int = 1) -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": ID.format(major=major),
        "title": "Sample record (SYNTHETIC compatibility fixture)",
        "description": "A small record used only by the compatibility-gate tests.",
        "type": "object",
        "properties": {
            "record_id": {"type": "string", "pattern": "^rec_[0-9A-HJKMNP-TV-Z]{26}$"},
            "status": {"type": "string", "enum": ["active", "retired"]},
            "flags": {"type": "array", "items": {"$ref": "#/$defs/flag"}, "uniqueItems": True},
            "note": {"type": "string", "maxLength": 200},
            "synthetic": {"const": True},
        },
        "required": ["record_id", "status"],
        "additionalProperties": False,
        "$defs": {
            "flag": {
                "type": "string",
                "pattern": "^[a-z][a-z0-9_]{0,63}$",
                "x-finplan-open-enum": True,
                "x-finplan-known-values": ["stale_source", "partial_response"],
            }
        },
    }


DOC = {"record_id": "rec_01JABCDEFGHJKMNPQRSTVWXYZ0", "status": "active", "flags": ["stale_source"], "note": "made under the old version", "synthetic": True}


def write_root(root: Path, version: str, schemas: list[dict], fixtures: bool = False, migration: dict | None = None) -> None:
    root.mkdir(parents=True)
    (root / "VERSION").write_text(version + "\n")
    for s in schemas:
        rel = s["$id"].split("contracts.finplan.invalid/", 1)[1]
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(s, indent=2) + "\n")
    if fixtures:
        p = root / "fixtures" / "sample-record" / "valid" / "produced-under-old.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps(DOC, indent=2) + "\n")
    if migration is not None:
        p = root / "migrations" / f"v{migration['to_major']}.yaml"
        p.parent.mkdir(parents=True)
        lines = [f"from_major: {migration['from_major']}", f"to_major: {migration['to_major']}", "consumers:"]
        lines += [f"  - {c}" for c in migration["consumers"]]
        lines.append(f"removal_criterion: {migration['removal_criterion']}")
        p.write_text("\n".join(lines) + "\n")


def mutate(fn, major: int = 1) -> dict:
    s = copy.deepcopy(base(major))
    fn(s)
    return s


MIGRATION_2 = {"from_major": 1, "to_major": 2, "consumers": ["financelambdastool", "financeagent"], "removal_criterion": "v1 removed after every consumer in prod pins contracts 2.x"}
MIGRATION_1 = dict(MIGRATION_2, from_major=0, to_major=1, removal_criterion="0.x was beta only and is never served in gamma or prod")


def drop_note(s):
    s["properties"].pop("note")


CASES = {
    "additive-optional-field": ("1.2.0", "1.3.0", [base()], [mutate(lambda s: s["properties"].update(label={"type": "string"}))], {}, True, "minor", True),
    "additive-under-patch": ("1.2.0", "1.2.1", [base()], [mutate(lambda s: s["properties"].update(label={"type": "string"}))], {}, False, "minor", False),
    "optional-made-required": ("1.2.0", "1.3.0", [base()], [mutate(lambda s: s["required"].append("note"))], {}, False, "major", True),
    "field-removed-minor": ("1.2.0", "1.3.0", [base()], [mutate(drop_note)], {}, False, "major", False),
    "type-changed": ("1.2.0", "1.3.0", [base()], [mutate(lambda s: s["properties"].update(note={"type": "integer"}))], {}, False, "major", False),
    "closed-enum-value-added": ("1.2.0", "1.3.0", [base()], [mutate(lambda s: s["properties"]["status"]["enum"].append("paused"))], {}, False, "major", False),
    "open-enum-value-added": ("1.2.0", "1.3.0", [base()], [mutate(lambda s: s["$defs"]["flag"]["x-finplan-known-values"].append("source_revised"))], {}, True, "minor", False),
    "new-schema-added": ("1.2.0", "1.3.0", [base()], [base(), {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": EXTRA_ID, "type": "object", "properties": {"synthetic": {"const": True}}}], {}, True, "minor", False),
    "annotation-only-patch": ("1.2.0", "1.2.1", [base()], [mutate(lambda s: s.update(description="Clarified wording only."))], {}, True, "patch", False),
    "same-version-changed": ("1.2.0", "1.2.0", [base()], [mutate(lambda s: s.update(description="Edited after publication."))], {}, False, "patch", False),
    "downgrade": ("1.2.0", "1.1.0", [base()], [base()], {}, False, "none", False),
    "major-v2-with-migration": ("1.2.0", "2.0.0", [base()], [base(), mutate(drop_note, 2)], {"migration": MIGRATION_2}, True, "minor", False),
    "major-v1-removed-with-migration": ("1.2.0", "2.0.0", [base()], [mutate(drop_note, 2)], {"migration": MIGRATION_2}, True, "major", False),
    "major-v1-removed-without-migration": ("1.2.0", "2.0.0", [base()], [mutate(drop_note, 2)], {}, False, "major", False),
    "major-in-place-breaking": ("1.2.0", "2.0.0", [base()], [mutate(drop_note)], {"migration": MIGRATION_2}, False, "major", False),
    "zero-to-one-in-place": ("0.3.0", "1.0.0", [base()], [mutate(drop_note)], {"migration": MIGRATION_1}, True, "major", False),
    "zero-minor-breaking": ("0.3.0", "0.4.0", [base()], [mutate(drop_note)], {}, False, "major", False),
    "id-major-ahead-of-package": ("1.2.0", "1.3.0", [base()], [base(), mutate(lambda s: None, 2)], {}, False, "minor", False),
}


def main() -> None:
    for name, (ov, nv, old, new, extra, ok, need, fixtures) in CASES.items():
        d = HERE / name
        shutil.rmtree(d, ignore_errors=True)
        write_root(d / "old", ov, old, fixtures=fixtures)
        write_root(d / "new", nv, new, migration=extra.get("migration"))
        (d / "case.json").write_text(json.dumps({"synthetic": True, "old_version": ov, "new_version": nv, "expect_ok": ok, "required_bump": need}, indent=2) + "\n")


if __name__ == "__main__":
    main()
