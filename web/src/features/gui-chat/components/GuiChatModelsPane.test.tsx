// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  api,
  type ModelRegistration,
  type ModelRegistrationsResponse,
} from "@/lib/api";
import { GuiChatModelsPane } from "./GuiChatModelsPane";

const payload: ModelRegistrationsResponse = {
  active: {
    chat: { model: "default-model", provider: "default-provider", registration_id: "chat-default" },
    image: { model: "image-v1", provider: "image-provider", registration_id: "image-a" },
    video: { model: "", provider: "", registration_id: null },
  },
  registrations: [
    registration("chat-current", "Current model", "chat", "current-provider", "current-model"),
    registration("chat-default", "Default model", "chat", "default-provider", "default-model"),
    registration("image-a", "Image model", "image", "image-provider", "image-v1"),
    registration("video-a", "Video model", "video", "video-provider", "video-v1"),
  ],
};

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML = "<div id=\"root\"></div>";
  vi.spyOn(api, "getModelRegistrations").mockResolvedValue(payload);
  vi.spyOn(api, "getModelRegistrationCatalog").mockImplementation(async (kind) => ({
    kind,
    providers: kind === "chat"
      ? [{ authenticated: true, credential_configured: true, models: ["catalog-model"], name: "Catalog Provider", slug: "catalog-provider" }]
      : [{ available: true, capabilities: {}, credential_configured: true, default_model: `${kind}-v1`, models: [{ id: `${kind}-v1` }], name: `${kind} provider`, provider: `${kind}-provider`, setup: { env_vars: [] } }],
  }));
  vi.spyOn(api, "createModelRegistration").mockResolvedValue(registration("created", "Created", "chat", "catalog-provider", "catalog-model"));
  vi.spyOn(api, "updateModelRegistration").mockResolvedValue(registration("chat-default", "Renamed", "chat", "default-provider", "default-model"));
  vi.spyOn(api, "deleteModelRegistration").mockResolvedValue({ id: "video-a", ok: true });
  vi.spyOn(api, "activateModelRegistration").mockResolvedValue({ kind: "video", model: "video-v1", ok: true, provider: "video-provider", registration_id: "video-a" });
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("GuiChatModelsPane", () => {
  it("renders a Skills-style list with current, default, active, search, and kind filters", async () => {
    await renderPane();

    expect(api.getModelRegistrations).toHaveBeenCalledWith();
    expect(document.querySelector("[data-models-pane].gui-chat-workspace-pane")).not.toBeNull();
    expect(document.body.textContent).toContain("Current conversation");
    expect(document.body.textContent).toContain("Default");

    await clickButton("Image", true);
    expect(document.body.textContent).toContain("Image model");
    expect(document.body.textContent).toContain("Active");
    expect(document.body.textContent).not.toContain("Current model");

    const search = document.querySelector<HTMLInputElement>('input[aria-label="Search models"]');
    await act(async () => {
      setInput(search, "missing");
    });
    expect(document.body.textContent).toContain("No matching models");
  });

  it("switches chat models for the session or global default and confirms expensive models", async () => {
    const onSwitchChat = vi.fn()
      .mockResolvedValueOnce({ confirm_required: false, value: "default-model" })
      .mockResolvedValueOnce({ confirm_message: "High price", confirm_required: true, value: "default-model" })
      .mockResolvedValueOnce({ confirm_required: false, value: "default-model" });
    await renderPane({ onSwitchChat });

    const defaultRow = rowFor("Default model");
    await clickWithin(defaultRow, "Use", true);
    expect(onSwitchChat).toHaveBeenCalledWith(expect.objectContaining({ id: "chat-default" }), false, false);

    await clickWithin(rowFor("Current model"), "Use as default", true);
    expect(document.body.textContent).toContain("High price");
    await clickButton("Switch anyway", true);
    expect(onSwitchChat).toHaveBeenLastCalledWith(expect.objectContaining({ id: "chat-current" }), true, true);
  });

  it("keeps CRUD available without a live conversation and activates media models", async () => {
    await renderPane({ canSwitchChat: false });

    expect(buttonWithin(rowFor("Default model"), "Use", true)?.disabled).toBe(true);
    await clickButton("Video", true);
    await clickWithin(rowFor("Video model"), "Activate", true);
    expect(api.activateModelRegistration).toHaveBeenCalledWith("video-a");

    await clickWithin(rowFor("Video model"), "Delete Video model", true, "aria-label");
    expect(document.body.textContent).toContain("Delete Video model?");
    await clickButton("Delete", true);
    expect(api.deleteModelRegistration).toHaveBeenCalledWith("video-a");
  });

  it("creates catalog models through the existing registration API", async () => {
    await renderPane();
    await clickButton("Add model", true);
    expect(api.getModelRegistrationCatalog).toHaveBeenCalledWith("chat");

    await setLabeledInput("Model name", "New catalog model");
    await setLabeledSelect("Model provider", "catalog-provider");
    await setLabeledSelect("Model", "catalog-model");
    await clickButton("Save model", true);

    expect(api.createModelRegistration).toHaveBeenCalledWith({
      kind: "chat",
      model: "catalog-model",
      name: "New catalog model",
      provider: "catalog-provider",
      source: "catalog",
    });
  });

  it("preserves write-only custom credentials when editing with an empty API key", async () => {
    const custom = { ...registration("custom-a", "Private endpoint", "chat", "registered-custom", "private-model"), credential_configured: true, source: "custom" as const };
    vi.mocked(api.getModelRegistrations).mockResolvedValue({
      ...payload,
      registrations: [...payload.registrations, custom],
    });
    await renderPane();

    await clickWithin(rowFor("Private endpoint"), "Edit Private endpoint", true, "aria-label");
    expect(document.body.textContent).toContain("write-only");
    await setLabeledInput("Model name", "Private endpoint renamed");
    await setLabeledInput("Model", "private-model");
    await setLabeledInput("Base URL", "https://llm.example/v1");
    await clickButton("Save model", true);

    expect(api.updateModelRegistration).toHaveBeenCalledWith("custom-a", expect.objectContaining({
      api_key: "",
      base_url: "https://llm.example/v1",
      model: "private-model",
      name: "Private endpoint renamed",
      source: "custom",
    }));
  });

  it("disables switching while generation is busy", async () => {
    const onSwitchChat = vi.fn();
    await renderPane({ busy: true, onSwitchChat });

    expect(buttonWithin(rowFor("Default model"), "Use", true)?.disabled).toBe(true);
    expect(document.querySelector('[role="alert"]')?.textContent).toContain("Stop the current response");
    expect(onSwitchChat).not.toHaveBeenCalled();
  });
});

