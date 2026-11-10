"""Forgebox dashboard plugin — backend API routes mounted at /api/plugins/forgebox/.

The Hermes dashboard imports ``dashboard/plugin_api.py`` directly (by path),
so this shim exists only to load the canonical implementation from the
packaged ``src/forgebox/plugin_api.py`` — the single source of truth shared by
the CLI, the test suite, and the REST surface. Loading by file path keeps the
dashboard working even when the ``forgebox`` package is not installed into the
active environment.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src"
_PLUGIN_API = _SOURCE_ROOT / "forgebox" / "plugin_api.py"

# Catalog installs copy the plugin tree without installing a Python distribution.
# Keep its source root available for deferred imports made by request handlers.
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))


def _load_impl():
    """Load src/forgebox/plugin_api.py under a unique module name."""
    spec = importlib.util.spec_from_file_location(
        "forgebox_dashboard_plugin_api", _PLUGIN_API
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {_PLUGIN_API}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_impl = _load_impl()
router = _impl.router

