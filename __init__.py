"""Forgebox — Hermes plugin entry point.

Registers best-effort path/cwd redirection and tool/approval observation.
Shell source stays unchanged: this plugin is not an OS security sandbox.
Applying changes is an explicit CLI or dashboard/Desktop operation.

All heavy lifting lives in ``src/forgebox/``; this module only adapts it to
the Hermes plugin context.
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
# Make submodules resolve into src/forgebox/ (this file is the package
# __init__; src/forgebox has no separate package identity).
__path__ = [os.path.join(_SRC, "forgebox")] + list(globals().get("__path__", []))


def register(ctx):
    """Register Forgebox hooks with the Hermes plugin context."""
    from forgebox.hooks import (
        post_approval_response,
        post_tool_call,
        pre_tool_call,
    )

    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("post_approval_response", post_approval_response)

