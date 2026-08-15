// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "@/i18n";
import type { SkillInfo } from "@/lib/api";
import { GuiChatSkillsPane } from "../GuiChatSkillsPane";

const mocks = vi.hoisted(() => ({
  createSkill: vi.fn(),
  deleteSkill: vi.fn(),
  getSkills: vi.fn(),
  toggleSkill: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      createSkill: mocks.createSkill,
      deleteSkill: mocks.deleteSkill,
      getSkills: mocks.getSkills,
      toggleSkill: mocks.toggleSkill,
    },
  };
});

const listedSkills: SkillInfo[] = [
  {
    name: "release-notes",
    description: "Draft release notes",
    category: "writing",
    enabled: true,
  },
  {
    name: "incident-helper",
    description: "Guide an incident response",
    category: "operations",
    enabled: false,
  },
];

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  Object.values(mocks).forEach((mock) => mock.mockReset());
  mocks.getSkills.mockResolvedValue(listedSkills);
  mocks.toggleSkill.mockResolvedValue({ ok: true });
  mocks.createSkill.mockResolvedValue({ success: true });
  mocks.deleteSkill.mockResolvedValue({ success: true });
  document.body.innerHTML = "";
  localStorage.clear();
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
});

describe("GuiChatSkillsPane", () => {
  it("loads, searches, and toggles skills for the current owner", async () => {
    const container = await renderPane();

    expect(mocks.getSkills).toHaveBeenCalledWith();
    expect(container.textContent).toContain("incident-helper");
    expect(container.textContent).toContain("release-notes");

    changeValue(container.querySelector('[aria-label="Search skills"]'), "incident");
    expect(container.textContent).toContain("incident-helper");
    expect(container.textContent).not.toContain("release-notes");

    const toggle = container.querySelector<HTMLButtonElement>('[aria-label="Enable incident-helper"]');
    await act(async () => {
      toggle?.click();
      await Promise.resolve();
    });

    expect(mocks.toggleSkill).toHaveBeenCalledWith("incident-helper", true);
    expect(toggle?.getAttribute("aria-checked")).toBe("true");
  });

  it("creates a skill and refreshes the owner-scoped list", async () => {
    const container = await renderPane();

    await act(async () => buttonNamed(container, "New skill")?.click());
    changeValue(document.body.querySelector('[aria-label="Skill name"]'), "daily-brief");
    changeValue(document.body.querySelector('[aria-label="Skill category"]'), "writing");
    changeValue(
      document.body.querySelector('[aria-label="SKILL.md"]'),
      "---\nname: daily-brief\ndescription: Draft a daily brief.\n---\n\n# Daily brief\n",
    );

    await act(async () => {
      buttonNamed(document.body, "Create skill")?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mocks.createSkill).toHaveBeenCalledWith(
      {
        name: "daily-brief",
        category: "writing",
        content: "---\nname: daily-brief\ndescription: Draft a daily brief.\n---\n\n# Daily brief\n",
      },
    );
    expect(mocks.getSkills).toHaveBeenCalledTimes(2);
    expect(document.body.querySelector('[role="dialog"]')).toBeNull();
  });

  it("confirms deletion and removes only after a successful request", async () => {
    const container = await renderPane();

    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Delete release-notes"]')?.click());
    expect(document.body.textContent).toContain("Delete release-notes?");

    await act(async () => {
      buttonNamed(document.body, "Delete")?.click();
      await Promise.resolve();
    });

    expect(mocks.deleteSkill).toHaveBeenCalledWith("release-notes");
    expect(container.textContent).not.toContain("release-notes");
    expect(container.textContent).toContain("incident-helper");
  });

  it("keeps a skill visible when deletion fails", async () => {
    mocks.deleteSkill.mockRejectedValue(new Error("Pinned skills cannot be deleted"));
    const container = await renderPane();

    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Delete release-notes"]')?.click());
    await act(async () => {
      buttonNamed(document.body, "Delete")?.click();
      await Promise.resolve();
    });

    expect(mocks.deleteSkill).toHaveBeenCalledWith("release-notes");
    expect(container.textContent).toContain("release-notes");
    expect(container.textContent).toContain("Pinned skills cannot be deleted");
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull();
  });
  it("renders Chinese workspace controls while preserving skill content", async () => {
    const container = await renderPane("zh");

    expect(container.textContent).toContain("新建技能");
    expect(container.textContent).toContain("可供此工作区中新对话使用的可复用指令。");
    expect(container.textContent).toContain("Draft release notes");
    expect(container.querySelector('[aria-label="搜索技能"]')).not.toBeNull();
  });

});

async function renderPane(locale: "en" | "zh" = "en") {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  localStorage.setItem("hermes-locale", locale);
  await act(async () => {
    root?.render(<I18nProvider><GuiChatSkillsPane /></I18nProvider>);
    await Promise.resolve();
  });
  return container;
}

function changeValue(element: Element | null, value: string) {
  if (!element) throw new Error("Expected form control");
  const prototype = element instanceof HTMLTextAreaElement
    ? HTMLTextAreaElement.prototype
    : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
  act(() => {
    setter?.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

function buttonNamed(rootNode: ParentNode, text: string) {
  return Array.from(rootNode.querySelectorAll<HTMLButtonElement>("button"))
    .find((button) => button.textContent?.trim() === text);
}
