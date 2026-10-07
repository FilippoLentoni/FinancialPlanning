"""Shared, offline test fixtures (FOUNDATION-owned; other suites build on these).

Guarantees for every offline test (``tests/unit``, ``tests/contract``; see ``tests/offline_env.py``):

* no real AWS credentials or endpoints are reachable: fake credentials are forced, the
  instance metadata service is disabled and ``AWS_PROFILE`` is removed, so an
  accidental un-mocked boto3 call fails instead of reaching an account. The deployed suites
  (``tests/integration``, ``tests/smoke``, run by ``scripts/stage_runner.py`` with
  ``FINPLAN_TARGET_ENV`` set) keep the stage role's real credentials;
* time comes from :class:`FrozenClock` (``clock`` fixture);
* metadata tests run against the in-memory fake (``ddb``), and the ``ddb_any``
  fixture parametrizes a test over the fake AND moto, proving the fake matches the
  DynamoDB semantics the repository relies on;
* S3 runs on moto (``s3``) with the six platform buckets created (``buckets``) and an
  :class:`ArtifactStore` bound to them (``artifacts``).

Fixtures: ``clock``, ``ctx``, ``ctx_factory``, ``ddb``, ``ddb_any``, ``repo``,
``repo_any``, ``s3``, ``buckets``, ``artifacts``, ``kms_key_id``, ``env_config``,
``no_network``.
"""

from __future__ import annotations

import socket
from typing import Any, Callable, Iterator

import pytest

# ---------------------------------------------------------------- hermetic AWS environment
# Offline suites get fake credentials and no metadata service (tests/offline_env.py). A deployed
# suite started by scripts/stage_runner.py (FINPLAN_TARGET_ENV set) keeps the stage role's real
# credentials; tests/unit and tests/contract apply the offline environment again unconditionally.
from tests.offline_env import apply_offline_environment, deployed_suite_mode  # noqa: E402

if not deployed_suite_mode():
    apply_offline_environment()

from moto import mock_aws  # noqa: E402

from finplan_platform.core.artifacts import ArtifactStore  # noqa: E402
from finplan_platform.core.clock import FrozenClock  # noqa: E402
from finplan_platform.core.config import BUCKET_ROLES, EnvConfig, load_config  # noqa: E402
from finplan_platform.core.context import Caller, OperationContext  # noqa: E402
from finplan_platform.core.ids import IdFactory  # noqa: E402
from finplan_platform.core.repository import MetadataRepository, create_tables  # noqa: E402
from tests.fakes import FakeDynamoDB  # noqa: E402

REGION = "us-east-2"
TEST_PRINCIPAL = "arn:aws:iam::<account-id>:role/finplan-beta-financialplanning-plan-api-handler-role"


@pytest.fixture
def clock() -> FrozenClock:
    """Monday 2026-01-12 14:30 UTC (09:30 America/New_York, a regular XNYS session)."""
    return FrozenClock("2026-01-12T14:30:00Z")


@pytest.fixture
def ctx_factory(clock: FrozenClock) -> Callable[..., OperationContext]:
    counter = {"n": 0}

    def make(principal: str = TEST_PRINCIPAL, *, env: str = "beta", role_class: str | None = None, channel: str | None = "direct_test", trigger: str = "on_demand") -> OperationContext:
        counter["n"] += 1
        return OperationContext(
            caller=Caller(principal=principal, role_class=role_class, channel=channel),
            env=env,
            correlation_id=f"cor_test{counter['n']:08d}",
            clock=clock,
            ids=IdFactory(clock),
            trigger=trigger,
            synthetic=True,
        )

    return make


@pytest.fixture
def ctx(ctx_factory: Callable[..., OperationContext]) -> OperationContext:
    return ctx_factory()


# ---------------------------------------------------------------- metadata
@pytest.fixture
def ddb() -> FakeDynamoDB:
    client = FakeDynamoDB()
    create_tables(client, "beta")
    return client


@pytest.fixture
def repo(ddb: FakeDynamoDB) -> MetadataRepository:
    return MetadataRepository(ddb, "beta")


@pytest.fixture(params=["fake", "moto"])
def ddb_any(request: pytest.FixtureRequest) -> Iterator[Any]:
    if request.param == "fake":
        client = FakeDynamoDB()
        create_tables(client, "beta")
        yield client
        return
    with mock_aws():
        import boto3

        client = boto3.client("dynamodb", region_name=REGION)
        create_tables(client, "beta")
        yield client


@pytest.fixture
def repo_any(ddb_any: Any) -> MetadataRepository:
    return MetadataRepository(ddb_any, "beta")


# ---------------------------------------------------------------- storage
@pytest.fixture
def s3() -> Iterator[Any]:
    with mock_aws():
        import boto3

        yield boto3.client("s3", region_name=REGION)


@pytest.fixture
def kms_key_id() -> Iterator[str]:
    """A moto KMS key (only valid inside the ``s3`` fixture's mock)."""
    import boto3

    kms = boto3.client("kms", region_name=REGION)
    yield kms.create_key(Description="platform test key (synthetic)")["KeyMetadata"]["Arn"]


@pytest.fixture
def buckets(s3: Any) -> dict[str, str]:
    names = {role: f"example-beta-{role}" for role in BUCKET_ROLES}
    for name in names.values():
        s3.create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": REGION})
    return names


@pytest.fixture
def artifacts(s3: Any, buckets: dict[str, str], clock: FrozenClock) -> ArtifactStore:
    return ArtifactStore(s3, buckets, clock=clock)


# ---------------------------------------------------------------- configuration
@pytest.fixture(params=["beta", "gamma", "prod"])
def env_config(request: pytest.FixtureRequest) -> EnvConfig:
    return load_config(request.param)


# ---------------------------------------------------------------- network guard
@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any outbound socket connection (XLS-03, provider tests)."""

    def refuse(*_a: Any, **_k: Any) -> None:
        raise OSError("network access is blocked in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# ---------------------------------------------------------------- CDK synthesis (offline)
@pytest.fixture(scope="session")
def foundation_synth() -> dict[str, Any]:
    """Synthesize the FOUNDATION stacks (storage + metadata) for all three environments once.

    Returns ``{"templates": {stack_name: template_json}, "stacks": {env: {"storage": Stack,
    "metadata": Stack}}, "resolver": TemplateResolver}``. Other agents' optional stack modules
    are not included here (``env_modules=()``) so these assertions stay independent of them.
    """
    import aws_cdk as cdk
    from aws_cdk.assertions import Template

    from infra.app import build_app
    from infra.policy_sim import TemplateResolver

    app = build_app(cdk.App(), ["beta", "gamma", "prod"], env_modules=(), app_modules=())
    templates: dict[str, Any] = {}
    stacks: dict[str, dict[str, Any]] = {}
    for env in ("beta", "gamma", "prod"):
        stage = app.node.find_child(env.capitalize())
        storage = stage.node.find_child("Storage")
        metadata = stage.node.find_child("Metadata")
        stacks[env] = {"storage": storage, "metadata": metadata}
        templates[storage.stack_name] = Template.from_stack(storage).to_json()
        templates[metadata.stack_name] = Template.from_stack(metadata).to_json()
    return {"templates": templates, "stacks": stacks, "resolver": TemplateResolver(templates)}
