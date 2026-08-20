/**
 * Dashboard Plugin SDK + Registry
 *
 * Exposes React, UI components, hooks, and utilities on the window so
 * that plugin bundles can use them without bundling their own copies.
 *
 * Plugins call window.__HERMES_PLUGINS__.register(name, Component)
 * to register their tab component.
 */

import React, {
  useState,
  useEffect,
  useCallback,
  useMemo,
  useRef,
  useContext,
  createContext,
} from "react";
import { api, fetchJSON, authedFetch, buildWsUrl, buildWsAuthParam } from "@/lib/api";
import { cn, timeAgo, isoTimeAgo } from "@/lib/utils";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Checkbox } from "@nous-research/ui/ui/components/checkbox";
import { Select, SelectOption } from "@nous-research/ui/ui/components/select";
import { Card, CardHeader, CardTitle, CardContent } from "@nous-research/ui/ui/components/card";
import { ConfirmDialog } from "@nous-research/ui/ui/components/confirm-dialog";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@nous-research/ui/ui/components/dialog";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Segmented } from "@nous-research/ui/ui/components/segmented";
import { Separator } from "@nous-research/ui/ui/components/separator";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Tabs, TabsList, TabsTrigger } from "@nous-research/ui/ui/components/tabs";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { useConfirmDelete } from "@nous-research/ui/hooks/use-confirm-delete";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { usePageHeader } from "@/contexts/usePageHeader";
import { useI18n } from "@/i18n";
import { registerSlot, PluginSlot } from "./slots";

// ---------------------------------------------------------------------------
// Plugin registry — plugins call register() to add their component.
// ---------------------------------------------------------------------------

type RegistryListener = () => void;

const _registered: Map<string, React.ComponentType> = new Map();
const _loadErrors: Map<string, string> = new Map();
const _listeners: Set<RegistryListener> = new Set();

function _notify() {
  for (const fn of _listeners) {
    try { fn(); } catch { /* ignore */ }
  }
}

/** Register a plugin component. Called by plugin JS bundles. */
function registerPlugin(name: string, component: React.ComponentType) {
  _loadErrors.delete(name);
  _registered.set(name, component);
  _notify();
}

/** Get a registered component by plugin name. */
export function getPluginComponent(name: string): React.ComponentType | undefined {
  return _registered.get(name);
}

export function getPluginLoadError(name: string): string | undefined {
  return _loadErrors.get(name);
}

export function setPluginLoadError(name: string, message: string) {
  _loadErrors.set(name, message);
  _notify();
}

/** Subscribe to registry changes (returns unsubscribe fn). */
export function onPluginRegistered(fn: RegistryListener): () => void {
  _listeners.add(fn);
  return () => _listeners.delete(fn);
}

/** Get current count of registered plugins. */
export function getRegisteredCount(): number {
  return _registered.size;
}

// ---------------------------------------------------------------------------
// Expose SDK + registry on window
// ---------------------------------------------------------------------------

/**
 * Version of the plugin SDK contract (see ``plugins/sdk.d.ts``). Bump the
 * major on any backwards-incompatible change to the exposed surface;
 * additive changes (new optional fields / helpers) don't require a bump.
 * Exposed at runtime as ``window.__HERMES_PLUGIN_SDK__.sdkVersion`` so a
 * plugin (or a future host-side compatibility gate) can read it.
 */
export const SDK_CONTRACT_VERSION = "1.2.0";

// Window globals for the plugin SDK are declared in ``plugins/sdk.d.ts`` —
// the single source of truth for the public contract. Don't redeclare them
// here (duplicate ambient declarations with differing modifiers conflict).

export function exposePluginSDK() {
  window.__HERMES_PLUGINS__ = {
    register: registerPlugin,
    registerSlot,
  };

  window.__HERMES_PLUGIN_SDK__ = {
    // Contract version of the plugin SDK surface (see plugins/sdk.d.ts).
    // Bump on backwards-incompatible changes; additive changes don't need it.
    sdkVersion: SDK_CONTRACT_VERSION,
    // React core — plugins use these instead of importing react
    React,
    hooks: {
      useState,
      useEffect,
      useCallback,
      useMemo,
      useRef,
      useContext,
      createContext,
    },

    // Hermes API client
    api,
    // Raw fetchJSON for plugin-specific JSON endpoints
    fetchJSON,
    // Authenticated fetch for non-JSON endpoints (uploads / blob downloads).
    // Handles loopback-token vs gated-cookie auth so plugins never read
    // browser-managed dashboard credentials directly.
    authedFetch,
    // Build a ws(s):// URL with the correct auth param for the active mode
    // (single-use ticket in gated mode, token in loopback). Use this for any
    // plugin WebSocket instead of hand-assembling the URL.
    buildWsUrl,
    // Lower-level: resolve just the [authParamName, authParamValue] pair, for
    // plugins that need to build the WS URL themselves.
    buildWsAuthParam,

    // UI components — Nous DS where available, shadcn/ui primitives elsewhere.
    components: {
      Card,
      CardHeader,
      CardTitle,
      CardContent,
      Badge,
      Button,
      Checkbox,
      ConfirmDialog,
      Dialog,
      DialogContent,
      DialogHeader,
      DialogTitle,
      H2,
      Input,
      Label,
      PluginSlot,
      Segmented,
      Select,
      SelectOption,
      Separator,
      Spinner,
      Tabs,
      TabsList,
      TabsTrigger,
      Toast,
    },

    // Utilities
    utils: { cn, timeAgo, isoTimeAgo },

    // Hooks
    useConfirmDelete,
    useI18n,
    usePageHeader,
    useToast,
  };
}
