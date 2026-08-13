// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ComposerReasoningPicker } from "./ComposerReasoningPicker";

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML = "<div id=\"root\"></div>";
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
});

describe("ComposerReasoningPicker", () => {
  it("renders supported levels and changes the current session level", async () => {
    const onChange = vi.fn().mockResolvedValue(undefined);
    await render({ currentLevel: "high", levels: ["high", "xhigh", "max"], onChange });

    await act(async () => trigger().click());
    expect(document.body.textContent).toContain("XHigh");
    expect(document.body.textContent).toContain("Max");

    await act(async () => {
      option("Max").click();
      await Promise.resolve();
    });
    expect(onChange).toHaveBeenCalledWith("max");
  });

  it("shows no active level when the session uses an unlisted effort", async () => {
    const onChange = vi.fn().mockResolvedValue(undefined);
    await render({ currentLevel: "medium", levels: ["high", "xhigh"], onChange });

    expect(trigger().textContent).toContain("Reasoning levels");
    await act(async () => trigger().click());
    await act(async () => {
      option("High").click();
      await Promise.resolve();
    });
    expect(onChange).toHaveBeenCalledWith("high");
  });

  it("stays hidden when the active model declares no reasoning levels", async () => {
    await render({ levels: [] });
    expect(document.querySelector("[data-composer-reasoning-picker]")).toBeNull();
  });
});

async function render(overrides: Partial<Parameters<typeof ComposerReasoningPicker>[0]>) {
  root = createRoot(document.getElementById("root")!);
  await act(async () => {
    root?.render(
      <ComposerReasoningPicker
        busy={false}
        levels={["high", "xhigh"]}
        onChange={vi.fn().mockResolvedValue(undefined)}
        {...overrides}
      />,
    );
  });
}

function trigger(): HTMLButtonElement {
  return document.querySelector<HTMLButtonElement>('button[aria-label="Change reasoning level"]')!;
}

function option(text: string): HTMLButtonElement {
  return Array.from(document.querySelectorAll<HTMLButtonElement>('[role="option"]'))
    .find((item) => item.textContent?.includes(text))!;
}
