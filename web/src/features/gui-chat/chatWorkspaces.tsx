import type { ComponentType, ReactNode } from "react";
import { ChatPluginWorkspace, resolvePluginIcon, type PluginManifest } from "@/plugins";

export interface ChatWorkspaceDefinition {
  id: string;
  path: string;
  label: string;
  description: string;
  icon: ComponentType<{ className?: string }>;
  pluginName?: string;
  render?: () => ReactNode;
}

function insertWorkspace(
  workspaces: ChatWorkspaceDefinition[],
  workspace: ChatWorkspaceDefinition,
  position: string | number,
) {
  if (typeof position === "number") {
    workspaces.splice(Math.max(0, Math.min(position, workspaces.length)), 0, workspace);
    return;
  }
  if (position.startsWith("before:") || position.startsWith("after:")) {
    const after = position.startsWith("after:");
    const target = position.slice(after ? 6 : 7);
    const index = workspaces.findIndex((candidate) => candidate.id === target);
    workspaces.splice(index < 0 ? workspaces.length : index + (after ? 1 : 0), 0, workspace);
    return;
  }
  workspaces.push(workspace);
}

export function buildChatWorkspaces(
  coreWorkspaces: ChatWorkspaceDefinition[],
  manifests: PluginManifest[],
): ChatWorkspaceDefinition[] {
  const ordered = [...coreWorkspaces];
  const occupiedIds = new Set(ordered.map((workspace) => workspace.id));
  const occupiedPaths = new Set(ordered.map((workspace) => workspace.path));

  for (const manifest of manifests) {
    for (const workspace of manifest.chat?.workspaces ?? []) {
      if (occupiedIds.has(workspace.id) || occupiedPaths.has(workspace.path)) continue;
      occupiedIds.add(workspace.id);
      occupiedPaths.add(workspace.path);
      insertWorkspace(ordered, {
        id: workspace.id,
        path: workspace.path,
        label: workspace.label,
        description: workspace.description,
        icon: resolvePluginIcon(workspace.icon),
        pluginName: manifest.name,
        render: () => (
          <ChatPluginWorkspace
            pluginName={manifest.name}
            workspaceId={workspace.id}
          />
        ),
      }, workspace.position);
    }
  }
  return ordered;
}
