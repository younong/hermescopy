import {
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type ClipboardEvent,
  type DragEvent,
  type KeyboardEvent,
} from "react";
import { ArrowUp, Plus, Square } from "lucide-react";

import {
  COMPOSER_ATTACHMENT_MAX_COUNT,
  validateComposerAttachment,
} from "../attachments";
import type { GuiComposerAttachment } from "../types";
import { ComposerAttachmentCard } from "./ComposerAttachmentCard";

const ATTACHMENT_ACCEPT = "*/*";

export function Composer({
  disabled,
  isGenerating,
  allowSendWhileGenerating = false,
  onSend,
  onStop,
}: {
  disabled?: boolean;
  isGenerating: boolean;
  allowSendWhileGenerating?: boolean;
  onSend: (
    text: string,
    attachments: GuiComposerAttachment[],
    updateAttachment: (id: string, patch: Partial<GuiComposerAttachment>) => void,
  ) => Promise<void>;
  onStop: () => void;
}) {
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<GuiComposerAttachment[]>([]);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isDraggingFiles, setIsDraggingFiles] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const attachmentsRef = useRef<GuiComposerAttachment[]>([]);
  const dragDepthRef = useRef(0);

  useEffect(() => {
    attachmentsRef.current = attachments;
  }, [attachments]);

  useEffect(() => {
    return () => {
      for (const attachment of attachmentsRef.current) {
        if (attachment.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
      }
    };
  }, []);

  const generationBlocksSend = isGenerating && !allowSendWhileGenerating;
  const canSend =
    (text.trim().length > 0 || attachments.length > 0) &&
    !disabled &&
    !generationBlocksSend &&
    !isSubmitting;
  const controlsDisabled = disabled || generationBlocksSend || isSubmitting;

  const updateAttachment = (id: string, patch: Partial<GuiComposerAttachment>) => {
    setAttachments((current) =>
      current.map((attachment) =>
        attachment.id === id ? { ...attachment, ...patch } : attachment,
      ),
    );
  };

  const clearAttachments = (
    items: GuiComposerAttachment[],
    options: { revokePreviewUrls?: boolean } = {},
  ) => {
    const shouldRevoke = options.revokePreviewUrls ?? true;
    if (shouldRevoke) {
      for (const attachment of items) {
        if (attachment.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
      }
    }
    setAttachments([]);
  };

  const submit = async () => {
    const next = text.trim();
    if (
      (!next && attachments.length === 0) ||
      disabled ||
      generationBlocksSend ||
      isSubmitting
    ) return;

    const attachmentsToSend = attachments;
    setLocalError(null);
    setIsSubmitting(true);
    try {
      await onSend(next || "请查看这些附件。", attachmentsToSend, updateAttachment);
      setText("");
      clearAttachments(attachmentsToSend, { revokePreviewUrls: false });
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : String(error));
    } finally {
      setIsSubmitting(false);
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey) return;
    event.preventDefault();
    void submit();
  };

  const addFiles = (files: FileList | File[]) => {
    const nextAttachments: GuiComposerAttachment[] = [];
    const errors: string[] = [];
    const availableSlots = COMPOSER_ATTACHMENT_MAX_COUNT - attachments.length;

    for (const file of Array.from(files)) {
      const validation = validateComposerAttachment(file);
      if (!validation.ok) {
        errors.push(validation.message);
        continue;
      }
      if (nextAttachments.length >= availableSlots) {
        errors.push(`每条消息最多添加 ${COMPOSER_ATTACHMENT_MAX_COUNT} 个附件。`);
        break;
      }

      nextAttachments.push({
        file,
        id: createClientId("attachment"),
        kind: validation.kind,
        mimeType: file.type,
        name: file.name,
        previewUrl: validation.kind === "image" ? URL.createObjectURL(file) : undefined,
        sizeBytes: file.size,
        status: "queued",
      });
    }

    if (nextAttachments.length > 0) {
      setAttachments((current) => [...current, ...nextAttachments]);
    }
    setLocalError(errors[0] ?? null);
  };

  const onFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    if (event.target.files) addFiles(event.target.files);
    event.target.value = "";
  };

  const onPaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = filesFromTransfer(event.clipboardData);
    if (files.length === 0 || controlsDisabled) return;
    event.preventDefault();
    addFiles(files);
  };

  const onDragEnter = (event: DragEvent<HTMLDivElement>) => {
    if (!transferHasFiles(event.dataTransfer)) return;
    event.preventDefault();
    if (controlsDisabled) return;
    dragDepthRef.current += 1;
    setIsDraggingFiles(true);
  };

  const onDragOver = (event: DragEvent<HTMLDivElement>) => {
    if (!transferHasFiles(event.dataTransfer)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = controlsDisabled ? "none" : "copy";
  };

  const onDragLeave = (event: DragEvent<HTMLDivElement>) => {
    if (!transferHasFiles(event.dataTransfer)) return;
    event.preventDefault();
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setIsDraggingFiles(false);
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    if (!transferHasFiles(event.dataTransfer)) return;
    event.preventDefault();
    dragDepthRef.current = 0;
    setIsDraggingFiles(false);
    const files = filesFromTransfer(event.dataTransfer);
    if (!controlsDisabled && files.length > 0) addFiles(files);
  };

  const removeAttachment = (id: string) => {
    setAttachments((current) => {
      const removed = current.find((attachment) => attachment.id === id);
      if (removed?.previewUrl) URL.revokeObjectURL(removed.previewUrl);
      return current.filter((attachment) => attachment.id !== id);
    });
    setLocalError(null);
  };

  return (
    <form
      className="pointer-events-none absolute inset-x-0 bottom-0 z-10 shrink-0 bg-gradient-to-t from-white via-white/95 to-transparent px-3 pb-[calc(0.75rem+env(safe-area-inset-bottom,0px))] pt-8 sm:px-6"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <input
        ref={fileInputRef}
        accept={ATTACHMENT_ACCEPT}
        className="hidden"
        multiple
        onChange={onFileChange}
        type="file"
      />
      <div
        className={[
          "pointer-events-auto mx-auto max-w-[50rem] rounded-[1.35rem] border bg-white px-3 pb-2.5 pt-3 shadow-[0_8px_28px_rgba(31,41,55,0.08)] transition focus-within:border-[#aebdd0] focus-within:ring-2 focus-within:ring-[#dce5ef]/70",
          isDraggingFiles
            ? "border-[#8ca7c5] bg-[#f8fbff] ring-2 ring-[#dce5ef]"
            : "border-[#c8d2df]",
        ].join(" ")}
        onDragEnter={onDragEnter}
        onDragLeave={onDragLeave}
        onDragOver={onDragOver}
        onDrop={onDrop}
      >
        {isDraggingFiles ? (
          <div className="mb-2 px-1 text-sm font-medium text-blue-500" role="status">
            Drop files to attach
          </div>
        ) : null}

        {attachments.length > 0 ? (
          <div className="scrollbar-none -mx-1 mb-3 flex gap-3 overflow-x-auto px-1 py-1">
            {attachments.map((attachment) => (
              <ComposerAttachmentCard
                key={attachment.id}
                attachment={attachment}
                disabled={controlsDisabled}
                onRemove={removeAttachment}
              />
            ))}
          </div>
        ) : null}

        <textarea
          aria-label="GUI chat message"
          className="min-h-11 max-h-40 w-full resize-none bg-transparent px-1 py-0.5 text-[0.9375rem] leading-6 text-[#26292e] outline-none placeholder:text-[#999da4] disabled:cursor-not-allowed disabled:opacity-70"
          disabled={disabled || isSubmitting}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          placeholder={disabled ? "Connecting to Hermes…" : "Message Hermes…"}
          rows={2}
          value={text}
        />

        {localError ? <div className="px-1 pb-2 text-xs text-destructive">{localError}</div> : null}

        <div className="flex items-center justify-between gap-2 pt-1">
          <div className="flex items-center gap-2">
            <button
              type="button"
              aria-label="Attach files"
              className="flex h-7 w-7 items-center justify-center rounded-full text-[#686d75] transition hover:bg-[#f0f1f3] disabled:cursor-not-allowed disabled:opacity-40"
              disabled={controlsDisabled}
              onClick={() => fileInputRef.current?.click()}
            >
              <Plus className="h-4 w-4" />
            </button>
            <span className="text-[0.6875rem] text-[#9a9ea5]">Enter to send · Shift+Enter for new line</span>
          </div>

          <div className="flex items-center gap-2">
            {isGenerating ? (
              <button
                type="button"
                aria-label="Stop generating"
                className="flex h-8 w-8 items-center justify-center rounded-full bg-[#2f3338] text-white transition hover:bg-[#191b1e] disabled:opacity-40"
                onClick={onStop}
                disabled={disabled}
              >
                <Square className="h-3 w-3 fill-current" />
              </button>
            ) : null}
            {!isGenerating || allowSendWhileGenerating ? (
              <button
                type="submit"
                aria-label={isSubmitting ? "Sending" : "Send message"}
                className="flex h-8 w-8 items-center justify-center rounded-full bg-[#2f3338] text-white transition hover:bg-[#191b1e] disabled:cursor-not-allowed disabled:bg-[#d7d9dc]"
                disabled={!canSend}
              >
                <ArrowUp className="h-4 w-4" />
              </button>
            ) : null}
          </div>
        </div>
      </div>
    </form>
  );
}

function filesFromTransfer(dataTransfer: DataTransfer): File[] {
  const files = Array.from(dataTransfer.files);
  if (files.length > 0) return files;
  return Array.from(dataTransfer.items)
    .filter((item) => item.kind === "file")
    .map((item) => item.getAsFile())
    .filter((file): file is File => file !== null);
}

function transferHasFiles(dataTransfer: DataTransfer): boolean {
  return (
    dataTransfer.files.length > 0 ||
    Array.from(dataTransfer.items).some((item) => item.kind === "file") ||
    Array.from(dataTransfer.types).includes("Files")
  );
}

function createClientId(prefix: string): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}
