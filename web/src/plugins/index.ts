export {
  exposePluginSDK,
  getPluginComponent,
  getPluginWorkspaceComponent,
  onPluginRegistered,
  getRegisteredCount,
} from "./registry";
export { ChatPluginWorkspace } from "./ChatPluginWorkspace";
export { PluginPage } from "./PluginPage";
export { PluginProvider, usePlugins } from "./PluginProvider";
export { resolvePluginIcon } from "./icons";
export { PluginSlot, KNOWN_SLOT_NAMES, registerSlot, getSlotEntries, onSlotRegistered, unregisterPluginSlots } from "./slots";
export type { KnownSlotName } from "./slots";
export type { PluginManifest, PluginWorkspaceManifest } from "./types";
