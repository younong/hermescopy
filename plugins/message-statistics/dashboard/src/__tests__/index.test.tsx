// @vitest-environment jsdom

import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getSessionStats = vi.fn();
const getSessions = vi.fn();
const getSessionComposition = vi.fn();
let workspace: React.ComponentType | undefined;

beforeEach(async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML = "";
  workspace = undefined;
  getSessionStats.mockResolvedValue({
    total: 3,
    active_store: 1,
    archived: 1,
    messages: 12,
    by_source: { cli: 2, telegram: 1 },
  });
  getSessions.mockResolvedValue({
    sessions: [
      {
        id: "session-a",
        title: "Recent chat",
        source: "cli",
        message_count: 7,
        last_active: 1,
      },
    ],
    total: 1,
  });
  getSessionComposition.mockResolvedValue({
    charts: [
      {
        id: "roles",
        label: "Message roles",
        availability: "available",
        accuracy: "exact_count",
        unit: "messages",
        total: 7,
        known_total: 7,
        segments: [
          {
            id: "user",
            label: "User",
            value: 3,
            percentage: 42.9,
            unit: "messages",
            status: "exact",
          },
          {
            id: "assistant",
            label: "Assistant",
            value: 4,
            percentage: 57.1,
            unit: "messages",
            status: "exact",
          },
        ],
        limitations: [],
        coverage: { included_sessions: 1, requested_sessions: 1 },
      },
    ],
    limitations: [],
    coverage: { included_sessions: 1, requested_sessions: 1 },
  });

  const Button = ({ children, onClick }: React.PropsWithChildren<{ onClick?: () => void }>) => (
    <button onClick={onClick}>{children}</button>
  );
  const Badge = ({ children }: React.PropsWithChildren) => <span>{children}</span>;
  const Checkbox = ({ checked, onClick, ...props }: { checked?: boolean; onClick?: () => void; [key: string]: unknown }) => (
    <button aria-checked={checked} onClick={onClick} role="checkbox" {...props} />
  );

  window.__HERMES_PLUGIN_SDK__ = {
    React,
    hooks: {
      useState: React.useState,
      useEffect: React.useEffect,
      useCallback: React.useCallback,
      useMemo: React.useMemo,
      useRef: React.useRef,
      useContext: React.useContext,
      createContext: React.createContext,
    },
    api: { getSessionStats, getSessions, getSessionComposition },
    components: { Badge, Button, Checkbox, Input: () => null },
    useI18n: () => ({ locale: "zh" }),
    utils: { timeAgo: () => "刚刚", cn: () => "", isoTimeAgo: () => "" },
  } as never;
  window.__HERMES_PLUGINS__ = {
    register: vi.fn(),
    registerSlot: vi.fn(),
    registerWorkspace: (pluginName, workspaceId, component) => {
      expect(pluginName).toBe("message-statistics");
      expect(workspaceId).toBe("statistics");
      workspace = component;
    },
  };

  vi.resetModules();
  await import("../index.tsx");
});

afterEach(() => {
  delete window.__HERMES_PLUGIN_SDK__;
  delete window.__HERMES_PLUGINS__;
  vi.clearAllMocks();
  document.body.innerHTML = "";
});

describe("message-statistics workspace", () => {
  it("registers, loads bounded authenticated-reader data, localizes, and renders composition", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const Workspace = workspace!;

    await act(async () => {
      root.render(<Workspace />);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(getSessionStats).toHaveBeenCalledOnce();
    expect(getSessions).toHaveBeenCalledWith(
      50,
      0,
      "recent",
      true,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(container.textContent).toContain("消息统计");
    expect(container.textContent).toContain("telegram · 1");
    expect(container.textContent).toContain("Recent chat");

    await act(async () => {
      container.querySelector<HTMLElement>('[role="checkbox"]')?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(getSessionComposition).toHaveBeenCalledWith(
      ["session-a"],
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(container.textContent).toContain("Message roles");
    expect(container.textContent).toContain("覆盖范围: 1/1");
    await act(async () => root.unmount());
  });

  it("offers retry after a reader error", async () => {
    getSessionStats.mockRejectedValueOnce(new Error("reader unavailable"));
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const Workspace = workspace!;

    await act(async () => {
      root.render(<Workspace />);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.querySelector('[role="alert"]')?.textContent).toContain("无法加载消息统计");
    await act(async () => container.querySelector("button")?.click());
    await vi.waitFor(() => expect(getSessionStats).toHaveBeenCalledTimes(2));
    await act(async () => root.unmount());
  });
});
