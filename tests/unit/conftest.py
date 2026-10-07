"""Offline suite: always hermetic, even if ``FINPLAN_TARGET_ENV`` is set (``tests/offline_env.py``)."""

from tests.offline_env import apply_offline_environment

apply_offline_environment()
