// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { I18nProvider, LOCALE_META } from "@/i18n";
import { LanguageSwitcher } from "./LanguageSwitcher";

vi.mock("@nous-research/ui/hooks/use-below-breakpoint", () => ({
  useBelowBreakpoint: () => false,
}));

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  localStorage.clear();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  document.body.innerHTML = "";
  localStorage.clear();
});

describe("LanguageSwitcher", () => {
  it("keeps the existing unrestricted locale list", async () => {
    await render(<LanguageSwitcher />);
    await openSwitcher();

    expect(optionLabels()).toEqual(Object.values(LOCALE_META).map(({ name }) => name));
  });

  it("filters and deduplicates an allowlist without changing its order", async () => {
    await render(<LanguageSwitcher allowedLocales={["zh", "en", "zh"]} />);
    await openSwitcher();

    expect(optionLabels()).toEqual(["简体中文", "English"]);
  });

  it("persists an allowed selection and restores it after remount", async () => {
    await render(<LanguageSwitcher allowedLocales={["en", "zh"]} />);
    await openSwitcher();
    await selectOption("简体中文");

    expect(localStorage.getItem("hermes-locale")).toBe("zh");
    expect(trigger().textContent).toContain("简体中文");

    await remount(<LanguageSwitcher allowedLocales={["en", "zh"]} />);

    expect(trigger().textContent).toContain("简体中文");
    await openSwitcher();
    expect(selectedOption()?.textContent).toContain("简体中文");
  });

  it.each([
    ["de", "disallowed"],
    ["not-a-locale", "invalid"],
  ])("normalizes an %s stored locale to the first allowed locale", async (storedLocale) => {
    localStorage.setItem("hermes-locale", storedLocale);

    await render(<LanguageSwitcher allowedLocales={["en", "zh"]} />);

    expect(localStorage.getItem("hermes-locale")).toBe("en");
    expect(trigger().textContent).toContain("EN");
    await openSwitcher();
    expect(selectedOption()?.textContent).toContain("English");
  });

  it("normalizes a newly disallowed current locale", async () => {
    localStorage.setItem("hermes-locale", "zh");
    await render(<LanguageSwitcher allowedLocales={["en", "zh"]} />);

    await render(<LanguageSwitcher allowedLocales={["en"]} />);

    expect(localStorage.getItem("hermes-locale")).toBe("en");
    expect(trigger().textContent).toContain("EN");
  });
});

async function render(node: React.ReactNode): Promise<void> {
  await act(async () => {
    root.render(<I18nProvider>{node}</I18nProvider>);
    await Promise.resolve();
  });
}

async function remount(node: React.ReactNode): Promise<void> {
  await act(async () => root.unmount());
  container.remove();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await render(node);
}

async function openSwitcher(): Promise<void> {
  await act(async () => {
    trigger().click();
    await Promise.resolve();
  });
}

async function selectOption(label: string): Promise<void> {
  const option = Array.from(document.querySelectorAll<HTMLButtonElement>('[role="option"]'))
    .find((candidate) => candidate.textContent?.includes(label));
  if (!option) throw new Error(`Missing locale option: ${label}`);
  await act(async () => {
    option.click();
    await Promise.resolve();
  });
}

function trigger(): HTMLButtonElement {
  const button = container.querySelector<HTMLButtonElement>('[aria-haspopup="listbox"]');
  if (!button) throw new Error("Missing language switcher trigger");
  return button;
}

function optionLabels(): string[] {
  return Array.from(document.querySelectorAll<HTMLElement>('[role="option"]'))
    .map((option) => option.textContent?.trim() ?? "");
}

function selectedOption(): HTMLElement | undefined {
  return Array.from(document.querySelectorAll<HTMLElement>('[role="option"]'))
    .find((option) => option.getAttribute("aria-selected") === "true");
}
