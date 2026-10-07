#!/usr/bin/env python3
"""FinancialPlanning CDK app: one ``Stage`` per environment plus account-level (shared) stacks.

Synthesis is offline: stacks are account-agnostic (``AWS::AccountId`` is a deploy-time pseudo
parameter), configuration comes from ``config/<env>.json``, and no context lookups are used.

Run: ``npx aws-cdk@2 synth`` (``cdk.json``: ``app = uv run python infra/app.py``).
Select environments with ``-c envs=beta,gamma`` (default: all three).

Hook contract for stack modules owned by other agents
------------------------------------------------------
* per-environment modules (``infra.stacks.api``, ``infra.stacks.ingestion``) expose
  ``add_to_stage(stage: aws_cdk.Stage, ctx: infra.stacks.common.StageContext) -> None``;
  ``ctx.storage`` / ``ctx.metadata`` are the environment's StorageStack and MetadataStack
  (use their ``grant_artifacts`` / ``grant_metadata`` / ``bucket_env`` helpers), ``ctx.extras``
  carries values between modules (for example ``ctx.extras["api"]``).
* account-level modules (``infra.stacks.tooling``, ``infra.stacks.pipeline``) expose
  ``add_to_app(app: aws_cdk.App, shared: Mapping, stages: dict[str, StageContext]) -> None``.

A module that does not exist yet is skipped; an import error *inside* an existing module fails
the synth.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aws_cdk as cdk  # noqa: E402

from finplan_platform.core.config import ENVIRONMENTS, load_config, load_shared_config  # noqa: E402
from infra.stacks.common import StageContext  # noqa: E402
from infra.stacks.metadata import MetadataStack  # noqa: E402
from infra.stacks.storage import StorageStack  # noqa: E402

ENV_MODULES = ("infra.stacks.api", "infra.stacks.ingestion")
APP_MODULES = ("infra.stacks.tooling", "infra.stacks.pipeline")


def optional_module(name: str) -> ModuleType | None:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name == name:
            return None
        raise


def stack_name(env: str, part: str) -> str:
    return f"finplan-{env}-financialplanning-{part}"


def build_app(app: cdk.App | None = None, envs: Iterable[str] | None = None, *, env_modules: Iterable[str] = ENV_MODULES, app_modules: Iterable[str] = APP_MODULES) -> cdk.App:
    app = app or cdk.App()
    selected = list(envs) if envs is not None else _selected_envs(app)
    shared = load_shared_config()
    stages: dict[str, StageContext] = {}
    for env in selected:
        cfg = load_config(env)
        stage = cdk.Stage(app, env.capitalize())
        storage = StorageStack(stage, "Storage", cfg=cfg, stack_name=stack_name(env, "storage"))
        metadata = MetadataStack(stage, "Metadata", cfg=cfg, storage=storage, stack_name=stack_name(env, "metadata"))
        ctx = StageContext(cfg=cfg, shared=shared, storage=storage, metadata=metadata)
        for name in env_modules:
            mod = optional_module(name)
            if mod is not None:
                mod.add_to_stage(stage, ctx)
        stages[env] = ctx
    for name in app_modules:
        mod = optional_module(name)
        if mod is not None:
            mod.add_to_app(app, shared, stages)
    return app


def _selected_envs(app: cdk.App) -> list[str]:
    raw = app.node.try_get_context("envs")
    if not raw:
        return list(ENVIRONMENTS)
    envs = [e.strip() for e in str(raw).split(",") if e.strip()]
    unknown = [e for e in envs if e not in ENVIRONMENTS]
    if unknown:
        raise SystemExit(f"unknown environments in -c envs: {unknown}")
    return envs


if __name__ == "__main__":
    build_app().synth()
