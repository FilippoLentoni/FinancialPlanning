"""ENV-17 schema level (13.3): budget allocation schema, sum check and budget_category."""

from finplan_contracts.validate import validate

from conftest import fixture


def test_default_allocation_is_valid_and_sums_to_the_ceiling(store):
    defaults = fixture("budget-allocation/valid/defaults.json")
    assert defaults == {"platform_infra": 8, "cpu_research": 7, "bedrock_explanations": 5, "gpu": 25, "reserve": 5}
    assert sum(defaults.values()) == 50 == store.get("budget-allocation").schema["x-finplan-default-ceiling-usd"]
    assert store.get("budget-allocation").schema["x-finplan-default-allocation"] == defaults
    assert validate(defaults, "budget-allocation").valid


def test_sum_above_ceiling_invalid():
    res = validate(fixture("budget-allocation/invalid/sum-above-ceiling.json"), "budget-allocation")
    assert res.code == "VALIDATION_FAILED" and "ceiling" in res.issues[0].message


def test_ceiling_from_context():
    defaults = fixture("budget-allocation/valid/defaults.json")
    assert not validate(defaults, "budget-allocation", context={"cost_ceiling_usd": 40}).valid
    assert validate(fixture("budget-allocation/valid/raised-ceiling-context.json"), "budget-allocation", context={"cost_ceiling_usd": 60}).valid
    assert not validate(fixture("budget-allocation/valid/raised-ceiling-context.json"), "budget-allocation").valid


def test_jev_spend_is_not_a_category():
    assert not validate(fixture("budget-allocation/invalid/unregistered-category-typesafe-jev.json"), "budget-allocation").valid
    assert not validate(fixture("cost-estimate/invalid/unregistered-category-typesafe.json"), "cost-estimate").valid


def test_budget_category_on_cost_estimate_and_budget_exceeded_envelope():
    for cat in ("cpu-research", "gpu", "category-platform-infra", "category-bedrock-explanations", "category-reserve"):
        assert validate(fixture(f"cost-estimate/valid/{cat}.json"), "cost-estimate").valid
    env = fixture("error/valid/budget-exceeded.json")
    assert env["code"] == "BUDGET_EXCEEDED" and env["retryable"] is False and env["details"]["budget_category"] == "cpu_research"
    assert validate(env, "error").valid
    assert not validate({**env, "retryable": True}, "error").valid


def test_negative_and_empty_allocations_invalid():
    assert not validate(fixture("budget-allocation/invalid/negative-amount.json"), "budget-allocation").valid
    assert not validate(fixture("budget-allocation/invalid/empty.json"), "budget-allocation").valid
