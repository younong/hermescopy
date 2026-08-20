/**
 * Hermes Dashboard Plugin SDK — typed contract (SPIKE)
 * ====================================================
 *
 * This is the public type surface for ``window.__HERMES_PLUGIN_SDK__`` and
 * ``window.__HERMES_PLUGINS__``, the globals the dashboard host exposes to
 * plugin bundles (see ``web/src/plugins/registry.ts::exposePluginSDK``).
 *
 * STATUS: spike. This file documents the contract and gives plugin authors
 * (in-repo IIFEs and external bundles alike) editor types without bundling
 * their own copies of React / the API client. It is intentionally a
 * hand-authored ambient declaration rather than ``typeof
 * window.__HERMES_PLUGIN_SDK__`` because:
 *   1. The runtime object is assembled from many internal modules
 *      (``@/lib/api``, ``@nous-research/ui``, …). Deriving the type would
 *      leak those internal import paths into the public contract and couple
 *      external plugins to the host's internal module layout.
 *   2. A hand-authored contract is the *versioned API boundary* — changing
 *      it is a deliberate act, visible in review, not an accidental
 *      consequence of refactoring an internal helper.
 *
 * Versioning: bump ``HermesPluginSDK["sdkVersion"]`` (and the
 * ``SDK_CONTRACT_VERSION`` const the host exposes) on any
 * backwards-incompatible change to this surface. Additive changes
 * (new optional fields, new helpers) don't require a major bump.
 *
 * OPEN QUESTIONS for productionising this spike (do not block the auth fix):
 *   - Ship as a published ``@hermes/dashboard-plugin-sdk`` types package, or
 *     keep in-repo and copy into external plugin repos?
 *   - Should the host assert at runtime that a plugin's declared
 *     ``manifest.sdk_version`` is compatible before executing it?
 *   - The ``components`` map is typed loosely as ``Record<string,
 *     ComponentType>`` here; do we want exact per-component prop types
 *     (pulls @nous-research/ui types into the contract) or is the loose
 *     shape the right boundary for external authors?
 */

import type {
  ComponentType,
  Context,
  DependencyList,
  Dispatch,
  EffectCallback,
  ReactNode,
  SetStateAction,
} from "react";
// ---------------------------------------------------------------------------
// Auth-relevant helpers (the surface this PR adds/sanctions)
// ---------------------------------------------------------------------------

/**
 * JSON ``fetch`` for dashboard ``/api/...`` endpoints. Handles auth in both
 * modes (loopback session-token header / gated cookie), throws
 * ``Error("<status>: <body>")`` on non-2xx, and triggers the global
 * 401 → /login redirect in gated mode. Use for all JSON plugin endpoints.
 */
export type FetchJSON = <T = unknown>(
  url: string,
  init?: RequestInit,
  options?: { allowUnauthorized?: boolean },
) => Promise<T>;

/**
 * Authenticated ``fetch`` for NON-JSON endpoints (uploads via ``FormData``,
 * binary/blob downloads). Same auth handling as ``fetchJSON`` but returns
 * the raw ``Response``, does not parse, does not throw on non-2xx, and does
 * not run the 401 redirect. Plugins MUST use this (or ``fetchJSON``) instead
 * of calling ``fetch`` with hand-managed dashboard credentials.
 */
export type AuthedFetch = (url: string, init?: RequestInit) => Promise<Response>;

/**
 * Build an absolute ``ws(s)://`` URL for a dashboard WebSocket endpoint with
 * the correct auth query param for the active mode (single-use ``ticket`` in
 * gated OAuth mode, ``token`` in loopback). Plugins MUST use this for any
 * WebSocket instead of hand-assembling the URL + reading the session token.
 */
export type BuildWsUrl = (
  path: string,
  params?: Record<string, string>,
  options?: { signal?: AbortSignal },
) => Promise<string>;

/**
 * Lower-level: resolve the ``[authParamName, authParamValue]`` pair for a
 * dashboard-relative WebSocket endpoint path. In gated mode the path binds
 * the single-use ticket audience.
 */
export type BuildWsAuthParam = (path: string) => Promise<[string, string]>;

// ---------------------------------------------------------------------------
// Registry surface (window.__HERMES_PLUGINS__)
// ---------------------------------------------------------------------------

