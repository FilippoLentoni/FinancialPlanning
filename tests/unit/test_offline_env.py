"""The offline-safety environment applies to offline suites only (defect 2 of the first pipeline run).

The first prod smoke failed with ``UnrecognizedClientException`` because ``tests/conftest.py`` forced
fake credentials on every suite, deployed ones included. Now:

* offline suites (this one) run with fake credentials, no metadata service and no profile, config
  or credential files, so an un-mocked boto3 call can never reach an account;
* a deployed suite started by the stage runner (``FINPLAN_TARGET_ENV`` set, ``tests/integration``
  or ``tests/smoke`` collected on their own) keeps the real credentials.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError

from tests.offline_env import CREDENTIAL_SOURCES, FAKE_ACCESS_KEY, FAKE_SECRET_KEY, OFFLINE_MARKER, apply_offline_environment, deployed_suite_mode

ROOT = Path(__file__).resolve().parents[2]
#: Credential-shaped placeholders (not real keys) standing in for the stage role's credentials.
STAGE_ENV = {
    "AWS_ACCESS_KEY_ID": "stage-role-access-key-placeholder",
    "AWS_SECRET_ACCESS_KEY": "stage-role-secret-placeholder",
    "AWS_SESSION_TOKEN": "stage-role-session-token-placeholder",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/placeholder",
}


def test_this_offline_suite_runs_with_the_fake_credentials() -> None:
    assert os.environ["AWS_ACCESS_KEY_ID"] == FAKE_ACCESS_KEY and os.environ["AWS_SECRET_ACCESS_KEY"] == FAKE_SECRET_KEY
    assert os.environ["AWS_EC2_METADATA_DISABLED"] == "true"
    assert os.environ["AWS_CONFIG_FILE"] == os.devnull and os.environ["AWS_SHARED_CREDENTIALS_FILE"] == os.devnull
    assert os.environ[OFFLINE_MARKER] == "1"
    assert not any(v in os.environ for v in CREDENTIAL_SOURCES)
    creds = boto3.session.Session().get_credentials()
    assert creds is not None and creds.access_key == FAKE_ACCESS_KEY and creds.method == "env"


def test_apply_replaces_real_credentials_and_disables_metadata() -> None:
    env = {**STAGE_ENV, "AWS_PROFILE": "someone", "AWS_WEB_IDENTITY_TOKEN_FILE": "/tmp/token", "AWS_ROLE_ARN": "arn:aws:iam::<account-id>:role/x"}
    apply_offline_environment(env)
    assert env["AWS_ACCESS_KEY_ID"] == FAKE_ACCESS_KEY and env["AWS_SECRET_ACCESS_KEY"] == FAKE_SECRET_KEY
    assert env["AWS_EC2_METADATA_DISABLED"] == "true" and env[OFFLINE_MARKER] == "1"
    assert not set(CREDENTIAL_SOURCES) & set(env)


def test_deployed_suite_mode_is_the_stage_runner_target() -> None:
    assert deployed_suite_mode({"FINPLAN_TARGET_ENV": "beta"})
    assert not deployed_suite_mode({})
    assert not deployed_suite_mode({"FINPLAN_TARGET_ENV": ""})


def test_an_unmocked_aws_call_from_an_offline_test_never_reaches_aws(no_network: None) -> None:
    """Fake keys + no metadata + no network: the call fails locally (never an account response)."""
    client = boto3.client("sts", region_name="us-east-2")
    with pytest.raises((EndpointConnectionError, NoCredentialsError, OSError, ClientError)) as info:
        client.get_caller_identity()
    if isinstance(info.value, ClientError):  # pragma: no cover - would mean a real endpoint answered
        pytest.fail("an offline test reached an AWS endpoint")


def _run_pytest(paths: list[str], extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", OFFLINE_MARKER, "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_EC2_METADATA_DISABLED")}
    env.update(extra_env)
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", *paths], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


def test_deployed_suite_mode_keeps_the_stage_credentials() -> None:
    """The stage runner's invocation (FINPLAN_TARGET_ENV + tests/integration) leaves real credentials intact.

    Only the credential probe is selected; nothing in it calls AWS (the placeholders are not keys).
    """
    proc = _run_pytest(["tests/integration/test_deployed_environment.py", "-k", "real_credentials"], {**STAGE_ENV, "FINPLAN_TARGET_ENV": "beta", "FINPLAN_SUITE": "integration-beta"})
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "1 passed" in proc.stdout


def test_offline_suites_stay_hermetic_even_with_a_target_env_set() -> None:
    """tests/unit applies the offline environment unconditionally (FINPLAN_TARGET_ENV set or not)."""
    proc = _run_pytest(["tests/unit/test_offline_env.py", "-k", "runs_with_the_fake_credentials"], {**STAGE_ENV, "FINPLAN_TARGET_ENV": "beta"})
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "1 passed" in proc.stdout
