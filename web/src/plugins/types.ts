/** Types for the dashboard plugin system. */

import type { PluginManifestResponse } from "@/lib/api";

export type PluginWorkspaceManifest = NonNullable<
  PluginManifestResponse["chat"]
>["workspaces"][number];

export type PluginManifest = PluginManifestResponse;
