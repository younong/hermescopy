import { useSyncExternalStore } from "react";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { useI18n } from "@/i18n";
import { formatPluginError } from "./errors";
import {
  getPluginLoadError,
  getPluginRegistryRevision,
  getPluginWorkspaceComponent,
  onPluginRegistered,
} from "./registry";

/** Renders a Chat workspace once its plugin bundle has registered it. */
export function ChatPluginWorkspace({
  pluginName,
  workspaceId,
}: {
  pluginName: string;
  workspaceId: string;
}) {
  const { t } = useI18n();
  useSyncExternalStore(
    onPluginRegistered,
    getPluginRegistryRevision,
    () => 0,
  );
  const Component = getPluginWorkspaceComponent(pluginName, workspaceId);
  const loadError = getPluginLoadError(pluginName);

  if (Component) return <Component />;

  if (loadError) {
    return (
      <div className="gui-chat-workspace-feedback is-error" role="alert">
        {formatPluginError(loadError, t)}
      </div>
    );
  }

  return (
    <div className="gui-chat-workspace-empty" role="status">
      <Spinner className="shrink-0" />
      <span>{t.common.loading}</span>
    </div>
  );
}
