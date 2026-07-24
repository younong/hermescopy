import {
  ArrowUp,
  Download,
  Ellipsis,
  FileText,
  Folder,
  FolderPlus,
  RefreshCw,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import {
  useEffect,
  useRef,
  useState,
  type DragEvent as ReactDragEvent,
} from "react";
import { createPortal } from "react-dom";

import type { ManagedFileEntry } from "@/lib/api";
import { useManagedFiles } from "../useManagedFiles";

const DATE_FORMAT = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

function formatBytes(size: number | null): string {
  if (size === null) return "—";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  if (size < 1024 * 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  return `${(size / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function transferHasFiles(event: ReactDragEvent<HTMLElement>): boolean {
  return Array.from(event.dataTransfer.types).includes("Files");
}

export function GuiChatFilesPane() {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const pathInputRef = useRef<HTMLInputElement | null>(null);
  const dragDepthRef = useRef(0);
  const [folderName, setFolderName] = useState("");
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [draggingFiles, setDraggingFiles] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<ManagedFileEntry | null>(null);
  const [openMenuPath, setOpenMenuPath] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const {
    activePath,
    canChangePath,
    canUpload,
    createDirectory,
    creating,
    deleteFile,
    deleting,
    downloadFile,
    error,
    listing,
    load,
    loading,
    setCurrentPath,
    uploading,
    uploadFiles,
  } = useManagedFiles();

  useEffect(() => {
    if (!openMenuPath) return;
    const close = () => setOpenMenuPath(null);
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, [openMenuPath]);

  const reportAction = (message: string) => {
    setActionError(null);
    setStatusMessage(message);
  };

  const submitUpload = async (files: File[]) => {
    if (files.length === 0) return;
    setActionError(null);
    setStatusMessage(null);
    try {
      await uploadFiles(files, activePath);
      reportAction(`${files.length} file${files.length === 1 ? "" : "s"} uploaded`);
    } catch (nextError) {
      setActionError(`Upload failed: ${String(nextError)}`);
    } finally {
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const submitCreate = async () => {
    setActionError(null);
    setStatusMessage(null);
    try {
      await createDirectory(folderName, activePath);
      setFolderName("");
      setCreateDialogOpen(false);
      reportAction("Folder created");
    } catch (nextError) {
      setActionError(`Create failed: ${String(nextError)}`);
    }
  };

  const submitDownload = async (entry: ManagedFileEntry) => {
    setOpenMenuPath(null);
    setActionError(null);
    setStatusMessage(null);
    try {
      await downloadFile(entry);
      reportAction(`${entry.name} downloaded`);
    } catch (nextError) {
      setActionError(`Download failed: ${String(nextError)}`);
    }
  };

  const confirmDelete = async () => {
    if (!pendingDelete) return;
    setActionError(null);
    setStatusMessage(null);
    try {
      await deleteFile(pendingDelete, activePath);
      reportAction(`${pendingDelete.name} deleted`);
      setPendingDelete(null);
    } catch (nextError) {
      setActionError(`Delete failed: ${String(nextError)}`);
    }
  };

  const goToPath = async () => {
    const nextPath = pathInputRef.current?.value.trim() ?? "";
    if (!nextPath) {
      setActionError("Path required");
      return;
    }
    await load(nextPath);
  };

  const handleDragEnter = (event: ReactDragEvent<HTMLElement>) => {
    if (!canUpload || !transferHasFiles(event)) return;
    event.preventDefault();
    dragDepthRef.current += 1;
    setDraggingFiles(true);
  };

  const handleDragOver = (event: ReactDragEvent<HTMLElement>) => {
    if (!canUpload || !transferHasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  };

  const handleDragLeave = (event: ReactDragEvent<HTMLElement>) => {
    if (!canUpload || !transferHasFiles(event)) return;
    event.preventDefault();
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setDraggingFiles(false);
  };

  const handleDrop = (event: ReactDragEvent<HTMLElement>) => {
    if (!canUpload) return;
    event.preventDefault();
    dragDepthRef.current = 0;
    setDraggingFiles(false);
    void submitUpload(Array.from(event.dataTransfer.files));
  };

  return (
    <section
      aria-label="Files"
      className="gui-chat-files-pane"
      onDragEnter={handleDragEnter}
      onDragLeave={handleDragLeave}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
    >
      <input
        aria-label="Choose files to upload"
        className="hidden"
        multiple
        onChange={(event) => void submitUpload(Array.from(event.currentTarget.files ?? []))}
        ref={fileInputRef}
        type="file"
      />

      <header className="gui-chat-files-toolbar">
        <div className="gui-chat-files-actions">
          <button
            className="gui-chat-files-primary-button"
            disabled={!activePath}
            onClick={() => setCreateDialogOpen(true)}
            type="button"
          >
            <FolderPlus aria-hidden />
            New folder
          </button>
          <button
            className="gui-chat-files-secondary-button"
            disabled={!canUpload}
            onClick={() => fileInputRef.current?.click()}
            type="button"
          >
            <Upload aria-hidden />
            {uploading ? "Uploading…" : "Upload"}
          </button>
        </div>
        <button
          aria-label="Refresh files"
          className="gui-chat-files-icon-button"
          disabled={loading}
          onClick={() => void load()}
          type="button"
        >
          <RefreshCw aria-hidden className={loading ? "animate-spin" : ""} />
        </button>
      </header>

      <div className="gui-chat-files-location">
        <div className="min-w-0">
          <h1>Files</h1>
          <p title={activePath}>{activePath || "Loading workspace…"}</p>
        </div>
        {canChangePath ? (
          <form
            className="gui-chat-files-path-form"
            onSubmit={(event) => {
              event.preventDefault();
              void goToPath();
            }}
          >
            <input
              aria-label="Path"
              defaultValue={activePath}
              key={activePath}
              placeholder="Path"
              ref={pathInputRef}
            />
            <button type="submit">Go</button>
          </form>
        ) : null}
      </div>

      {(error || actionError || statusMessage) ? (
        <div
          className={`gui-chat-files-feedback ${error || actionError ? "is-error" : ""}`}
          role={error || actionError ? "alert" : "status"}
        >
          {actionError ?? error ?? statusMessage}
        </div>
      ) : null}

      <div className="gui-chat-files-table-wrap">
        <div className="gui-chat-files-table" role="table" aria-label="Workspace files">
          <div className="gui-chat-files-table-header" role="row">
            <span role="columnheader">Name</span>
            <span role="columnheader">Size</span>
            <span role="columnheader">Modified</span>
            <span aria-label="Actions" role="columnheader" />
          </div>

          {listing && listing.parent !== null ? (
            <button
              className="gui-chat-files-row gui-chat-files-parent-row"
              onClick={() => setCurrentPath(listing.parent ?? undefined)}
              role="row"
              type="button"
            >
              <span className="gui-chat-files-name" role="cell">
                <span className="gui-chat-files-type-icon is-folder"><ArrowUp aria-hidden /></span>
                <span>Parent folder</span>
              </span>
              <span role="cell">—</span>
              <span role="cell">—</span>
              <span role="cell" />
            </button>
          ) : null}

          {loading && !listing ? (
            <div className="gui-chat-files-empty" role="status">Loading files…</div>
          ) : listing && listing.entries.length === 0 ? (
            <div className="gui-chat-files-empty">No files in this folder</div>
          ) : (
            listing?.entries.map((entry) => (
              <div className="gui-chat-files-row" key={entry.path} role="row">
                <button
                  className="gui-chat-files-name"
                  onClick={() => entry.is_directory
                    ? setCurrentPath(entry.path)
                    : void submitDownload(entry)}
                  role="cell"
                  type="button"
                >
                  <span className={`gui-chat-files-type-icon ${entry.is_directory ? "is-folder" : "is-file"}`}>
                    {entry.is_directory ? <Folder aria-hidden /> : <FileText aria-hidden />}
                  </span>
                  <span className="truncate" title={entry.name}>{entry.name}</span>
                </button>
                <span className="gui-chat-files-meta" role="cell">{formatBytes(entry.size)}</span>
                <span className="gui-chat-files-meta" role="cell">
                  {Number.isFinite(entry.mtime) ? DATE_FORMAT.format(entry.mtime * 1000) : "—"}
                </span>
                <span className="relative flex justify-end" role="cell">
                  <button
                    aria-expanded={openMenuPath === entry.path}
                    aria-haspopup="menu"
                    aria-label={`Actions for ${entry.name}`}
                    className="gui-chat-files-icon-button"
                    onClick={(event) => {
                      event.stopPropagation();
                      setOpenMenuPath((path) => path === entry.path ? null : entry.path);
                    }}
                    type="button"
                  >
                    <Ellipsis aria-hidden />
                  </button>
                  {openMenuPath === entry.path ? (
                    <div
                      className="gui-chat-files-menu"
                      onClick={(event) => event.stopPropagation()}
                      role="menu"
                    >
                      {entry.is_directory ? (
                        <button onClick={() => setCurrentPath(entry.path)} role="menuitem" type="button">
                          <Folder aria-hidden /> Open
                        </button>
                      ) : (
                        <button onClick={() => void submitDownload(entry)} role="menuitem" type="button">
                          <Download aria-hidden /> Download
                        </button>
                      )}
                      <button
                        className="is-destructive"
                        onClick={() => {
                          setOpenMenuPath(null);
                          setPendingDelete(entry);
                        }}
                        role="menuitem"
                        type="button"
                      >
                        <Trash2 aria-hidden /> Delete
                      </button>
                    </div>
                  ) : null}
                </span>
              </div>
            ))
          )}
        </div>
        {draggingFiles ? (
          <div className="gui-chat-files-drop-overlay">
            <Upload aria-hidden />
            <strong>Drop files to upload</strong>
            <span>{activePath}</span>
          </div>
        ) : null}
      </div>

      {createDialogOpen ? (
        <GuiChatFilesDialog
          busy={creating}
          description={`Create in ${activePath}`}
          onClose={() => {
            if (creating) return;
            setCreateDialogOpen(false);
            setFolderName("");
          }}
          title="New folder"
        >
          <input
            aria-label="Folder name"
            autoFocus
            className="gui-chat-files-dialog-input"
            disabled={creating}
            onChange={(event) => setFolderName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void submitCreate();
            }}
            placeholder="Folder name"
            value={folderName}
          />
          <div className="gui-chat-files-dialog-actions">
            <button disabled={creating} onClick={() => setCreateDialogOpen(false)} type="button">Cancel</button>
            <button className="is-primary" disabled={creating} onClick={() => void submitCreate()} type="button">
              {creating ? "Creating…" : "Create"}
            </button>
          </div>
        </GuiChatFilesDialog>
      ) : null}

      {pendingDelete ? (
        <GuiChatFilesDialog
          busy={deleting}
          description={pendingDelete.is_directory
            ? "This removes the folder and everything inside it."
            : "This permanently removes the file."}
          onClose={() => !deleting && setPendingDelete(null)}
          title={`Delete ${pendingDelete.name}?`}
        >
          <div className="gui-chat-files-dialog-actions">
            <button disabled={deleting} onClick={() => setPendingDelete(null)} type="button">Cancel</button>
            <button className="is-destructive" disabled={deleting} onClick={() => void confirmDelete()} type="button">
              {deleting ? "Deleting…" : "Delete"}
            </button>
          </div>
        </GuiChatFilesDialog>
      ) : null}
    </section>
  );
}

function GuiChatFilesDialog({
  busy,
  children,
  description,
  onClose,
  title,
}: {
  busy: boolean;
  children: React.ReactNode;
  description: string;
  onClose: () => void;
  title: string;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  return createPortal(
    <div className="gui-chat-files-dialog-backdrop" data-gui-chat role="presentation">
      <div aria-labelledby="gui-chat-files-dialog-title" aria-modal="true" className="gui-chat-files-dialog" role="dialog">
        <button aria-label="Close" className="gui-chat-files-dialog-close" disabled={busy} onClick={onClose} type="button">
          <X aria-hidden />
        </button>
        <h2 id="gui-chat-files-dialog-title">{title}</h2>
        <p>{description}</p>
        {children}
      </div>
    </div>,
    document.body,
  );
}
