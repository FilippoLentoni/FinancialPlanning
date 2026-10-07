"""Offline-safety environment for the offline suites (unit, contract), never for deployed suites.

:func:`apply_offline_environment` removes every way botocore could find real credentials
(profile, session token, container and web-identity credentials, config and credential files),
forces fake static keys and disables the instance metadata service, so an accidental un-mocked
boto3 call fails instead of reaching an account. It marks the environment with
:data:`OFFLINE_MARKER`.

Where it applies:

* ``tests/unit/conftest.py`` and ``tests/contract/conftest.py`` apply it unconditionally: the
  offline suites are hermetic even when ``FINPLAN_TARGET_ENV`` happens to be set;
* ``tests/conftest.py`` applies it unless :func:`deployed_suite_mode` is true, that is unless
  ``scripts/stage_runner.py`` set ``FINPLAN_TARGET_ENV`` for a deployed suite
  (``tests/integration`` in beta and gamma, ``tests/smoke`` in prod). Those suites run with the
  stage role's real credentials.

Run deployed suites on their own directory (as the stage runner does): collecting
``tests/unit`` in the same session applies the offline environment to the whole process.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping

__all__ = ["CREDENTIAL_SOURCES", "FAKE_ACCESS_KEY", "FAKE_SECRET_KEY", "OFFLINE_MARKER", "apply_offline_environment", "deployed_suite_mode"]

FAKE_ACCESS_KEY = "testing-fake-key"
FAKE_SECRET_KEY = "testing-fake-secret"
OFFLINE_MARKER = "FINPLAN_OFFLINE_TESTS"
CREDENTIAL_SOURCES = (
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
)


def deployed_suite_mode(environ: MutableMapping[str, str] | None = None) -> bool:
    """True when the stage runner started a deployed suite (``FINPLAN_TARGET_ENV`` is set)."""
    env = os.environ if environ is None else environ
    return bool(env.get("FINPLAN_TARGET_ENV"))


def apply_offline_environment(environ: MutableMapping[str, str] | None = None) -> None:
    env = os.environ if environ is None else environ
    for var in CREDENTIAL_SOURCES:
        env.pop(var, None)
    env["AWS_ACCESS_KEY_ID"] = FAKE_ACCESS_KEY
    env["AWS_SECRET_ACCESS_KEY"] = FAKE_SECRET_KEY
    env["AWS_DEFAULT_REGION"] = "us-east-2"
    env["AWS_REGION"] = "us-east-2"
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    env["AWS_CONFIG_FILE"] = os.devnull
    env["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
    env[OFFLINE_MARKER] = "1"
