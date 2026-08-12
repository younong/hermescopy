// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "@/i18n";
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
    voice: { model: "", provider: "", registration_id: null },
    code: { model: "gpt-5.3-codex", provider: "openai-codex", registration_id: "code-codex" },
    vector: { model: "", provider: "", registration_id: null },
  },
  registrations: [
    registration("chat-current", "Current model", "chat", "current-provider", "current-model"),
    registration("chat-default", "Default model", "chat", "default-provider", "default-model"),
    registration("code-codex", "Codex model", "code", "openai-codex", "gpt-5.3-codex"),
    registration("admin-chat", "Admin model", "chat", "admin-provider", "admin-model", "admin"),
    registration("image-a", "Image model", "image", "image-provider", "image-v1"),
    registration("video-a", "Video model", "video", "video-provider", "video-v1"),
    registration("voice-a", "Voice model", "voice", "openai", "gpt-4o-mini-tts"),
    registration("vector-a", "Vector model", "vector", "openai", "text-embedding-3-small"),
  ],
};

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML = "<div id=\"root\"></div>";
  localStorage.clear();
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
  it("renders separate Chat and Code model tabs", async () => {
    await renderPane();

    expect(api.getModelRegistrations).toHaveBeenCalledWith();
    expect(document.querySelector("[data-models-pane].gui-chat-workspace-pane")).not.toBeNull();
    expect(document.body.textContent).toContain("Current conversation");
    expect(document.body.textContent).toContain("Default");
    expect(document.body.textContent).not.toContain("Codex model");

    await clickButton("Code", true);
    expect(document.body.textContent).toContain("Codex model");
    expect(document.body.textContent).not.toContain("Current model");

    await clickButton("Chat", true);
    expect(document.body.textContent).toContain("Current model");
    expect(document.body.textContent).not.toContain("Codex model");

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

  it("does not route Code models through the Chat switch callback", async () => {
    const onSwitchChat = vi.fn();
    const onActivateCode = vi.fn().mockResolvedValue(undefined);
    await renderPane({ onSwitchChat, onActivateCode });
    await clickButton("Code", true);
    await clickWithin(rowFor("Codex model"), "Default", true);
    expect(onActivateCode).toHaveBeenCalledWith(expect.objectContaining({ id: "code-codex", kind: "code" }));
    expect(onSwitchChat).not.toHaveBeenCalled();
  });

  it("shows Admin and Mine models while keeping administrator models immutable", async () => {
    const onSwitchChat = vi.fn().mockResolvedValue({ confirm_required: false, value: "admin-model" });
    await renderPane({ onSwitchChat });

    const adminRow = rowFor("Admin model");
    expect(adminRow.textContent).toContain("Admin");
    expect(adminRow.textContent).toContain("Managed by your administrator.");
    expect(buttonWithin(adminRow, "Edit Admin model", true, "aria-label")).toBeUndefined();
    expect(buttonWithin(adminRow, "Delete Admin model", true, "aria-label")).toBeUndefined();
    await clickWithin(adminRow, "Use", true);
    expect(onSwitchChat).toHaveBeenCalledWith(expect.objectContaining({ id: "admin-chat" }), false, false);

    const mineRow = rowFor("Current model");
    expect(mineRow.textContent).toContain("Mine");
    expect(buttonWithin(mineRow, "Edit Current model", true, "aria-label")).toBeDefined();
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

  it("creates voice and vector registrations without activation controls", async () => {
    await renderPane();

    await clickButton("Voice", true);
    expect(document.body.textContent).toContain("Voice model");
    expect(buttonWithin(rowFor("Voice model"), "Activate", true)).toBeUndefined();
    await clickButton("Add model", true);
    const sourceSelect = document.querySelector<HTMLSelectElement>('select[aria-label="Source"]');
    expect(sourceSelect?.disabled).toBe(true);
    expect(sourceSelect?.value).toBe("manual");
    expect(sourceSelect?.selectedOptions[0]?.textContent).toBe("Manual");
    await setLabeledInput("Name", "New voice model");
    await setLabeledInput("Provider", "openai");
    await setLabeledInput("Model", "gpt-4o-mini-tts");
    await clickButton("Save model", true);
    expect(api.getModelRegistrationCatalog).not.toHaveBeenCalledWith("voice");
    expect(api.createModelRegistration).toHaveBeenCalledWith({
      kind: "voice",
      model: "gpt-4o-mini-tts",
      name: "New voice model",
      provider: "openai",
      source: "manual",
    });

    await clickButton("Vector", true);
    expect(document.body.textContent).toContain("Vector model");
    expect(buttonWithin(rowFor("Vector model"), "Activate", true)).toBeUndefined();
  });

  it("creates catalog models through the existing registration API", async () => {
    await renderPane();
    await clickButton("Add model", true);
    expect(api.getModelRegistrationCatalog).toHaveBeenCalledWith("chat");

    await setLabeledInput("Name", "New catalog model");
    await setLabeledSelect("Provider", "catalog-provider");
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
    await setLabeledInput("Name", "Private endpoint renamed");
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
  it("renders Chinese model chrome while preserving registration names", async () => {
    await renderPane({}, "zh");

    expect(document.body.textContent).toContain("添加模型");
    expect(document.body.textContent).toContain("管理模型，并选择用于对话和内容生成的模型。");
    expect(document.body.textContent).toContain("Current model");
    expect(document.querySelector('[aria-label="搜索模型"]')).not.toBeNull();
  });

});

async function renderPane(
  overrides: Partial<Parameters<typeof GuiChatModelsPane>[0]> = {},
  locale: "en" | "zh" = "en",
) {
  localStorage.setItem("hermes-locale", locale);
  const container = document.getElementById("root");
  root = createRoot(container!);
  await act(async () => {
    root?.render(
      <I18nProvider><GuiChatModelsPane
        busy={false}
        canSwitchChat
        currentModel="current-model"
        currentProvider="current-provider"
        onSwitchChat={vi.fn().mockResolvedValue({ confirm_required: false, value: "default-model" })}
        {...overrides}
      /></I18nProvider>,
    );
    await Promise.resolve();
    await Promise.resolve();
  });
}

function registration(
  id: string,
  name: string,
  kind: ModelRegistration["kind"],
  provider: string,
  model: string,
  scope: ModelRegistration["scope"] = "user",
): ModelRegistration {
  const source = kind === "voice" || kind === "vector" ? "manual" : "catalog";
  return {
    credential_configured: null,
    id,
    kind,
    model,
    mutable: scope === "user",
    name,
    provider,
    scope,
    source,
    use_gateway: false,
  };
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
