// @vitest-environment jsdom

import { act, useState, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PageHeaderContext } from "@/contexts/page-header-context";
import { I18nProvider } from "@/i18n";
import { api, type ModelRegistrationsResponse } from "@/lib/api";
import ModelRegistrationsPage, {
  registrationRequestFromForm,
} from "./ModelRegistrationsPage";

const payload: ModelRegistrationsResponse = {
  registrations: [
    {
      id: "chat-a",
      name: "Primary chat",
      kind: "chat",
      provider: "anthropic",
      model: "claude-test",
      source: "catalog",
      use_gateway: false,
      credential_configured: null,
    },
    {
      id: "image-a",
      name: "Image maker",
      kind: "image",
      provider: "image-provider",
      model: "image-v1",
      source: "catalog",
      use_gateway: true,
      credential_configured: null,
    },
  ],
  active: {
    chat: { registration_id: null, provider: "", model: "" },
    image: {
      registration_id: "image-a",
      provider: "image-provider",
      model: "image-v1",
    },
    video: { registration_id: null, provider: "", model: "" },
  },
};

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML = "";
  vi.spyOn(api, "getModelRegistrations").mockResolvedValue(payload);
  vi.spyOn(api, "getModelRegistrationCatalog").mockImplementation(
    async (kind) => ({
      kind,
      providers:
        kind === "chat"
          ? [
              {
                slug: "anthropic",
                name: "Anthropic",
                models: ["claude-test"],
                authenticated: true,
                credential_configured: true,
              },
            ]
          : kind === "image"
            ? [
                {
                  provider: "image-provider",
                  name: "Image Provider",
                  available: true,
                  credential_configured: true,
                  models: [{ id: "image-v1", display: "Image V1" }],
                  default_model: "image-v1",
                  capabilities: {},
                  setup: { env_vars: [] },
                },
              ]
            : [],
    }),
  );
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("registrationRequestFromForm", () => {
  it("maps custom chat form fields to the backend request shape", () => {
    expect(
      registrationRequestFromForm({
        name: " Private endpoint ",
        kind: "chat",
        source: "custom",
        provider: "ignored",
        model: " private-model ",
        baseUrl: " https://llm.example/v1 ",
        apiMode: "openai",
        apiKey: " secret ",
        contextLength: "32000",
        useGateway: false,
      }),
    ).toEqual({
      name: "Private endpoint",
      kind: "chat",
      source: "custom",
      model: "private-model",
      base_url: "https://llm.example/v1",
      api_mode: "openai",
      api_key: "secret",
      context_length: 32000,
    });
  });
});

describe("ModelRegistrationsPage", () => {
  it("renders registrations and prevents deleting the active media model", async () => {
    renderPage();
    await flush();

    expect(document.body.textContent).toContain("Primary chat");
    expect(document.body.textContent).toContain("Image maker");
    expect(document.body.textContent).toContain("Active");
    const activeDelete = document.querySelector<HTMLButtonElement>(
      'button[aria-label="Delete: Image maker"]',
    );
    expect(activeDelete?.disabled).toBe(true);
    expect(activeDelete?.title).toContain("Switch the active model");
  });

  it("creates a catalog registration from the add-model form", async () => {
    const create = vi
      .spyOn(api, "createModelRegistration")
      .mockResolvedValue(payload.registrations[0]);
    renderPage();
    await flush();

    await clickButton("Add model");
    await flush();
    expect(api.getModelRegistrationCatalog).toHaveBeenCalledWith("chat");
    setInput("registration-name", "New chat");
    await chooseOption("registration-provider", "Anthropic");
    await chooseOption("registration-model", "claude-test");
    await clickButton("Save");
    await flush();

    expect(create).toHaveBeenCalledWith({
      name: "New chat",
      kind: "chat",
      source: "catalog",
      provider: "anthropic",
      model: "claude-test",
    });
  });

  it("re-requires write-only custom endpoint fields when editing", async () => {
    const customPayload: ModelRegistrationsResponse = {
      ...payload,
      registrations: [
        {
          id: "custom-a",
          name: "Private endpoint",
          kind: "chat",
          provider: "registered-custom-a",
          model: "private-model",
          source: "custom",
          use_gateway: false,
          credential_configured: true,
        },
      ],
    };
    vi.mocked(api.getModelRegistrations).mockResolvedValue(customPayload);
    const update = vi.spyOn(api, "updateModelRegistration");
    renderPage();
    await flush();

    await clickButton("", "Edit: Private endpoint");
    expect(api.getModelRegistrationCatalog).not.toHaveBeenCalled();
    expect(document.body.textContent).toContain(
      "Custom endpoint URLs are write-only",
    );
    await clickButton("Save");

    expect(document.body.textContent).toContain("Base URL is required");
    expect(update).not.toHaveBeenCalled();
  });

  it("activates inactive media registrations", async () => {
    const inactivePayload: ModelRegistrationsResponse = {
      ...payload,
      active: {
        ...payload.active,
        image: { registration_id: null, provider: "", model: "" },
      },
    };
    vi.mocked(api.getModelRegistrations).mockResolvedValue(inactivePayload);
    const activate = vi
      .spyOn(api, "activateModelRegistration")
      .mockResolvedValue({
        ok: true,
        registration_id: "image-a",
        kind: "image",
        provider: "image-provider",
        model: "image-v1",
      });
    renderPage();
    await flush();

    await clickButton("Set active");
    await flush();

    expect(activate).toHaveBeenCalledWith("image-a");
  });
});

function TestPageHeader({ children }: { children: ReactNode }) {
  const [end, setEnd] = useState<ReactNode>(null);
  const [title, setTitle] = useState<string | null>(null);
  return (
    <PageHeaderContext.Provider
      value={{ setAfterTitle: () => {}, setEnd, setTitle }}
    >
      <header>
        <h1>{title}</h1>
        {end}
      </header>
      {children}
    </PageHeaderContext.Provider>
  );
}

function renderPage() {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <MemoryRouter initialEntries={["/model-registrations"]}>
        <I18nProvider>
          <TestPageHeader>
            <ModelRegistrationsPage />
          </TestPageHeader>
        </I18nProvider>
      </MemoryRouter>,
    );
  });
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function clickButton(label: string, ariaLabel?: string) {
  const button = Array.from(document.querySelectorAll("button")).find(
    (candidate) =>
      (ariaLabel
        ? candidate.getAttribute("aria-label") === ariaLabel ||
          candidate.textContent?.trim() === ariaLabel
        : candidate.textContent?.trim() === label),
  );
  expect(button).toBeDefined();
  await act(async () => button?.click());
}

function setInput(id: string, value: string) {
  const input = document.getElementById(id) as HTMLInputElement;
  const setter = Object.getOwnPropertyDescriptor(
    HTMLInputElement.prototype,
    "value",
  )?.set;
  setter?.call(input, value);
  act(() => input.dispatchEvent(new Event("input", { bubbles: true })));
}

async function chooseOption(id: string, label: string) {
  const select = document.getElementById(id);
  const trigger = select?.querySelector<HTMLButtonElement>('[role="combobox"]');
  expect(trigger).toBeDefined();
  await act(async () => trigger?.click());
  const option = Array.from(
    select?.querySelectorAll<HTMLElement>('[role="option"]') ?? [],
  ).find((candidate) => candidate.textContent?.trim() === label);
  expect(option).toBeDefined();
  await act(async () => option?.click());
}
