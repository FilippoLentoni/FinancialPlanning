"""finplan-conformance subcommand map and lazy loading."""

from finplan_contracts import cli

from conftest import FIXTURES

EXPECTED = {
    "validate": "validate", "conformance": "conformance", "ownership-check": "ownership", "neutrality": "neutrality",
    "leak-scan": "leak_scan", "copied-id": "copied_id", "live-perm-scan": "live_perms", "compat": "compat",
    "digest": "digests", "manifest-gate": "manifest_gate", "pipeline-check": "pipeline_check",
    "bootstrap-precheck": "bootstrap", "budget": "budget", "ssm-path": "ssm",
}


def test_subcommand_map():
    assert {k: v[0] for k, v in cli.SUBCOMMANDS.items()} == EXPECTED


def test_help_and_unknown(capsys):
    assert cli.main(["--help"]) == 0
    assert cli.main([]) == 2
    assert cli.main(["nope"]) == 2


def test_missing_module_only_breaks_its_subcommand(monkeypatch, capsys):
    monkeypatch.setitem(cli.SUBCOMMANDS, "ghost", ("module_that_does_not_exist", "x"))
    assert cli.main(["ghost"]) == 2
    assert "unavailable" in capsys.readouterr().err
    assert cli.main(["validate", "--schema", "execution", str(FIXTURES / "execution" / "valid" / "paper.json")]) == 0


def test_validate_subcommand_reports_invalid(capsys):
    rc = cli.main(["validate", "--schema", "execution", "--envelope", str(FIXTURES / "execution" / "invalid" / "live-mode.json")])
    out = capsys.readouterr().out
    assert rc == 1 and "OPERATION_NOT_PERMITTED" in out


def test_validate_subcommand_context(capsys):
    path = str(FIXTURES / "budget-allocation" / "valid" / "defaults.json")
    assert cli.main(["validate", "-s", "budget-allocation", path]) == 0
    assert cli.main(["validate", "-s", "budget-allocation", "--context", "cost_ceiling_usd=40", path]) == 1


def test_conformance_subcommand():
    assert cli.main(["conformance"]) == 0
