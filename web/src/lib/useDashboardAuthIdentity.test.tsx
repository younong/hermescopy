// @vitest-environment jsdom

import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { dashboardAuthTransition } from "./dashboardAuthTransition";
import {
  DashboardAuthIdentityProvider,
  useDashboardAuthIdentity,
} from "./useDashboardAuthIdentity";

const apiMocks = vi.hoisted(() => ({
  getAuthMe: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getAuthMe: apiMocks.getAuthMe,
    },
  };
});

let root: Root | null = null;

beforeEach(() => {
  apiMocks.getAuthMe.mockReset();
  window.__HERMES_AUTH_REQUIRED__ = true;
  document.body.innerHTML = "";
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  dashboardAuthTransition.reset();
  delete window.__HERMES_AUTH_REQUIRED__;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("DashboardAuthIdentityProvider", () => {
  it("does not expose an owner as ready before its transition completes", async () => {
    const identity = deferred<AuthIdentity>();
    apiMocks.getAuthMe.mockReturnValue(identity.promise);
    const transition = vi.spyOn(dashboardAuthTransition, "transition");
    const readiness: Array<{ owner: string; ready: string }> = [];
    const observer = new MutationObserver(() => {
      const probe = document.querySelector<HTMLElement>("[data-first-owner]");
      if (!probe) return;
      readiness.push({
        owner: probe.textContent ?? "",
        ready: probe.dataset.ready ?? "",
      });
    });
    observer.observe(document.body, { attributes: true, childList: true, subtree: true });

    renderIdentityTree();

    await act(async () => {
      identity.resolve(authIdentity());
      await identity.promise;
    });
    observer.disconnect();

    expect(transition).toHaveBeenCalledWith("owner-a");
    expect(readiness).not.toContainEqual({ owner: "owner-a", ready: "false" });
    expect(document.querySelector("[data-first-owner]")).toMatchObject({
      textContent: "owner-a",
    });
    expect(document.querySelector("[data-first-owner]")?.getAttribute("data-ready")).toBe("true");
  });

  it("shares a resolved owner with consumers that mount later", async () => {
    const identity = deferred<AuthIdentity>();
    apiMocks.getAuthMe.mockReturnValue(identity.promise);
    const transition = vi.spyOn(dashboardAuthTransition, "transition");

    renderIdentityTree();

    await act(async () => {
      identity.resolve(authIdentity());
      await identity.promise;
    });

    expect(apiMocks.getAuthMe).toHaveBeenCalledOnce();
    expect(document.querySelector("[data-first-owner]")?.textContent).toBe("owner-a");
    const transitionCount = transition.mock.calls.length;

    await act(async () => {
      document.querySelector<HTMLButtonElement>("[data-mount-late]")?.click();
    });

    expect(document.querySelector("[data-late-owner]")?.textContent).toBe("owner-a");
    expect(apiMocks.getAuthMe).toHaveBeenCalledOnce();
    expect(transition).toHaveBeenCalledTimes(transitionCount);
  });
});

function renderIdentityTree() {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <DashboardAuthIdentityProvider>
        <ConsumerHost />
      </DashboardAuthIdentityProvider>,
    );
  });
}

function ConsumerHost() {
  const [showLateConsumer, setShowLateConsumer] = useState(false);
  return (
    <>
      <IdentityProbe name="first" />
      <button data-mount-late onClick={() => setShowLateConsumer(true)} type="button" />
      {showLateConsumer ? <IdentityProbe name="late" /> : null}
    </>
  );
}

function IdentityProbe({ name }: { name: "first" | "late" }) {
  const { ownerKey, ready } = useDashboardAuthIdentity();
  if (name === "first") {
    return <div data-first-owner data-ready={ready}>{ownerKey}</div>;
  }
  return <div data-late-owner data-ready={ready}>{ownerKey}</div>;
}

interface AuthIdentity {
  display_name: string;
  email: string;
  expires_at: number;
  org_id: string;
  owner_key: string;
  provider: string;
  tenant_id: string;
  user_id: string;
}

function authIdentity(): AuthIdentity {
  return {
    display_name: "",
    email: "",
    expires_at: 4_102_444_800,
    org_id: "org-a",
    owner_key: "owner-a",
    provider: "local",
    tenant_id: "tenant-a",
    user_id: "user-a",
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}
