"""Find and import the planner core (``planner_core``, standard library only).

The core lives at ``custom_components/solar_ev_planner/core/`` once the planner integration
is in the repository. Until then, point ``PLANNER_CORE_PATH`` at a ``planner_core`` folder.
The folder is loaded on its own, as the package ``planner_core``, so the integration's
``__init__.py`` (which imports Home Assistant) never runs: the tools work in any Python 3.14
with aiohttp.

Search order: ``PLANNER_CORE_PATH``, the integration's ``core`` folder, then an importable
``planner_core``.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType

REPO = Path(__file__).resolve().parent.parent
INTEGRATED = REPO / "custom_components" / "solar_ev_planner" / "core"
PACKAGE = "planner_core"


class CoreNotFound(ImportError):
    """No planner core in any known location."""


def candidates() -> list[Path]:
    out = []
    if env := os.environ.get("PLANNER_CORE_PATH"):
        out.append(Path(env).expanduser())
    out.append(INTEGRATED)
    return out


def _load_folder(folder: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        PACKAGE, folder / "__init__.py", submodule_search_locations=[str(folder)]
    )
    if spec is None or spec.loader is None:
        raise CoreNotFound(f"cannot load the planner core from {folder}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(PACKAGE, None)
        raise
    return module


def load_core() -> ModuleType:
    """The ``planner_core`` package (cached in ``sys.modules`` after the first load)."""
    if (loaded := sys.modules.get(PACKAGE)) is not None:
        return loaded
    for folder in candidates():
        if (folder / "__init__.py").is_file():
            return _load_folder(folder)
    try:
        return importlib.import_module(PACKAGE)
    except ImportError:
        pass
    where = INTEGRATED.relative_to(REPO) if INTEGRATED.is_relative_to(REPO) else INTEGRATED
    raise CoreNotFound(
        "The planner core was not found. Set PLANNER_CORE_PATH to a planner_core folder, "
        f"or install the planner integration so {where} exists."
    )


def core_available() -> bool:
    try:
        load_core()
    except ImportError:
        return False
    return True
