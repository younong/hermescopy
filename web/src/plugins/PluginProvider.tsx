import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { api, HERMES_BASE_PATH } from "@/lib/api";
import type { PluginManifest } from "./types";
import {
  getPluginComponent,
  getPluginLoadError,
  getPluginWorkspaceComponent,
  onPluginRegistered,
  setPluginLoadError,
} from "./registry";

export type PluginMode = "admin" | "member";

interface PluginContextValue {
  manifests: PluginManifest[];
  loading: boolean;
}

const PluginContext = createContext<PluginContextValue | null>(null);

export function PluginProvider({
  children,
  mode,
}: {
  children: ReactNode;
  mode: PluginMode;
}) {
  const [manifests, setManifests] = useState<PluginManifest[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setManifests([]);
    api
      .getPlugins()
      .then((list) => {
        if (cancelled) return;
        const visible = mode === "member"
          ? list.flatMap((manifest) => {
              const workspaces = manifest.chat?.workspaces.filter(
                (workspace) => !workspace.admin_only,
              ) ?? [];
              return workspaces.length > 0
                ? [{ ...manifest, chat: { workspaces }, tab: undefined }]
                : [];
            })
          : list;
        setManifests(visible);
        if (visible.length === 0) setLoading(false);
      }, () => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [mode]);

  useEffect(() => {
    if (manifests.length === 0) return;

    const updateLoading = () => {
      if (manifests.every(isManifestTerminal)) setLoading(false);
    };
    const unsubscribe = onPluginRegistered(updateLoading);
    updateLoading();

    for (const manifest of manifests) {
      if (isManifestTerminal(manifest)) continue;
      if (manifest.css) loadPluginCss(manifest.name, manifest.css);
      loadPluginScript(manifest);
    }
    return unsubscribe;
  }, [manifests]);


  const value = useMemo(
    () => ({ manifests, loading }),
    [loading, manifests],
  );

  return <PluginContext.Provider value={value}>{children}</PluginContext.Provider>;
}

export function usePlugins(): PluginContextValue {
  const value = useContext(PluginContext);
  if (!value) throw new Error("usePlugins must be used within PluginProvider");
  return value;
}

function loadPluginCss(pluginName: string, css: string) {
  const url = pluginAssetUrl(pluginName, css);
  if (document.querySelector(`link[data-hermes-plugin-css="${url}"]`)) return;

  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = url;
  link.setAttribute("data-hermes-plugin", pluginName);
  link.setAttribute("data-hermes-plugin-css", url);
  document.head.appendChild(link);
}

function loadPluginScript(manifest: PluginManifest) {
  const url = pluginAssetUrl(manifest.name, manifest.entry);
  if (document.querySelector(`script[data-hermes-plugin-src="${url}"]`)) return;

  const script = document.createElement("script");
  script.setAttribute("data-hermes-plugin", manifest.name);
  script.setAttribute("data-hermes-plugin-src", url);
  script.src = url;
  script.async = true;
  if (manifest.integrity) {
    script.integrity = manifest.integrity;
    script.crossOrigin = "anonymous";
  }
  script.onerror = () => {
    setPluginLoadError(manifest.name, "LOAD_FAILED");
    console.warn(`[plugins] Failed to load ${manifest.name} from ${url} (open Network tab)`);
  };
  script.onload = () => {
    queueMicrotask(() => {
      if (isManifestRegistered(manifest)) return;
      setPluginLoadError(manifest.name, "NO_REGISTER");
    });
  };
  document.body.appendChild(script);
}

function isManifestRegistered(manifest: PluginManifest): boolean {
  if (manifest.tab && !getPluginComponent(manifest.name)) return false;
  return (manifest.chat?.workspaces ?? []).every((workspace) =>
    Boolean(getPluginWorkspaceComponent(manifest.name, workspace.id)),
  );
}

function isManifestTerminal(manifest: PluginManifest): boolean {
  return isManifestRegistered(manifest) || Boolean(getPluginLoadError(manifest.name));
}

function pluginAssetUrl(pluginName: string, asset: string): string {
  return `${HERMES_BASE_PATH}/dashboard-plugins/${pluginName}/${asset}`;
}
