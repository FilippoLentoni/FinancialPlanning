"""Schema loader/registry: every schema is 2020-12, has a namespaced $id with its major, and resolves offline."""

import json

import pytest
from jsonschema import Draft202012Validator

from finplan_contracts.schemas import ID_BASE, ID_PATTERN, SchemaNotFound, schema_id


def test_all_schemas_load(store):
    assert len(store) >= 60
    for info in store:
        assert info.schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        Draft202012Validator.check_schema(info.schema)


def test_ids_carry_namespace_and_major(store):
    for info in store:
        m = ID_PATTERN.match(info.id)
        assert m, info.id
        assert m.group("ns") == info.namespace and int(m.group("major")) == info.major == 1
        assert info.id.startswith(ID_BASE)
        assert info.id == schema_id(info.namespace, info.name)


def test_names_unique_across_namespaces(store):
    assert len(set(store.names())) == len(store)


def test_lookup_by_name_id_and_namespace(store):
    by_name = store.get("plan-version")
    assert store.get(by_name.id) is by_name
    assert store.get("core/plan-version") is by_name
    assert store.get("core/v1/plan-version.json") is by_name
    assert store.get("tools/query-market-data-request").namespace == "finance"
    with pytest.raises(SchemaNotFound):
        store.get("finance/plan-version")
    with pytest.raises(SchemaNotFound):
        store.get("no-such-schema")


def test_every_ref_resolves_offline(store):
    def refs(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "$ref":
                    yield v
                else:
                    yield from refs(v)
        elif isinstance(node, list):
            for v in node:
                yield from refs(v)

    for info in store:
        for ref in refs(info.schema):
            uri = ref if ref.startswith("http") else info.id + ref if ref.startswith("#") else None
            assert uri is not None, f"{info.name}: relative ref {ref}"
            store.resolve_pointer(uri)


def test_declared_semantic_checks_are_registered(store):
    from finplan_contracts.validate import CHECKS

    for info in store:
        for check in info.checks:
            assert check in CHECKS, (info.name, check)


def test_core_schemas_have_no_finance_property_names(store):
    """DOM-01 (basic form for the schemas added here, incl. the D10 additions): no core
    schema declares a property named by the finance adapter's deny-list."""
    deny = set(store.domain_registry()["domains"][0]["neutrality_deny_terms"])

    def props(node):
        if isinstance(node, dict):
            for key in ("properties",):
                if isinstance(node.get(key), dict):
                    yield from node[key].keys()
            for v in node.values():
                yield from props(v)
        elif isinstance(node, list):
            for v in node:
                yield from props(v)

    for info in store:
        if info.namespace == "core":
            bad = set(props(info.schema)) & deny
            assert not bad, (info.name, bad)


def test_requests_are_closed_and_responses_open(store):
    for info in store:
        if info.name.startswith("tools/") and info.name.endswith("-request"):
            assert info.schema.get("additionalProperties") is False, info.name
            # These immutable 1.2/1.3 schemas predate the declarative semantic marker;
            # their deployed adapter pipeline still rejects storage inputs before validation.
            legacy = {
                "tools/get-performance-evidence-request", "tools/get-publication-request",
                "tools/list-executions-request", "tools/list-publications-request",
                "tools/recommend-portfolio-invocation-request", "tools/recommend-portfolio-request",
            }
            if info.name not in legacy:
                assert "no_storage_locations" in info.checks, info.name


def test_registry_json_is_valid(store):
    from finplan_contracts.validate import validate

    reg = json.loads((store.root / "domains" / "registry.json").read_text())
    assert validate(reg, "domain-registry").valid
