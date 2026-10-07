#!/usr/bin/env python3
"""Build gate: the market-data provider and calendar libraries are pinned exactly (ING-14, ING-15).

Checks, all offline:

1. ``pyproject.toml`` declares ``yfinance`` and ``exchange-calendars`` with an exact ``==``
   pin (no range, no wildcard) wherever they appear (dependencies, optional extras,
   dependency groups);
2. ``uv.lock`` resolves each to exactly that version;
3. the installed distribution (when installed) has that version;
4. the shipped XNYS calendar's version names ``exchange_calendars`` and the pinned version.

Exit code 1 lists every problem. The build stage runs this next to
``scripts/generate_calendar.py --check``.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PINNED_LIBRARIES = ("yfinance", "exchange-calendars")
_REQ = re.compile(r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(?P<spec>[^;]*)(;.*)?$")
_EXACT = re.compile(r"^==\s*(?P<v>[0-9][0-9A-Za-z.+!-]*)\s*$")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirements(doc: dict[str, Any]) -> Iterable[tuple[str, str]]:
    project = doc.get("project", {})
    for req in project.get("dependencies", []) or []:
        yield "project.dependencies", req
    for extra, reqs in (project.get("optional-dependencies") or {}).items():
        for req in reqs:
            yield f"project.optional-dependencies.{extra}", req
    for group, reqs in (doc.get("dependency-groups") or {}).items():
        for req in reqs:
            if isinstance(req, str):
                yield f"dependency-groups.{group}", req


def pin_problems(pyproject: Path) -> tuple[dict[str, str], list[str]]:
    doc = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    pins: dict[str, str] = {}
    problems: list[str] = []
    seen: dict[str, list[str]] = {lib: [] for lib in PINNED_LIBRARIES}
    for where, req in _requirements(doc):
        m = _REQ.match(req)
        if not m:
            continue
        name = _norm(m.group("name"))
        if name not in seen:
            continue
        seen[name].append(where)
        exact = _EXACT.match(m.group("spec").strip())
        if not exact:
            problems.append(f"{where}: {req!r} is not an exact '==' pin")
            continue
        v = exact.group("v")
        if "*" in v:
            problems.append(f"{where}: {req!r} uses a wildcard")
            continue
        if name in pins and pins[name] != v:
            problems.append(f"{name} is pinned to both {pins[name]} and {v}")
        pins.setdefault(name, v)
    for lib, places in seen.items():
        if not places:
            problems.append(f"{lib} is not declared in {pyproject.name}")
    return pins, problems


def pinned_versions(pyproject: Path) -> dict[str, str]:
    pins, _ = pin_problems(pyproject)
    return pins


def lock_versions(lock: Path) -> dict[str, str]:
    doc = tomllib.loads(lock.read_text(encoding="utf-8"))
    return {_norm(p["name"]): str(p.get("version")) for p in doc.get("package", []) if "name" in p}


def check(pyproject: Path, lock: Path, calendar_dir: Path | None = None, *, installed: bool = True) -> list[str]:
    pins, problems = pin_problems(pyproject)
    if lock.is_file():
        locked = lock_versions(lock)
        for lib in PINNED_LIBRARIES:
            if lib in pins and locked.get(lib) != pins[lib]:
                problems.append(f"uv.lock resolves {lib} to {locked.get(lib)!r}, not the pin {pins[lib]}")
    else:
        problems.append(f"{lock} is missing")
    if installed:
        from importlib.metadata import PackageNotFoundError, version

        for lib in PINNED_LIBRARIES:
            try:
                have = version(lib)
            except PackageNotFoundError:
                continue
            if lib in pins and have != pins[lib]:
                problems.append(f"installed {lib} {have} differs from the pin {pins[lib]}")
    if calendar_dir is not None and "exchange-calendars" in pins:
        shipped = sorted(calendar_dir.glob("xnys-*.json"))
        want = f"xnys-exchange_calendars-{pins['exchange-calendars']}-"
        if len(shipped) != 1:
            problems.append(f"expected one shipped XNYS calendar, found {[p.name for p in shipped]}")
        elif not shipped[0].stem.startswith(want):
            problems.append(f"shipped calendar {shipped[0].name} does not name the pinned exchange_calendars {pins['exchange-calendars']}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pyproject", type=Path, default=ROOT / "pyproject.toml")
    ap.add_argument("--lock", type=Path, default=ROOT / "uv.lock")
    ap.add_argument("--calendar-dir", type=Path, default=ROOT / "platform" / "finplan_platform" / "data" / "calendars")
    args = ap.parse_args(argv)
    problems = check(args.pyproject, args.lock, args.calendar_dir)
    for p in problems:
        print(f"FAIL: {p}", file=sys.stderr)
    if not problems:
        pins = pinned_versions(args.pyproject)
        print("ok: " + ", ".join(f"{k}=={v}" for k, v in sorted(pins.items())))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