async function renderPane(overrides: Partial<Parameters<typeof GuiChatModelsPane>[0]> = {}) {
  const container = document.getElementById("root");
  root = createRoot(container!);
  await act(async () => {
    root?.render(
      <GuiChatModelsPane
        busy={false}
        canSwitchChat
        currentModel="current-model"
        currentProvider="current-provider"
        onSwitchChat={vi.fn().mockResolvedValue({ confirm_required: false, value: "default-model" })}
        {...overrides}
      />,
    );
    await Promise.resolve();
    await Promise.resolve();
  });
}

function registration(id: string, name: string, kind: ModelRegistration["kind"], provider: string, model: string): ModelRegistration {
  return { credential_configured: null, id, kind, model, name, provider, source: "catalog", use_gateway: false };
}

function rowFor(text: string): HTMLElement {
  const row = Array.from(document.querySelectorAll<HTMLElement>("article")).find((item) => item.textContent?.includes(text));
  if (!row) throw new Error(`Missing row: ${text}`);
  return row;
}

function buttonWithin(rootElement: ParentNode, text: string, exact = false, attribute?: string): HTMLButtonElement | undefined {
  return Array.from(rootElement.querySelectorAll<HTMLButtonElement>("button")).find((button) => {
    const value = attribute ? button.getAttribute(attribute) : button.textContent?.trim();
    return exact ? value === text : value?.includes(text);
  });
}

async function clickWithin(rootElement: ParentNode, text: string, exact = false, attribute?: string) {
  await act(async () => {
    buttonWithin(rootElement, text, exact, attribute)?.click();
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function clickButton(text: string, exact = false) {
  await clickWithin(document, text, exact);
}

async function setLabeledInput(label: string, value: string) {
  await act(async () => {
    setInput(document.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`), value);
  });
}

async function setLabeledSelect(label: string, value: string) {
  const select = document.querySelector<HTMLSelectElement>(`select[aria-label="${label}"]`);
  await act(async () => {
    if (!select) return;
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
    setter?.call(select, value);
    select.dispatchEvent(new Event("change", { bubbles: true }));
    await Promise.resolve();
  });
}

function setInput(input: HTMLInputElement | null, value: string) {
  if (!input) return;
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  setter?.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
}
