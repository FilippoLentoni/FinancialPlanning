"""FinancialPlanning cross-repo contract package (``finplan-contracts``).

``__version__`` is written into ``_version.py`` from ``contracts/VERSION`` at
build time (hatch version hook). Source checkouts without a build fall back to
reading ``contracts/VERSION`` directly, so there is one version source only.
"""

from __future__ import annotations

from pathlib import Path


def _read_version() -> str:
    try:
        from ._version import __version__ as built  # type: ignore[import-not-found]

        return built
    except ImportError:  # pragma: no cover - only in an unbuilt source tree
        pass
    here = Path(__file__).resolve().parent
    for candidate in (here / "data" / "VERSION", *(p / "VERSION" for p in here.parents)):
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()
    return "0.0.0+unknown"


__version__ = _read_version()

__all__ = ["__version__"]
