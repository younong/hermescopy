// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthWidget } from "./AuthWidget";
import { dashboardAuthTransition } from "@/lib/dashboardAuthTransition";

const mocks = vi.hoisted(() => ({
  identity: vi.fn(),
  logout: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, logout: mocks.logout },
  };
});

vi.mock("@/lib/useDashboardAuthIdentity", () => ({
  useDashboardAuthIdentity: mocks.identity,
}));

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  mocks.identity.mockReset();
  mocks.logout.mockReset();
  document.body.innerHTML = "";
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  dashboardAuthTransition.reset();
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("AuthWidget", () => {
  it("renders a compact account action in header mode", () => {
    mocks.identity.mockReturnValue(identity());

    renderWidget(<AuthWidget variant="header" />);

    expect(document.body.textContent).toContain("Member User");
    expect(document.body.textContent).not.toContain("via basic");
    expect(document.querySelector('[aria-label="Log out"]')).not.toBeNull();
  });

  it("resets owner-scoped state before logging out", () => {
    mocks.identity.mockReturnValue(identity());
    const reset = vi.spyOn(dashboardAuthTransition, "reset");

    renderWidget(<AuthWidget variant="header" />);
    act(() => {
      document.querySelector<HTMLButtonElement>('[aria-label="Log out"]')?.click();
    });

    expect(reset).toHaveBeenCalledOnce();
    expect(mocks.logout).toHaveBeenCalledOnce();
  });
});

function renderWidget(widget: React.ReactNode) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root?.render(widget));
}

function identity() {
  return {
    authMe: {
      display_name: "Member User",
      email: "member@example.com",
      expires_at: 4_102_444_800,
      org_id: "org-a",
      owner_key: "owner-a",
      provider: "basic",
      role: "member",
      tenant_id: "tenant-a",
      user_id: "member-a",
    },
    authRequired: true,
    error: null,
    loading: false,
    ownerKey: "owner-a",
    ready: true,
    refresh: vi.fn(),
  };
}
