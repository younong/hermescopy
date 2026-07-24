import { useCallback, useEffect, useRef, useState } from "react";

import { api, type ManagedFileEntry, type ManagedFilesResponse } from "@/lib/api";

export function joinManagedFilePath(base: string, name: string): string {
  const cleanName = name.trim().replace(/^[\\/]+/, "");
  if (!cleanName) return base;
  const separator = base.includes("\\") && !base.includes("/") ? "\\" : "/";
  if (!base || base.endsWith("/") || base.endsWith("\\")) return `${base}${cleanName}`;
  return `${base}${separator}${cleanName}`;
}

export function downloadManagedFileDataUrl(dataUrl: string, name: string) {
  const link = document.createElement("a");
  link.href = dataUrl;
  link.download = name || "download";
  document.body.appendChild(link);
  link.click();
  link.remove();
}

export function useManagedFiles() {
  const [currentPath, setCurrentPath] = useState<string | undefined>(undefined);
  const [listing, setListing] = useState<ManagedFilesResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestRef = useRef(0);

  const load = useCallback(async (path?: string) => {
    const request = ++requestRef.current;
    setLoading(true);
    setError(null);
    try {
      const result = await api.listFiles(path);
      if (request !== requestRef.current) return;
      setListing(result);
      setCurrentPath(result.path);
    } catch (nextError) {
      if (request !== requestRef.current) return;
      setError(String(nextError));
    } finally {
      if (request === requestRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    // Existing dashboard data pages fetch from effects; keep this local and explicit
    // until the shared lint profile is updated for async page loaders.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load(currentPath).catch(() => undefined);
  }, [currentPath, load]);

  const activePath = listing?.path ?? currentPath ?? "";

  const uploadFiles = useCallback(async (files: File[], path: string) => {
    if (files.length === 0) return;
    if (!path) throw new Error("Directory unavailable");
    setUploading(true);
    try {
      for (const file of files) {
        await api.uploadFile(joinManagedFilePath(path, file.name), file, true);
      }
      await load(path);
    } finally {
      setUploading(false);
    }
  }, [load]);

  const createDirectory = useCallback(async (name: string, path: string) => {
    if (!path) throw new Error("Directory unavailable");
    if (!name.trim()) throw new Error("Folder name required");
    setCreating(true);
    try {
      await api.createDirectory(joinManagedFilePath(path, name));
      await load(path);
    } finally {
      setCreating(false);
    }
  }, [load]);

  const downloadFile = useCallback(async (entry: ManagedFileEntry) => {
    if (entry.is_directory) return;
    const file = await api.readFile(entry.path);
    downloadManagedFileDataUrl(file.data_url, file.name);
  }, []);

  const deleteFile = useCallback(async (entry: ManagedFileEntry, path: string) => {
    setDeleting(true);
    try {
      await api.deleteFile(entry.path, entry.is_directory);
      await load(path);
    } finally {
      setDeleting(false);
    }
  }, [load]);

  return {
    activePath,
    canChangePath: listing?.can_change_path ?? false,
    canUpload: Boolean(activePath) && !uploading,
    createDirectory,
    creating,
    currentPath,
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
  };
}
