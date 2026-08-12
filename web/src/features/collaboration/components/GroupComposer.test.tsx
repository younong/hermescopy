// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CollaborationMembership } from "../types";
import { GroupComposer } from "./GroupComposer";

const memberships: CollaborationMembership[] = [
  {
    employee_id: "employee-a",
    created_at: 1,
    group_id: "group-a",
    join_sequence: 1,
    leave_sequence: null,
    left_at: null,
    membership_id: "membership-a",
    profile_fingerprint: "fingerprint-a",
    profile_revision: 1,
    role: "Researcher",
  },
  {
    employee_id: "employee-b",
    created_at: 1,
    group_id: "group-a",
    join_sequence: 2,
    leave_sequence: null,
    left_at: null,
    membership_id: "membership-b",
    profile_fingerprint: "fingerprint-b",
    profile_revision: 1,
    role: "Writer",
  },
];

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML = "";
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
});

describe("GroupComposer mention picker", () => {
  it("opens for @, allows multiple selections, and closes on Enter without sending", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const container = renderComposer(onSubmit);
    const textarea = getTextarea(container);

    await setTextareaValue(textarea, "Please ask @");

    expect(getPicker(container)).not.toBeNull();
    const checkboxes = container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]');
    await click(checkboxes[0]);
    await click(checkboxes[1]);
    expect(Array.from(checkboxes, (checkbox) => checkbox.checked)).toEqual([true, true]);

    await keyDown(textarea, "Enter");

    expect(getPicker(container)).toBeNull();
    expect(textarea.value).toBe("Please ask ");
    expect(container.textContent).toContain("@Alice, @Bob");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("removes the trigger and keeps @all as structured selection on Enter", async () => {
    const container = renderComposer();
    const textarea = getTextarea(container);

    await setTextareaValue(textarea, "Ask everyone @");
    const allButton = container.querySelector<HTMLButtonElement>('button[aria-pressed="false"]');
    expect(allButton?.textContent).toContain("@all");
    await click(allButton);
    await keyDown(textarea, "Enter");

    expect(textarea.value).toBe("Ask everyone ");
    expect(container.textContent).toContain("@all");
    expect(getPicker(container)).toBeNull();
  });

  it("keeps the literal @ only when selection is explicitly cancelled", async () => {
    const container = renderComposer();
    const textarea = getTextarea(container);

    await setTextareaValue(textarea, "Keep this @");
    await click(container.querySelector('button[aria-label="Close employee mentions"]'));

    expect(textarea.value).toBe("Keep this @");
    expect(getPicker(container)).toBeNull();

    const mentionButton = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Choose employee mentions"]',
    );
    await click(mentionButton);
    await act(async () => document.body.dispatchEvent(new Event("pointerdown", { bubbles: true })));
    expect(getPicker(container)).toBeNull();
  });
});

function renderComposer(onSubmit = vi.fn().mockResolvedValue(undefined)) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <GroupComposer
        archived={false}
        defaultSelection={{ mentionAll: false, membershipIds: ["membership-a"] }}
        disabled={false}
        employeeName={(employeeId) => employeeId === "employee-a" ? "Alice" : "Bob"}
        memberships={memberships}
        onSubmit={onSubmit}
        onUpload={vi.fn()}
      />,
    );
  });
  return container;
}

function getTextarea(container: HTMLElement): HTMLTextAreaElement {
  const textarea = container.querySelector<HTMLTextAreaElement>('textarea[aria-label="Group message"]');
  if (!textarea) throw new Error("Group message textarea not found");
  return textarea;
}

function getPicker(container: HTMLElement): HTMLElement | null {
  return container.querySelector('[role="dialog"][aria-label="Employee mentions"]');
}

async function setTextareaValue(textarea: HTMLTextAreaElement, value: string) {
  await act(async () => {
    const valueSetter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    valueSetter?.call(textarea, value);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function click(target: EventTarget | null | undefined) {
  if (!target) throw new Error("Click target not found");
  await act(async () => target.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true })));
}

async function keyDown(target: EventTarget, key: string) {
  await act(async () => target.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key })));
}
