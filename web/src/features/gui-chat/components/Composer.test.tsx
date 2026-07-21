// @vitest-environment jsdom

import { act, type ComponentProps } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { IMAGE_ATTACHMENT_MAX_BYTES } from "../attachments";
import { Composer } from "./Composer";

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:preview");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
  document.body.innerHTML = "";
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("Composer attachment transfers", () => {
  it("queues pasted files and prevents the browser paste", async () => {
    const container = renderComposer();
    const textarea = getTextarea(container);
    const image = new File(["image"], "shot.png", { type: "image/png" });
    const documentFile = new File(["notes"], "notes.txt", { type: "text/plain" });

    const event = transferEvent("paste", transfer([image, documentFile]));
    await dispatch(textarea, event);

    expect(event.defaultPrevented).toBe(true);
    expect(container.querySelector('[title="shot.png"]')).not.toBeNull();
    expect(container.querySelector('[title="notes.txt"]')).not.toBeNull();
    expect(URL.createObjectURL).toHaveBeenCalledWith(image);
  });

  it("leaves normal text paste untouched", async () => {
    const container = renderComposer();
    const textarea = getTextarea(container);
    const event = transferEvent("paste", transfer([], ["text/plain"]));

    await dispatch(textarea, event);

    expect(event.defaultPrevented).toBe(false);
    expect(container.querySelector('[aria-label^="Remove "]')).toBeNull();
  });

  it("queues dropped files but ignores non-file drags", async () => {
    const container = renderComposer();
    const dropTarget = getDropTarget(container);
    const first = new File(["one"], "one.txt", { type: "text/plain" });
    const second = new File(["two"], "two.pdf", { type: "application/pdf" });
    const textDrop = transferEvent("drop", transfer([], ["text/plain"]));

    await dispatch(dropTarget, textDrop);
    expect(textDrop.defaultPrevented).toBe(false);

    const fileDrop = transferEvent("drop", transfer([first, second]));
    await dispatch(dropTarget, fileDrop);

    expect(fileDrop.defaultPrevented).toBe(true);
    expect(container.querySelector('[title="one.txt"]')).not.toBeNull();
    expect(container.querySelector('[title="two.pdf"]')).not.toBeNull();
  });

  it("keeps drag feedback stable across nested elements and clears it on drop", async () => {
    const container = renderComposer();
    const dropTarget = getDropTarget(container);
    const textarea = getTextarea(container);
    const dataTransfer = transfer([], ["Files"]);

    await dispatch(dropTarget, transferEvent("dragenter", dataTransfer));
    expect(container.textContent).toContain("Drop files to attach");

    await dispatch(textarea, transferEvent("dragenter", dataTransfer));
    await dispatch(textarea, transferEvent("dragleave", dataTransfer));
    expect(container.textContent).toContain("Drop files to attach");

    await dispatch(dropTarget, transferEvent("drop", dataTransfer));
    expect(container.textContent).not.toContain("Drop files to attach");
  });

  it.each([
    { disabled: true, isGenerating: false, label: "disabled" },
    { disabled: false, isGenerating: true, label: "generating" },
  ])("safely rejects file drops while $label", async ({ disabled, isGenerating }) => {
    const container = renderComposer({ disabled, isGenerating });
    const file = new File(["blocked"], "blocked.txt", { type: "text/plain" });
    const event = transferEvent("drop", transfer([file]));

    await dispatch(getDropTarget(container), event);

    expect(event.defaultPrevented).toBe(true);
    expect(container.querySelector('[title="blocked.txt"]')).toBeNull();
  });

  it("does not accept new drops while a message is submitting", async () => {
    let resolveSend: (() => void) | undefined;
    const onSend = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveSend = resolve;
        }),
    );
    const container = renderComposer({ onSend });
    const dropTarget = getDropTarget(container);
    await dispatch(
      dropTarget,
      transferEvent("drop", transfer([new File(["first"], "first.txt", { type: "text/plain" })])),
    );

    const sendButton = Array.from(container.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("Send"),
    );
    await dispatch(sendButton ?? null, new MouseEvent("click", { bubbles: true, cancelable: true }));
    expect(onSend).toHaveBeenCalledOnce();

    const blockedDrop = transferEvent(
      "drop",
      transfer([new File(["second"], "second.txt", { type: "text/plain" })]),
    );
    await dispatch(dropTarget, blockedDrop);

    expect(blockedDrop.defaultPrevented).toBe(true);
    expect(container.querySelector('[title="first.txt"]')).not.toBeNull();
    expect(container.querySelector('[title="second.txt"]')).toBeNull();

    await act(async () => resolveSend?.());
  });

  it("applies existing attachment validation to transferred files", async () => {
    const container = renderComposer();
    const oversized = new File(["image"], "too-large.png", { type: "image/png" });
    Object.defineProperty(oversized, "size", { value: IMAGE_ATTACHMENT_MAX_BYTES + 1 });

    await dispatch(getDropTarget(container), transferEvent("drop", transfer([oversized])));

    expect(container.textContent).toContain("too-large.png 超过 25MB，无法上传。");
    expect(container.querySelector('[title="too-large.png"]')).toBeNull();
  });
});

function renderComposer({
  disabled = false,
  isGenerating = false,
  onSend = vi.fn().mockResolvedValue(undefined),
}: {
  disabled?: boolean;
  isGenerating?: boolean;
  onSend?: ComponentProps<typeof Composer>["onSend"];
} = {}) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <Composer
        disabled={disabled}
        isGenerating={isGenerating}
        onSend={onSend}
        onStop={vi.fn()}
      />,
    );
  });
  return container;
}

function getTextarea(container: HTMLElement): HTMLTextAreaElement {
  const textarea = container.querySelector<HTMLTextAreaElement>(
    'textarea[aria-label="GUI chat message"]',
  );
  if (!textarea) throw new Error("Composer textarea not found");
  return textarea;
}

function getDropTarget(container: HTMLElement): HTMLElement {
  const target = getTextarea(container).parentElement;
  if (!target) throw new Error("Composer drop target not found");
  return target;
}

function transfer(files: File[], types = files.length > 0 ? ["Files"] : []): DataTransfer {
  return {
    dropEffect: "none",
    files,
    items: files.map((file) => ({ getAsFile: () => file, kind: "file" })),
    types,
  } as unknown as DataTransfer;
}

function transferEvent(type: string, dataTransfer: DataTransfer): Event {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, type === "paste" ? "clipboardData" : "dataTransfer", {
    value: dataTransfer,
  });
  return event;
}

async function dispatch(target: EventTarget | null, event: Event) {
  if (!target) throw new Error("Event target not found");
  await act(async () => {
    target.dispatchEvent(event);
  });
}
