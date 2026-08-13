// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  api,
  type ModelRegistration,
  type ModelRegistrationsResponse,
} from "@/lib/api";
import { ComposerModelPicker } from "./ComposerModelPicker";

const payload: ModelRegistrationsResponse = {
  active: {
    chat: { model: "default-model", provider: "default-provider", registration_id: "chat-default" },
    code: { model: "", provider: "", registration_id: null },
    image: { model: "image-v1", provider: "image-provider", registration_id: "image-a" },
    video: { model: "", provider: "", registration_id: null },
    voice: { model: "", provider: "", registration_id: null },
    vector: { model: "", provider: "", registration_id: null },
  },
  registrations: [
    registration("chat-current", "Current model", "chat", "current-provider", "current-model"),
    registration("chat-default", "Default model", "chat", "default-provider", "default-model"),
    registration("image-a", "Image model", "image", "image-provider", "image-v1"),
  ],
};

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML = "<div id=\"root\"></div>";
  vi.spyOn(api, "getModelRegistrations").mockResolvedValue(payload);
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("ComposerModelPicker", () => {
  it("shows the current model short name and lazy-loads chat registrations on open", async () => {
    await renderPicker();

    expect(trigger().textContent).toContain("current-model");
    expect(api.getModelRegistrations).not.toHaveBeenCalled();

    await openPicker();

    expect(api.getModelRegistrations).toHaveBeenCalledWith();
    expect(listbox()).not.toBeNull();
    expect(listbox().textContent).toContain("default-model");
    expect(listbox().textContent).not.toContain("Default model");
    expect(listbox().textContent).not.toContain("default-provider");
    expect(listbox().textContent).not.toContain("Image model");

    const current = optionFor("current-model");
    expect(current.getAttribute("aria-selected")).toBe("true");
    expect(current.disabled).toBe(true);
  });

  it("marks the current model when the gateway reports a raw provider name", async () => {
    // Live gateways report the agent provider (bare "custom"), not the
    // registration slug ("current-provider"), for custom endpoints.
    await renderPicker({ currentProvider: "custom" });
    await openPicker();

    const current = optionFor("current-model");
    expect(current.getAttribute("aria-selected")).toBe("true");
    expect(current.disabled).toBe(true);
  });

  it("falls back to a placeholder when no model is active", async () => {
    await renderPicker({ currentModel: undefined, currentProvider: undefined });
    expect(trigger().textContent).toContain("Select model");
  });

  it("switches the session model and closes the popover", async () => {
    const onSwitchChat = vi.fn().mockResolvedValue({ confirm_required: false, value: "default-model" });
    await renderPicker({ onSwitchChat });
    await openPicker();

    await act(async () => {
      optionFor("default-model").click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(onSwitchChat).toHaveBeenCalledWith(
      expect.objectContaining({ id: "chat-default" }),
      false,
    );
    expect(queryListbox()).toBeNull();
  });

  it("confirms expensive models before switching", async () => {
    const onSwitchChat = vi.fn()
      .mockResolvedValueOnce({ confirm_message: "High price", confirm_required: true, value: "default-model" })
      .mockResolvedValueOnce({ confirm_required: false, value: "default-model" });
    await renderPicker({ onSwitchChat });
    await openPicker();

    await act(async () => {
      optionFor("default-model").click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("High price");
    expect(onSwitchChat).toHaveBeenCalledTimes(1);

    await clickButton("Use model", true);
    expect(onSwitchChat).toHaveBeenLastCalledWith(
      expect.objectContaining({ id: "chat-default" }),
      true,
    );
  });

  it("disables switching while a response is generating", async () => {
    await renderPicker({ busy: true });
    expect(trigger().disabled).toBe(true);
  });

  it("disables switching without an active conversation", async () => {
    await renderPicker({ canSwitch: false });
    expect(trigger().disabled).toBe(true);
  });

  it("surfaces load failures with a retry", async () => {
    vi.mocked(api.getModelRegistrations).mockRejectedValueOnce(new Error("boom"));
    await renderPicker();
    await openPicker();

    expect(document.body.textContent).toContain("boom");

    await clickButton("Retry");
    expect(api.getModelRegistrations).toHaveBeenCalledTimes(2);
    expect(listbox().textContent).toContain("default-model");
  });

  it("navigates to model management from the popover", async () => {
    const onManageModels = vi.fn();
    await renderPicker({ onManageModels });
    await openPicker();

    await clickButton("Manage models…");
    expect(onManageModels).toHaveBeenCalledTimes(1);
    expect(queryListbox()).toBeNull();
  });
});

async function renderPicker(overrides: Partial<Parameters<typeof ComposerModelPicker>[0]> = {}) {
  const container = document.getElementById("root");
  root = createRoot(container!);
  await act(async () => {
    root?.render(
      <ComposerModelPicker
        busy={false}
        canSwitch
        currentModel="current-model"
        currentProvider="current-provider"
        onManageModels={vi.fn()}
        onSwitchChat={vi.fn().mockResolvedValue({ confirm_required: false, value: "default-model" })}
        {...overrides}
      />,
    );
    await Promise.resolve();
    await Promise.resolve();
  });
}

function registration(id: string, name: string, kind: ModelRegistration["kind"], provider: string, model: string): ModelRegistration {
  return { credential_configured: null, id, kind, model, mutable: true, name, provider, scope: "user", source: "catalog", use_gateway: false };
}

function trigger(): HTMLButtonElement {
  const button = document.querySelector<HTMLButtonElement>('button[aria-label="Switch chat model"]');
  if (!button) throw new Error("Missing model picker trigger");
  return button;
}

function queryListbox(): HTMLElement | null {
  return document.querySelector<HTMLElement>('[role="listbox"]');
}

function listbox(): HTMLElement {
  const element = queryListbox();
  if (!element) throw new Error("Missing model listbox");
  return element;
}

function optionFor(text: string): HTMLButtonElement {
  const option = Array.from(listbox().querySelectorAll<HTMLButtonElement>('[role="option"]'))
    .find((item) => item.textContent?.includes(text));
  if (!option) throw new Error(`Missing option: ${text}`);
  return option;
}

async function openPicker() {
  await act(async () => {
    trigger().click();
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function clickButton(text: string, exact = false) {
  await act(async () => {
    const button = Array.from(document.querySelectorAll<HTMLButtonElement>("button")).find((item) => {
      const value = item.textContent?.trim();
      return exact ? value === text : value?.includes(text);
    });
    button?.click();
    await Promise.resolve();
    await Promise.resolve();
  });
}
