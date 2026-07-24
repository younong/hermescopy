import {
  useEffect,
  useRef,
  useState,
  type DragEvent as ReactDragEvent,
} from "react";
import {
  ArrowUp,
  Download,
  FileIcon,
  Folder,
  FolderOpen,
  FolderPlus,
  RefreshCw,
  Trash2,
  Upload,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@nous-research/ui/ui/components/dialog";
import { Input } from "@nous-research/ui/ui/components/input";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { DeleteConfirmDialog } from "@/components/DeleteConfirmDialog";
import { usePageHeader } from "@/contexts/usePageHeader";
import { useManagedFiles } from "@/features/files/useManagedFiles";
import type { ManagedFileEntry } from "@/lib/api";
import { PluginSlot } from "@/plugins";

const DATE_FORMAT = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

function formatBytes(size: number | null): string {
  if (size === null) return "-";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  if (size < 1024 * 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  return `${(size / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function displayPath(path: string | null | undefined): string {
  return path?.trim() || "Files";
}

function transferHasFiles(event: ReactDragEvent<HTMLElement>): boolean {
  return Array.from(event.dataTransfer.types).includes("Files");
}

export default function FilesPage() {
  const { toast, showToast } = useToast();
  const { setAfterTitle, setEnd } = usePageHeader();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const dragDepthRef = useRef(0);
  const [pathInput, setPathInput] = useState("");
  const [draggingFiles, setDraggingFiles] = useState(false);
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [folderName, setFolderName] = useState("");
  const [pendingDelete, setPendingDelete] = useState<ManagedFileEntry | null>(null);
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
  const headerPath = displayPath(listing?.locked_root ?? listing?.path);

  useEffect(() => {
    setAfterTitle(
      <Badge tone="outline" className="max-w-[22rem] truncate text-xs" title={headerPath}>
        {headerPath}
      </Badge>,
    );
    setEnd(
      <div className="flex items-center gap-2">
        <Button
          ghost
          size="icon"
          type="button"
          onClick={() => void load()}
          disabled={loading}
          aria-label="Refresh files"
        >
          {loading ? <Spinner /> : <RefreshCw />}
        </Button>
      </div>,
    );
    return () => {
      setAfterTitle(null);
      setEnd(null);
    };
  }, [headerPath, load, loading, setAfterTitle, setEnd]);

  const openDirectory = (entry: ManagedFileEntry) => {
    if (entry.is_directory) {
      setCurrentPath(entry.path);
    }
  };

  const goToPath = async () => {
    const nextPath = pathInput.trim() || activePath;
    if (!nextPath) {
      showToast("Path required", "error");
      return;
    }
    await load(nextPath);
    setPathInput("");
  };

  const submitCreateDirectory = async () => {
    try {
      await createDirectory(folderName, activePath);
      setFolderName("");
      setCreateDialogOpen(false);
      showToast("Folder created", "success");
    } catch (e) {
      showToast(`Create failed: ${e}`, "error");
    }
  };

  const submitUploadFiles = async (files: FileList | null) => {
    if (!files?.length) return;
    try {
      await uploadFiles(Array.from(files), activePath);
      showToast(`${files.length} file${files.length === 1 ? "" : "s"} uploaded`, "success");
    } catch (e) {
      showToast(`Upload failed: ${e}`, "error");
    } finally {
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
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
    if (dragDepthRef.current === 0) {
      setDraggingFiles(false);
    }
  };

  const handleDrop = (event: ReactDragEvent<HTMLElement>) => {
    if (!canUpload) return;
    event.preventDefault();
    dragDepthRef.current = 0;
    setDraggingFiles(false);
    void submitUploadFiles(event.dataTransfer.files);
  };

  const submitDownloadFile = async (entry: ManagedFileEntry) => {
    try {
      await downloadFile(entry);
    } catch (e) {
      showToast(`Download failed: ${e}`, "error");
    }
  };

  const confirmDelete = async () => {
    if (!pendingDelete) return;
    try {
      await deleteFile(pendingDelete, activePath);
      showToast("Deleted", "success");
      setPendingDelete(null);
    } catch (e) {
      showToast(`Delete failed: ${e}`, "error");
    }
  };

  return (
    <div className="flex min-w-0 max-w-full flex-col gap-4">
      <Toast toast={toast} />
      <PluginSlot name="files:top" />
      <input
        ref={fileInputRef}
        type="file"
        multiple
        className="hidden"
        onChange={(event) => void submitUploadFiles(event.currentTarget.files)}
      />

      <div className="flex min-w-0 flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
        {canChangePath ? (
          <form
            className="flex min-w-0 flex-1 items-center gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              void goToPath();
            }}
          >
            <Input
              value={pathInput || activePath}
              onChange={(event) => setPathInput(event.target.value)}
              aria-label="Path"
              placeholder="Path"
              className="h-9 min-w-0 flex-1 font-mono"
            />
            <Button type="submit" size="sm" outlined className="uppercase">
              Go
            </Button>
          </form>
        ) : (
          <div className="min-w-0 truncate font-mono text-sm text-text-secondary" title={activePath}>
            {activePath}
          </div>
        )}
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <Button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={!canUpload}
            size="sm"
            outlined
            className="uppercase"
            prefix={uploading ? <Spinner /> : <Upload />}
          >
            Upload
          </Button>
          <Button
            type="button"
            onClick={() => setCreateDialogOpen(true)}
            disabled={!activePath}
            size="sm"
            outlined
            className="uppercase"
            prefix={<FolderPlus />}
          >
            Create
          </Button>
        </div>
      </div>

      <button
        type="button"
        onClick={() => canUpload && fileInputRef.current?.click()}
        onDragEnter={handleDragEnter}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        disabled={!canUpload}
        aria-label="Upload files"
        className={`flex min-h-20 w-full min-w-0 items-center justify-between gap-4 border border-dashed px-4 py-3 text-left transition ${
          draggingFiles
            ? "border-primary bg-primary/10 text-foreground"
            : "border-border bg-background/20 text-text-secondary hover:border-text-tertiary hover:bg-background/35"
        } disabled:cursor-not-allowed disabled:opacity-60`}
      >
        <span className="flex min-w-0 items-center gap-3">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center border border-border bg-background/45 text-text-tertiary">
            {uploading ? <Spinner /> : <Upload className="h-4 w-4" />}
          </span>
          <span className="min-w-0">
            <span className="block text-sm font-semibold uppercase tracking-[0.08em] text-foreground">
              {uploading ? "Uploading" : draggingFiles ? "Release to upload" : "Drop files here"}
            </span>
            <span className="block truncate font-mono text-xs text-text-secondary" title={activePath}>
              {activePath || "Loading"}
            </span>
          </span>
        </span>
        <span className="hidden shrink-0 text-xs font-semibold uppercase tracking-[0.08em] text-text-tertiary sm:block">
          Choose files
        </span>
      </button>

      <Card className="min-w-0 max-w-full overflow-hidden">
        <CardContent className="overflow-x-auto p-0">
          {error && (
            <div className="border-b border-destructive/20 bg-destructive/10 p-3 text-sm text-destructive">
              {error}
            </div>
          )}

          <div className="grid min-w-[42rem] grid-cols-[minmax(12rem,1fr)_7rem_10rem_5.5rem] items-center gap-3 border-b border-border px-4 py-2 text-xs font-semibold uppercase tracking-[0.08em] text-text-tertiary">
            <span>Name</span>
            <span>Size</span>
            <span>Modified</span>
            <span className="text-right">Actions</span>
          </div>

          {listing && listing.parent !== null && (
            <button
              type="button"
              onClick={() => setCurrentPath(listing.parent ?? undefined)}
              className="grid w-full min-w-[42rem] grid-cols-[minmax(12rem,1fr)_7rem_10rem_5.5rem] items-center gap-3 border-b border-border/60 px-4 py-2 text-left text-sm transition hover:bg-background/40"
            >
              <span className="flex min-w-0 items-center gap-2 font-mono text-text-secondary">
                <ArrowUp className="h-4 w-4 shrink-0 text-text-tertiary" />
                ..
              </span>
              <span />
              <span />
              <span />
            </button>
          )}

          {loading && !listing ? (
            <div className="flex items-center justify-center gap-2 py-12 text-sm text-muted-foreground">
              <Spinner />
              Loading files...
            </div>
          ) : listing && listing.entries.length === 0 ? (
            <div className="py-12 text-center text-sm text-muted-foreground">No files</div>
          ) : (
            listing?.entries.map((entry) => (
              <div
                key={entry.path}
                className="grid min-w-[42rem] grid-cols-[minmax(12rem,1fr)_7rem_10rem_5.5rem] items-center gap-3 border-b border-border/60 px-4 py-2 text-sm last:border-b-0 hover:bg-background/35"
              >
                <button
                  type="button"
                  onClick={() => (entry.is_directory ? openDirectory(entry) : void submitDownloadFile(entry))}
                  className="flex min-w-0 items-center gap-2 text-left font-mono text-foreground"
                >
                  {entry.is_directory ? (
                    <Folder className="h-4 w-4 shrink-0 text-warning" />
                  ) : (
                    <FileIcon className="h-4 w-4 shrink-0 text-text-tertiary" />
                  )}
                  <span className="truncate">{entry.name}</span>
                </button>
                <span className="text-xs tabular-nums text-text-secondary">{formatBytes(entry.size)}</span>
                <span className="truncate text-xs text-text-secondary">
                  {Number.isFinite(entry.mtime) ? DATE_FORMAT.format(entry.mtime * 1000) : "-"}
                </span>
                <span className="flex justify-end gap-1">
                  {entry.is_directory ? (
                    <Button
                      ghost
                      size="icon"
                      type="button"
                      onClick={() => openDirectory(entry)}
                      aria-label={`Open ${entry.name}`}
                    >
                      <FolderOpen />
                    </Button>
                  ) : (
                    <Button
                      ghost
                      size="icon"
                      type="button"
                      onClick={() => void submitDownloadFile(entry)}
                      aria-label={`Download ${entry.name}`}
                    >
                      <Download />
                    </Button>
                  )}
                  <Button
                    ghost
                    size="icon"
                    type="button"
                    onClick={() => setPendingDelete(entry)}
                    aria-label={`Delete ${entry.name}`}
                    className="text-destructive hover:text-destructive"
                  >
                    <Trash2 />
                  </Button>
                </span>
              </div>
            ))
          )}
        </CardContent>
      </Card>

      <PluginSlot name="files:bottom" />

      <Dialog
        open={createDialogOpen}
        onOpenChange={(open) => {
          if (creating) return;
          setCreateDialogOpen(open);
          if (!open) setFolderName("");
        }}
      >
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Create folder</DialogTitle>
            <DialogDescription>
              Target: {activePath || "Loading"}
            </DialogDescription>
          </DialogHeader>
          <div className="p-4">
            <Input
              autoFocus
              value={folderName}
              onChange={(event) => setFolderName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") void submitCreateDirectory();
              }}
              placeholder="Folder name"
              disabled={creating}
            />
          </div>
          <DialogFooter>
            <Button
              type="button"
              outlined
              onClick={() => {
                setCreateDialogOpen(false);
                setFolderName("");
              }}
              disabled={creating}
            >
              Cancel
            </Button>
            <Button
              type="button"
              onClick={() => void submitCreateDirectory()}
              disabled={creating}
              prefix={creating ? <Spinner /> : <FolderPlus />}
            >
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <DeleteConfirmDialog
        open={Boolean(pendingDelete)}
        loading={deleting}
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => void confirmDelete()}
        title={pendingDelete ? `Delete ${pendingDelete.name}?` : "Delete item?"}
        description={
          pendingDelete?.is_directory
            ? "This removes the folder and everything inside it."
            : "This removes the file."
        }
      />
    </div>
  );
}
