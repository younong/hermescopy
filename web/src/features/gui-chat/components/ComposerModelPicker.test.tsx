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
    expect(listbox().textContent).toContain("default-model");
    expect(listbox().textContent).not.toContain("Image model");
    expect(optionFor("current-model").disabled).toBe(true);
  });

  it("selects locally, closes immediately, and remains available for another selection", async () => {
    const onSelect = vi.fn();
    await renderPicker({ onSelect });
    await openPicker();

    await act(async () => optionFor("default-model").click());

    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: "chat-default" }));
    expect(queryListbox()).toBeNull();
    expect(trigger().disabled).toBe(false);

    await openPicker();
    expect(listbox()).not.toBeNull();
  });

  it("allows local selection while a response is generating", async () => {
    const onSelect = vi.fn();
    await renderPicker({ onSelect });
    await openPicker();
    await act(async () => optionFor("default-model").click());
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it("disables selection without an active conversation", async () => {
    await renderPicker({ canSelect: false });
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
  root = createRoot(document.getElementById("root")!);
  await act(async () => {
    root?.render(
      <ComposerModelPicker
        canSelect
        currentModel="current-model"
        currentProvider="current-provider"
        onManageModels={vi.fn()}
        onSelect={vi.fn()}
        {...overrides}
      />,
    );
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

async function clickButton(text: string) {
  await act(async () => {
    const button = Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
      .find((item) => item.textContent?.trim() === text);
    if (!button) throw new Error(`Missing button: ${text}`);
    button.click();
    await Promise.resolve();
  });
}
