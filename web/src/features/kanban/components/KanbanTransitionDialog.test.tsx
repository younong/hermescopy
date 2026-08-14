// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { KanbanTransitionDialog } from "./KanbanTransitionDialog";

vi.mock("@/i18n", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/i18n")>();
  const { en } = await import("@/i18n/en");
  return { ...actual, useI18n: () => ({ locale: "en", t: en }) };
});

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML = "";
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
});

describe("KanbanTransitionDialog", () => {
  it("requires a completion summary before submitting done", async () => {
    const onConfirm = vi.fn();
    const container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => root?.render(<KanbanTransitionDialog busy={false} onClose={vi.fn()} onConfirm={onConfirm} request={{ ids: ["T-1"], status: "done" }} />));

    const submit = buttonNamed(document.body, "Apply");
    expect(submit?.disabled).toBe(true);
    changeValue(document.body.querySelector("textarea"), "Implemented and tested");
    expect(submit?.disabled).toBe(false);
    await act(async () => submit?.click());
    expect(onConfirm).toHaveBeenCalledWith("Implemented and tested");
  });

  it("collects a reason for scheduled transitions", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => root?.render(<KanbanTransitionDialog busy={false} onClose={vi.fn()} onConfirm={vi.fn()} request={{ ids: ["T-2"], status: "scheduled" }} />));

    expect(document.body.textContent).toContain("known time delay");
    expect(document.body.querySelector("textarea")?.getAttribute("placeholder")).toContain("blocked or scheduled");
  });
});

function changeValue(element: Element | null, value: string) {
  if (!(element instanceof HTMLTextAreaElement)) throw new Error("Expected textarea");
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
  act(() => {
    setter?.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

function buttonNamed(rootNode: ParentNode, text: string) {
  return Array.from(rootNode.querySelectorAll<HTMLButtonElement>("button")).find((button) => button.textContent?.trim() === text);
}
