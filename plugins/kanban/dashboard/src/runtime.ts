import { copyForLocale } from "./translations";

const sdk = window.__HERMES_PLUGIN_SDK__;
const registry = window.__HERMES_PLUGINS__;
if (!sdk || !registry) throw new Error("Hermes plugin SDK is unavailable");
const hostSdk = sdk;
const hostRegistry = registry;

export const React = hostSdk.React;
export const { useCallback, useEffect, useMemo, useRef, useState } = hostSdk.hooks;
export const { createPortal } = hostSdk.reactDom;
export const { Markdown } = hostSdk.components as Record<string, any>;
export const {
  AlertTriangle, Archive, CheckSquare2, ChevronDown, CirclePlus, Download,
  GripVertical, MessageSquareText, Network, Paperclip, Pencil, Plus, RefreshCw,
  Search, Settings2, Trash2, Upload, WandSparkles, X, Zap,
} = hostSdk.icons as Record<string, any>;
export const { authedFetch, buildWsUrl, fetchJSON } = hostSdk;
export { hostRegistry as registry };

export function useI18n(): { locale: string; t: { common: Record<string, string>; kanban: Record<string, any> } } {
  const host = hostSdk.useI18n() as { locale?: string; t?: { common?: Record<string, string> } };
  const locale = host.locale || "en";
  return { locale, t: { common: host.t?.common || {}, kanban: copyForLocale(locale) } };
}

export function kanbanTranslations(t: { kanban: Record<string, any> }): Record<string, any> {
  return t.kanban;
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0B";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  const rounded = unit === 0 ? Math.round(value) : value >= 10 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${rounded}${units[unit]}`;
}

export function triggerDownload(url: string, filename: string): void {
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noreferrer";
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  link.remove();
}
