"""Explicit descriptor-backed workspace context for authenticated owner workers.

The context carries an already-open ``ControlledRoots`` capability set.  It is
attached to an owner worker's immutable gateway runtime rather than derived from
an ambient cwd, environment variable, or a user-provided path.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes_cli.controlled_roots import ControlledRoots


@dataclass(frozen=True)
class AuthenticatedWorkspaceContext:
    """One authenticated worker's fixed default-workspace capability.

    The prefix is established during trusted worker construction, never from a
    tool call, browser input, environment variable, or session state.
    """

    roots: ControlledRoots
    workspace_prefix: str = "default"

    def __post_init__(self) -> None:
        prefix = self.workspace_prefix
        if not isinstance(prefix, str) or not prefix or prefix.startswith("/") or "\x00" in prefix:
            raise ValueError("workspace_prefix must be a non-empty relative path")
        components = tuple(prefix.split("/"))
        if any(component in {"", ".", ".."} for component in components):
            raise ValueError("workspace_prefix must not contain empty, dot, or parent components")
