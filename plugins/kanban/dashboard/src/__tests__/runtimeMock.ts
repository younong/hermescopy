import React from "react";
import { createPortal } from "react-dom";
import { vi } from "vitest";
import { copyForLocale } from "../translations";

const Icon = (props: Record<string, unknown>) => React.createElement("svg", props);

const runtimeMocks = vi.hoisted(() => ({
  authedFetch: vi.fn(),
  buildWsUrl: vi.fn(),
  fetchJSON: vi.fn(),
  register: vi.fn(),
  registerWorkspace: vi.fn(),
}));

vi.mock("../runtime", () => ({
  React,
  createPortal,
  useCallback: React.useCallback,
  useEffect: React.useEffect,
  useMemo: React.useMemo,
  useRef: React.useRef,
  useState: React.useState,
  Markdown: ({ children, content }: { children?: React.ReactNode; content?: string }) => React.createElement("div", null, children ?? content),
  AlertTriangle: Icon,
  Archive: Icon,
  CheckSquare2: Icon,
  ChevronDown: Icon,
  CirclePlus: Icon,
  Download: Icon,
  GripVertical: Icon,
  MessageSquareText: Icon,
  Network: Icon,
  Paperclip: Icon,
  Pencil: Icon,
  Plus: Icon,
  RefreshCw: Icon,
  Search: Icon,
  Settings2: Icon,
  Trash2: Icon,
  Upload: Icon,
  WandSparkles: Icon,
  X: Icon,
  Zap: Icon,
  authedFetch: runtimeMocks.authedFetch,
  buildWsUrl: runtimeMocks.buildWsUrl,
  fetchJSON: runtimeMocks.fetchJSON,
  registry: {
    register: runtimeMocks.register,
    registerWorkspace: runtimeMocks.registerWorkspace,
  },
  useI18n: () => ({
    locale: "en",
    t: {
      common: { cancel: "Cancel", close: "Close", delete: "Delete", refresh: "Refresh" },
      kanban: copyForLocale("en"),
    },
  }),
  kanbanTranslations: (t: { kanban: Record<string, unknown> }) => t.kanban,
  formatBytes: (bytes: number) => `${bytes}B`,
  triggerDownload: vi.fn(),
}));

export { runtimeMocks };
