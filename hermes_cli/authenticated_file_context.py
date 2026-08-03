"""Explicit descriptor-backed workspace context for authenticated owner workers.

The context carries an already-open ``ControlledRoots`` capability set.  It is
attached to an owner worker's immutable gateway runtime rather than derived from
an ambient cwd, environment variable, or a user-provided path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hermes_cli.controlled_roots import ControlledRoots, RootKind


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

    def controlled_workspace_path(
        self,
        path: str,
        *,
        allow_workspace_root: bool = False,
    ) -> str:
        """Map a model-visible workspace path below the fixed capability root."""
        if not isinstance(path, str) or not path or "\x00" in path:
            raise ValueError("path must be a non-empty workspace-relative path")

        sandbox_root = "/workspace"
        if path == sandbox_root:
            if allow_workspace_root:
                return self.workspace_prefix
            raise ValueError("path must identify an entry below /workspace")
        if path.startswith(f"{sandbox_root}/"):
            path = path[len(sandbox_root) + 1 :]
        elif path.startswith(("/", "~")):
            raise ValueError("path must be workspace-relative or below /workspace")

        components = tuple(path.split("/"))
        if any(component in {"", ".", ".."} for component in components):
            raise ValueError("path must not contain empty, dot, or parent components")
        return f"{self.workspace_prefix}/{path}"

    def controlled_api_path(
        self,
        path: str | None,
        *,
        allow_workspace_root: bool = False,
    ) -> str:
        """Map one API path into this context's fixed workspace capability."""
        value = str(path or "").strip()
        if not value:
            if allow_workspace_root:
                return self.workspace_prefix
            raise ValueError("path must identify an entry in the workspace")
        if value == "/workspace" or value.startswith("/workspace/"):
            return self.controlled_workspace_path(
                value,
                allow_workspace_root=allow_workspace_root,
            )

        candidate = Path(value)
        if candidate.is_absolute():
            workspace = self.workspace_path
            if candidate == workspace and allow_workspace_root:
                return self.workspace_prefix
            try:
                value = candidate.relative_to(workspace).as_posix()
            except ValueError as exc:
                raise ValueError("absolute path is outside the authenticated workspace") from exc
        return self.controlled_workspace_path(
            value,
            allow_workspace_root=allow_workspace_root,
        )

    def visible_workspace_path(self, controlled_path: str) -> str:
        """Return a selected-workspace-relative API path."""
        prefix = f"{self.workspace_prefix}/"
        if controlled_path == self.workspace_prefix:
            return ""
        if not controlled_path.startswith(prefix):
            raise ValueError("controlled path is outside the authenticated workspace")
        return controlled_path[len(prefix) :]

    @property
    def workspace_path(self) -> Path:
        """Return the canonical path used only for diagnostics and libraries."""
        root = self.roots.get(RootKind.WORKSPACE).canonical_path
        return root / self.workspace_prefix

    def diagnostic_path(self, controlled_path: str) -> Path:
        """Return a diagnostic path after validating the controlled prefix."""
        visible = self.visible_workspace_path(controlled_path)
        return self.workspace_path if not visible else self.workspace_path / visible
