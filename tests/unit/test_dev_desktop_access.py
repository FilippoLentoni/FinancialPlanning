"""Beta operator access uses deployed builders and keeps integrity denials."""
import copy

import pytest

from finplan_platform.core.config import EnvConfig, load_config, validate_config
from finplan_platform.handlers.api import ROUTES, resolve_role_class, route_allowed
from infra.policy_sim import Principal, simulate
from infra.stacks.api import resource_policy_document
from infra.stacks.policies import WRITE_ONCE
from tests.unit.test_policy_simulation import ddbsim, policies, s3sim

ADMIN = {"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}
DESKTOP = Principal.role("finplan-devdesktop", ADMIN)


@pytest.mark.parametrize("env", ["beta", "gamma", "prod"])
def test_deployed_table_bucket_and_api_access(policies, env):
    expected = env == "beta"
    for logical in ("portfolio", "execution", "snapshot-catalog", "audit-event"):
        assert bool(ddbsim(policies, "dynamodb:Scan", env, logical, DESKTOP)) == expected
        assert bool(ddbsim(policies, "dynamodb:PutItem", env, logical, DESKTOP)) == expected
    for stem in ("raw", "curated", "snapshots", "plans", "run-staging-area", "reports"):
        assert bool(s3sim(policies, "s3:GetObject", env, stem, "uploads/excel/example", DESKTOP)) == expected
    cfg = load_config(env)
    policy = resource_policy_document(cfg, lambda name: f"arn:aws:iam::<account-id>:role/{name}")
    for route in ROUTES:
        resource = f"execute-api:/live/{route.resource_pattern}"
        assert bool(simulate("execute-api:Invoke", resource, DESKTOP, resource_policy=policy)) == expected
        assert route_allowed(route, resolve_role_class(DESKTOP.arn, cfg)) == expected


@pytest.mark.parametrize("logical", ["audit-event", "portfolio-history", "activity-event"])
def test_beta_append_only_history_still_denies_desktop(policies, logical):
    for action in ("dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem", "dynamodb:PartiQLUpdate"):
        assert ddbsim(policies, action, "beta", logical, DESKTOP).decision == "explicitDeny"


def test_beta_storage_integrity_still_denies_desktop(policies):
    assert not s3sim(policies, "s3:GetObject", "beta", "raw", "example", DESKTOP, **{"aws:SecureTransport": "false"})
    for stem, prefixes in WRITE_ONCE.items():
        stem = "run-staging-area" if stem == "outputs" else stem
        for prefix in prefixes:
            assert not s3sim(policies, "s3:PutObject", "beta", stem, prefix + "example", DESKTOP)
            assert not s3sim(policies, "s3:DeleteObject", "beta", stem, prefix + "example", DESKTOP)
    assert not s3sim(policies, "s3:PutObject", "beta", "curated", "example", DESKTOP, **{"s3:x-amz-server-side-encryption": "AES256"})


@pytest.mark.parametrize("env", ["gamma", "prod"])
def test_operator_exception_is_rejected_and_inactive_outside_beta(env):
    doc = copy.deepcopy(load_config(env).data)
    doc["dev_desktop_access"] = {"role_names": ["finplan-devdesktop"]}
    assert validate_config(doc, env=env)
    assert EnvConfig(doc).dev_desktop_role_names == ()
    assert resolve_role_class(DESKTOP.arn, EnvConfig(doc)) is None
