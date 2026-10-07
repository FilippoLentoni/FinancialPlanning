"""OWN-08: end-to-end compatibility gate over the four repositories' manifests in one environment (task 8.4)."""

from __future__ import annotations

import json

import pytest

from finplan_contracts import manifest_gate
from finplan_contracts.manifest_gate import gate, load_manifests
from finplan_contracts.validate import validate

from infra_helpers import INFRA


def _set(name):
    return load_manifests([INFRA / "manifests" / name])


@pytest.mark.parametrize("name", ["gamma-compatible", "gamma-incompatible-major", "gamma-two-majors-served", "gamma-agent-missing", "gamma-mixed-environments"])
def test_fixture_manifests_validate_against_the_contract(name):
    for m in _set(name):
        assert validate(m, "release-manifest").valid, (name, m["repo"])
        assert m["synthetic"] is True


def test_compatible_releases_pass():
    res = gate(_set("gamma-compatible"), "gamma")
    assert res.compatible, res.to_dict()
    assert set(res.releases) == {"financialplanning", "financemodel", "financelambdastool", "financeagent"}


def test_tool_pinned_to_major_2_with_platform_serving_only_1_is_blocked():
    res = gate(_set("gamma-incompatible-major"), "gamma")
    assert not res.compatible
    pairs = {(i.consumer, i.producer) for i in res.incompatibilities}
    assert ("financelambdastool", "financialplanning") in pairs
    i = next(i for i in res.incompatibilities if i.producer == "financialplanning")
    assert i.pinned_major == 2 and i.served_contract_majors == (1,)
    assert "serves only major(s) 1" in str(i)


def test_producers_serving_both_majors_during_migration_pass():
    assert gate(_set("gamma-two-majors-served"), "gamma").compatible


def test_missing_release_blocks_with_dependency_missing():
    res = gate(_set("gamma-agent-missing"), "gamma")
    assert not res.compatible and any("financeagent" in p and "dependency missing" in p for p in res.problems)
    assert gate(_set("gamma-agent-missing"), "gamma", participants=["financialplanning", "financemodel", "financelambdastool"]).compatible


def test_manifest_from_another_environment_does_not_count():
    res = gate(_set("gamma-mixed-environments"), "gamma")
    assert not res.compatible and any("financemodel" in p for p in res.problems)


def test_region_mismatch_and_invalid_manifest_block():
    ms = _set("gamma-compatible")
    ms[0] = {**ms[0], "region": "us-west-2"}
    assert not gate(ms, "gamma").compatible
    ms = _set("gamma-compatible")
    del ms[1]["artifact_digest"]
    assert not gate(ms, "gamma").compatible


def test_cli(capsys):
    assert manifest_gate.main(["--env", "gamma", str(INFRA / "manifests" / "gamma-compatible")]) == 0
    assert "COMPATIBLE" in capsys.readouterr().out
    assert manifest_gate.main(["--env", "gamma", "--json", str(INFRA / "manifests" / "gamma-incompatible-major")]) == 1
    assert json.loads(capsys.readouterr().out)["compatible"] is False


def test_reads_manifests_through_an_injected_read_only_ssm_client():
    stored = {f"/finplan/gamma/{m['repo']}/release/manifest": json.dumps(m) for m in _set("gamma-compatible")}

    class ParameterNotFound(Exception):
        pass

    class FakeSsm:
        calls: list[str] = []

        def get_parameter(self, Name):
            self.calls.append(Name)
            if Name not in stored:
                raise ParameterNotFound(Name)
            return {"Parameter": {"Name": Name, "Value": stored[Name]}}

    fake = FakeSsm()
    assert gate(manifest_gate.load_from_ssm(fake, "gamma"), "gamma").compatible
    assert all(n.startswith("/finplan/gamma/") for n in fake.calls)


@pytest.mark.parametrize("env, allowed", [("beta", True), ("gamma", False), ("prod", False)])
def test_zero_x_contract_pins_are_beta_only(env, allowed):
    """Design risk table / task 7.4: 0.x contracts stay in beta; gamma and prod require >= 1.0.0."""
    manifests = []
    for m in _set("gamma-compatible"):
        m = json.loads(json.dumps(m).replace("/finplan/gamma/", f"/finplan/{env}/"))
        m["environment"] = env
        if m["repo"] == "financeagent":
            m["contract_version"] = "0.3.0"
        if m.get("served_contract_majors"):
            m["served_contract_majors"] = [0, 1]
        manifests.append(m)
    res = gate(manifests, env)
    assert res.compatible is allowed, res.to_dict()
    if not allowed:
        assert any("0.x pre-release" in p and "financeagent" in p for p in res.problems)
