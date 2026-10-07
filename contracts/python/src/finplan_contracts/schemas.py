"""Schema loader and registry.

Loads every JSON Schema under ``<root>/core/v<major>/`` and ``<root>/<domain>/v<major>/``
into a :class:`referencing.Registry` keyed by ``$id``, so ``$ref`` between schemas
resolves offline (``$id`` values are identifiers only and are never fetched).

Schema *names* are the path below ``<namespace>/v<major>/`` without ``.json``
(for example ``plan-version`` or ``tools/get-plan-request``). Names are unique
across namespaces; fixtures live at ``fixtures/<name>/{valid,invalid}/``.

The contracts root is found, in order, from: an explicit ``root`` argument, the
``FINPLAN_CONTRACTS_ROOT`` environment variable, the data bundled in an installed
wheel (``finplan_contracts/data``), or the source checkout (the nearest parent
directory holding ``VERSION`` and ``core/``).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

#: Base of every contract ``$id`` (design D3). The ``.invalid`` host keeps ``$id``s
#: as identifiers only, never fetched URLs.

# ------------------------------------------------------------ ECMA patterns
# JSON Schema ``pattern`` is ECMA-262: without the multiline flag ``$`` matches only
# at the very end of the string. Python's ``$`` also matches before a trailing
# newline, so ``"pv_<ULID>\n"`` would pass ``^pv_...$`` here but fail in the
# TypeScript (Ajv) validator. ``ecma_pattern`` rewrites every unescaped ``$`` outside
# a character class to ``\Z`` so both languages agree (CS-10, ID-01).
def ecma_pattern(pattern: str) -> str:
    out: list[str] = []
    i, in_class = 0, False
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            out.append(pattern[i : i + 2])
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
        elif c == "$":
            out.append(r"\Z")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


@lru_cache(maxsize=None)
def _compiled(pattern: str) -> "re.Pattern[str]":
    return re.compile(ecma_pattern(pattern))


def _ecma_pattern_keyword(validator: Any, patrn: str, instance: Any, schema: Any) -> Iterator[ValidationError]:
    if validator.is_type(instance, "string") and not _compiled(patrn).search(instance):
        yield ValidationError(f"{instance!r} does not match {patrn!r}")


def _ecma_pattern_properties(validator: Any, pattern_properties: dict[str, Any], instance: Any, schema: Any) -> Iterator[ValidationError]:
    if not validator.is_type(instance, "object"):
        return
    for patrn, subschema in pattern_properties.items():
        for k, v in instance.items():
            if _compiled(patrn).search(k):
                yield from validator.descend(v, subschema, path=k, schema_path=patrn)


#: Draft 2020-12 with ECMA-262 ``pattern``/``patternProperties`` anchoring.
ContractValidator = validators.extend(
    Draft202012Validator,
    {"pattern": _ecma_pattern_keyword, "patternProperties": _ecma_pattern_properties},
)

# jsonschema's ``evolve`` (used on every ``$ref`` descent) re-selects the validator
# class from the target schema's ``$schema``, which would switch back to the stock
# Draft202012Validator inside every referenced contract schema. Keep our class for
# the 2020-12 dialect.
_EVOLVE_FIELDS = [(f.name, f.alias) for f in ContractValidator.__attrs_attrs__ if f.init]


def _evolve(self: Any, **changes: Any) -> Any:
    schema = changes.setdefault("schema", self.schema)
    cls = validators.validator_for(schema, default=ContractValidator)
    if cls is Draft202012Validator:
        cls = ContractValidator
    for attr_name, init_name in _EVOLVE_FIELDS:
        if init_name not in changes:
            changes[init_name] = getattr(self, attr_name)
    return cls(**changes)


ContractValidator.evolve = _evolve  # type: ignore[method-assign]

ID_BASE = "https://contracts.finplan.invalid/"
ID_PATTERN = re.compile(r"^https://contracts\.finplan\.invalid/(?P<ns>[a-z][a-z0-9_]*)/v(?P<major>[0-9]+)/(?P<name>[a-z0-9/-]+)\.json$")
CORE_NAMESPACE = "core"
#: Directory names under the contracts root that are not schema namespaces.
NON_SCHEMA_DIRS = {"fixtures", "domains", "ownership", "conformance", "python", "typescript", "templates", "node_modules"}
ENV_ROOT = "FINPLAN_CONTRACTS_ROOT"


class SchemaNotFound(KeyError):
    """Raised when a schema name or ``$id`` is not in the store."""


@dataclass(frozen=True)
class SchemaInfo:
    name: str
    namespace: str
    major: int
    id: str
    path: Path
    schema: dict[str, Any] = field(repr=False, hash=False, compare=False)

    @property
    def checks(self) -> list[str]:
        return list(self.schema.get("x-finplan-checks", []))


def _looks_like_root(p: Path) -> bool:
    return (p / "VERSION").is_file() and (p / "core").is_dir()


def contracts_root(root: str | os.PathLike[str] | None = None) -> Path:
    """Return the contracts root directory (see module docstring)."""
    if root is not None:
        return Path(root).resolve()
    env = os.environ.get(ENV_ROOT)
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve().parent
    bundled = here / "data"
    if _looks_like_root(bundled):
        return bundled
    for parent in here.parents:
        if _looks_like_root(parent):
            return parent
    raise FileNotFoundError(f"contracts root not found; set {ENV_ROOT}")


def bundled_root() -> Path | None:
    """Root of the data bundled inside an installed wheel, if present."""
    bundled = Path(__file__).resolve().parent / "data"
    return bundled if _looks_like_root(bundled) else None


def contract_version(root: str | os.PathLike[str] | None = None) -> str:
    return (contracts_root(root) / "VERSION").read_text(encoding="utf-8").strip()


class SchemaStore:
    """All contract schemas of one contracts root, indexed by name and ``$id``."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = contracts_root(root)
        self._by_name: dict[str, SchemaInfo] = {}
        self._by_id: dict[str, SchemaInfo] = {}
        self._load()
        self.registry: Registry = Registry().with_resources(
            (info.id, Resource(contents=info.schema, specification=DRAFT202012)) for info in self._by_id.values()
        )
        self._validators: dict[str, Any] = {}

    # ------------------------------------------------------------------ load
    def _schema_files(self) -> Iterator[tuple[str, int, Path, Path]]:
        for ns_dir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            if ns_dir.name in NON_SCHEMA_DIRS or ns_dir.name.startswith("."):
                continue
            for major_dir in sorted(p for p in ns_dir.iterdir() if p.is_dir() and re.fullmatch(r"v[0-9]+", p.name)):
                for path in sorted(major_dir.rglob("*.json")):
                    yield ns_dir.name, int(major_dir.name[1:]), major_dir, path

    def _load(self) -> None:
        for ns, major, major_dir, path in self._schema_files():
            name = path.relative_to(major_dir).with_suffix("").as_posix()
            schema = json.loads(path.read_text(encoding="utf-8"))
            expected = f"{ID_BASE}{ns}/v{major}/{name}.json"
            sid = schema.get("$id")
            if sid != expected:
                raise ValueError(f"{path}: $id {sid!r} does not match its location (expected {expected!r})")
            Draft202012Validator.check_schema(schema)
            if name in self._by_name:
                other = self._by_name[name]
                raise ValueError(f"duplicate schema name {name!r}: {other.path} and {path}")
            info = SchemaInfo(name=name, namespace=ns, major=major, id=sid, path=path, schema=schema)
            self._by_name[name] = info
            self._by_id[sid] = info

    # ---------------------------------------------------------------- access
    def names(self) -> list[str]:
        return sorted(self._by_name)

    def __iter__(self) -> Iterator[SchemaInfo]:
        return iter(sorted(self._by_name.values(), key=lambda i: i.name))

    def __len__(self) -> int:
        return len(self._by_name)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and (self._resolve_key(key) is not None)

    def _resolve_key(self, key: str) -> SchemaInfo | None:
        if key in self._by_id:
            return self._by_id[key]
        if key in self._by_name:
            return self._by_name[key]
        # Accept "<namespace>/<name>" and "<namespace>/v1/<name>(.json)" forms.
        m = re.fullmatch(r"(?P<ns>[a-z][a-z0-9_]*)/(v(?P<major>[0-9]+)/)?(?P<name>[a-z0-9/-]+?)(\.json)?", key)
        if m:
            info = self._by_name.get(m.group("name"))
            if info and info.namespace == m.group("ns") and (m.group("major") is None or int(m.group("major")) == info.major):
                return info
        return None

    def get(self, key: str) -> SchemaInfo:
        """Look up a schema by name (``plan-version``), ``ns/name`` or ``$id``."""
        info = self._resolve_key(key)
        if info is None:
            raise SchemaNotFound(key)
        return info

    def validator(self, key: str) -> Any:
        info = self.get(key)
        v = self._validators.get(info.id)
        if v is None:
            v = ContractValidator(info.schema, registry=self.registry)
            self._validators[info.id] = v
        return v

    def resolve_pointer(self, uri: str) -> Any:
        """Resolve ``<$id>#/json/pointer`` to the referenced subschema."""
        resolver = self.registry.resolver()
        return resolver.lookup(uri).contents

    # -------------------------------------------------------------- metadata
    def fixtures_dir(self, key: str) -> Path:
        return self.root / "fixtures" / self.get(key).name

    def domain_registry(self) -> dict[str, Any]:
        return json.loads((self.root / "domains" / "registry.json").read_text(encoding="utf-8"))

    @property
    def version(self) -> str:
        return (self.root / "VERSION").read_text(encoding="utf-8").strip()


@lru_cache(maxsize=8)
def _cached_store(root: str) -> SchemaStore:
    return SchemaStore(root)


def load_store(root: str | os.PathLike[str] | None = None) -> SchemaStore:
    """Return a cached :class:`SchemaStore` for ``root`` (default discovery if None)."""
    return _cached_store(str(contracts_root(root)))


def schema_id(namespace: str, name: str, major: int = 1) -> str:
    return f"{ID_BASE}{namespace}/v{major}/{name}.json"
