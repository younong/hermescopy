// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { I18nProvider, useI18n } from "./context";

let root: Root | null = null;

function LocaleProbe() {
  const { locale, setLocale } = useI18n();
  return (
    <button data-locale={locale} onClick={() => setLocale("en")} type="button">
      {locale}
    </button>
  );
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  localStorage.clear();
  document.body.innerHTML = '<div id="root"></div>';
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  localStorage.clear();
  document.body.innerHTML = "";
});

async function renderProvider() {
  const container = document.getElementById("root");
  if (!container) throw new Error("Missing test root");
  root = createRoot(container);
  await act(async () => {
    root?.render(
      <I18nProvider>
        <LocaleProbe />
      </I18nProvider>,
    );
  });
}

describe("I18nProvider", () => {
  it("defaults to Simplified Chinese when no locale has been saved", async () => {
    await renderProvider();

    expect(document.querySelector("[data-locale]")?.textContent).toBe("zh");
    expect(document.documentElement.lang).toBe("zh");
  });

  it("preserves a saved locale", async () => {
    localStorage.setItem("hermes-locale", "en");

    await renderProvider();

    expect(document.querySelector("[data-locale]")?.textContent).toBe("en");
    expect(document.documentElement.lang).toBe("en");
  });

  it("persists language changes and updates the document language", async () => {
    await renderProvider();

    await act(async () => {
      document.querySelector<HTMLButtonElement>("[data-locale]")?.click();
    });

    expect(document.querySelector("[data-locale]")?.textContent).toBe("en");
    expect(document.documentElement.lang).toBe("en");
    expect(localStorage.getItem("hermes-locale")).toBe("en");
  });
});