export interface PluginRegistry {
  /** Register the plugin's main tab component by manifest name. */
  register(name: string, component: ComponentType<Record<string, never>>): void;
  /** Register a Chat workspace component declared by the plugin manifest. */
  registerWorkspace(
    pluginName: string,
    workspaceId: string,
    component: ComponentType<Record<string, never>>,
  ): void;
  /** Register a component into a named host slot. */
  registerSlot(slot: string, name: string, component: ComponentType): void;
}

// ---------------------------------------------------------------------------
// SDK surface (window.__HERMES_PLUGIN_SDK__)
// ---------------------------------------------------------------------------

export interface HermesPluginSDK {
  /** Contract version of this SDK surface (see SDK_CONTRACT_VERSION). */
  readonly sdkVersion: string;

  /** React core — use instead of importing/bundling react. */
  React: typeof import("react").default;
  reactDom: {
    createPortal: typeof import("react-dom").createPortal;
  };
  hooks: {
    useState: <T>(initial: T | (() => T)) => [T, Dispatch<SetStateAction<T>>];
    useEffect: (effect: EffectCallback, deps?: DependencyList) => void;
    useCallback: <T extends (...args: never[]) => unknown>(callback: T, deps: DependencyList) => T;
    useMemo: <T>(factory: () => T, deps: DependencyList) => T;
    useRef: <T>(initial: T) => { current: T };
    useContext: <T>(context: Context<T>) => T;
    createContext: <T>(defaultValue: T) => Context<T>;
  };

  /**
   * Typed convenience client for core dashboard endpoints. Typed permissively
   * at the boundary (methods vary in arity and return type — most return
   * ``Promise<T>``, a few return a URL string synchronously); plugins call the
   * specific methods they need. See ``web/src/lib/api.ts`` for the concrete shape.
   */
  api: Record<string, (...args: never[]) => unknown>;

  /** JSON fetch with host auth handling. */
  fetchJSON: FetchJSON;
  /** Authenticated raw fetch for uploads / blob downloads. */
  authedFetch: AuthedFetch;
  /** Build an auth'd WebSocket URL for the active mode. */
  buildWsUrl: BuildWsUrl;
  /** Resolve just the WS auth query-param pair. */
  buildWsAuthParam: BuildWsAuthParam;

  /**
   * Shared UI primitives (Nous DS / shadcn). Typed permissively at the
   * boundary: the host's concrete components (some of which require props like
   * ``active``/``value``/``name``) must be assignable here, and external plugin
   * authors render them dynamically without the host's internal prop types.
   * ``ComponentType<any>`` intentionally keeps component names public without
   * leaking or falsely constraining the host's internal prop types.
   */
  components: Record<string, ComponentType<any>>; // eslint-disable-line @typescript-eslint/no-explicit-any

  /** Generic host hooks available to plugin-owned pages. */
  useConfirmDelete: <TId>(options: {
    onDelete: (id: TId) => Promise<void>;
  }) => {
    cancel: () => void;
    confirm: () => Promise<void>;
    isDeleting: boolean;
    isOpen: boolean;
    pendingId: TId | null;
    requestDelete: (id: TId) => void;
  };
  usePageHeader: () => {
    setAfterTitle: (node: ReactNode) => void;
    setEnd: (node: ReactNode) => void;
    setTitle: (title: string | null) => void;
  };
  useToast: (duration?: number) => {
    showToast: (message: string, type: "error" | "success") => void;
    toast: { message: string; type: "error" | "success" } | null;
  };

  /** Host-provided icon components so plugin bundles do not ship lucide/react. */
  icons: Record<string, ComponentType<{ className?: string }>>;

  utils: {
    cn: (...classes: Array<string | false | null | undefined>) => string;
    /** Relative-time formatter. Accepts an epoch-ms number. */
    timeAgo: (ts: number) => string;
    /** Relative-time formatter for an ISO-8601 string. */
    isoTimeAgo: (iso: string) => string;
  };

  /**
   * i18n hook. Returns the host's locale plus a deliberately loose translation
   * tree so the public SDK does not import or mirror the host's internal
   * ``I18nContextValue`` shape. Plugins typically call ``useI18n().t(...)``.
   */
  useI18n: () => {
    locale: string;
    t: any; // eslint-disable-line @typescript-eslint/no-explicit-any
  };
}

declare global {
  interface Window {
    __HERMES_PLUGIN_SDK__?: HermesPluginSDK;
    __HERMES_PLUGINS__?: PluginRegistry;
  }
}

export {};
