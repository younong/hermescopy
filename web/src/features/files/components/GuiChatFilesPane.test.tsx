// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ManagedFilesResponse } from "@/lib/api";
import { GuiChatFilesPane } from "./GuiChatFilesPane";

const mocks = vi.hoisted(() => ({
  createDirectory: vi.fn(),
  deleteFile: vi.fn(),
  downloadFile: vi.fn(),
  load: vi.fn(),
  setCurrentPath: vi.fn(),
  uploadFiles: vi.fn(),
}));

let listing: ManagedFilesResponse;
let canChangePath = true;

vi.mock("../useManagedFiles", () => ({
  useManagedFiles: () => ({
    activePath: listing.path,
    canChangePath,
    canUpload: true,
    createDirectory: mocks.createDirectory,
    creating: false,
    deleteFile: mocks.deleteFile,
    deleting: false,
    downloadFile: mocks.downloadFile,
    error: null,
    listing,
    load: mocks.load,
    loading: false,
    setCurrentPath: mocks.setCurrentPath,
    uploading: false,
    uploadFiles: mocks.uploadFiles,
  }),
}));

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  listing = {
    can_change_path: true,
    entries: [
      {
        is_directory: true,
        mime_type: null,
        mtime: 1_700_000_000,
        name: "reports",
        path: "/workspace/reports",
        size: null,
      },
      {
        is_directory: false,
        mime_type: "text/plain",
        mtime: 1_700_000_100,
        name: "notes.txt",
        path: "/workspace/notes.txt",
        size: 2048,
      },
    ],
    locked_root: "/workspace",
    parent: "/",
    path: "/workspace",
    root: "/workspace",
  };
  canChangePath = true;
  Object.values(mocks).forEach((mock) => mock.mockReset());
  mocks.createDirectory.mockResolvedValue(undefined);
  mocks.deleteFile.mockResolvedValue(undefined);
  mocks.downloadFile.mockResolvedValue(undefined);
  mocks.load.mockResolvedValue(undefined);
  mocks.uploadFiles.mockResolvedValue(undefined);
  document.body.innerHTML = "";
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
});

describe("GuiChatFilesPane", () => {
  it("navigates folders, downloads files, and uploads selected files", async () => {
    const container = await renderPane();

    await act(async () => {
      buttonNamed(container, "reports")?.click();
      buttonNamed(container, "notes.txt")?.click();
      await Promise.resolve();
    });

    expect(mocks.setCurrentPath).toHaveBeenCalledWith("/workspace/reports");
    expect(mocks.downloadFile).toHaveBeenCalledWith(listing.entries[1]);

    const file = new File(["draft"], "draft.txt", { type: "text/plain" });
    const input = container.querySelector<HTMLInputElement>('input[type="file"]');
    Object.defineProperty(input, "files", { configurable: true, value: [file] });
    await act(async () => {
      input?.dispatchEvent(new Event("change", { bubbles: true }));
      await Promise.resolve();
    });

    expect(mocks.uploadFiles).toHaveBeenCalledWith([file], "/workspace");
    expect(container.textContent).toContain("1 file uploaded");
  });

  it("creates a folder and confirms deletion from the row menu", async () => {
    const container = await renderPane();

    await act(async () => buttonNamed(container, "New folder")?.click());
    const folderInput = document.body.querySelector<HTMLInputElement>('input[aria-label="Folder name"]');
    await act(async () => {
      if (folderInput) {
        const valueSetter = Object.getOwnPropertyDescriptor(
          HTMLInputElement.prototype,
          "value",
        )?.set;
        valueSetter?.call(folderInput, "archive");
        folderInput.dispatchEvent(new Event("input", { bubbles: true }));
      }
    });
    await act(async () => {
      buttonNamed(document.body, "Create")?.click();
      await Promise.resolve();
    });

    expect(mocks.createDirectory).toHaveBeenCalledWith("archive", "/workspace");

    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Actions for notes.txt"]')?.click());
    await act(async () => buttonNamed(container, "Delete")?.click());
    expect(document.body.textContent).toContain("Delete notes.txt?");
    await act(async () => {
      buttonNamed(document.body, "Delete")?.click();
      await Promise.resolve();
    });

    expect(mocks.deleteFile).toHaveBeenCalledWith(listing.entries[1], "/workspace");
  });

  it("navigates to an empty-string workspace parent", async () => {
    listing.path = "default";
    listing.parent = "";
    const container = await renderPane();

    await act(async () => container.querySelector<HTMLButtonElement>(".gui-chat-files-parent-row")?.click());

    expect(mocks.setCurrentPath).toHaveBeenCalledWith("");
  });

  it("hides arbitrary path entry when the backend restricts navigation", async () => {
    canChangePath = false;
    const container = await renderPane();

    expect(container.querySelector('input[aria-label="Path"]')).toBeNull();
    expect(container.textContent).toContain("/workspace");
  });
});

async function renderPane() {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(<GuiChatFilesPane />);
    await Promise.resolve();
  });
  return container;
}

function buttonNamed(rootNode: ParentNode, text: string) {
  return Array.from(rootNode.querySelectorAll<HTMLButtonElement>("button"))
    .find((button) => button.textContent?.trim() === text);
}
