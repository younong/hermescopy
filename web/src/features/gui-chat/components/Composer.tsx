import { useEffect, useRef, useState, type ChangeEvent, type KeyboardEvent } from "react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Paperclip, Send, Square } from "lucide-react";

import { validateComposerAttachment } from "../attachments";
import type { GuiComposerAttachment } from "../types";
import { ComposerAttachmentCard } from "./ComposerAttachmentCard";

const ATTACHMENT_ACCEPT = "*/*";

export function Composer({
  disabled,
  isGenerating,
  onSend,
  onStop,
}: {
  disabled?: boolean;
  isGenerating: boolean;
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
  const [localError, setLocalError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const attachmentsRef = useRef<GuiComposerAttachment[]>([]);

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

  const canSend =
    (text.trim().length > 0 || attachments.length > 0) && !disabled && !isGenerating && !isSubmitting;
  const controlsDisabled = disabled || isGenerating || isSubmitting;

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
    if ((!next && attachments.length === 0) || disabled || isGenerating || isSubmitting) return;

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

    for (const file of Array.from(files)) {
      const validation = validateComposerAttachment(file);
      if (!validation.ok) {
        errors.push(validation.message);
        continue;
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

  const removeAttachment = (id: string) => {
    setAttachments((current) => {
      const removed = current.find((attachment) => attachment.id === id);
      if (removed?.previewUrl) URL.revokeObjectURL(removed.previewUrl);
      return current.filter((attachment) => attachment.id !== id);
    });
  };

  return (
    <form
      className="shrink-0 border-t border-current/15 bg-background-base/95 p-3"
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
      <div className="rounded-[28px] border border-current/20 bg-background-base p-3 shadow-sm transition focus-within:border-blue-300 focus-within:ring-2 focus-within:ring-blue-200/50">
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
          className="min-h-16 max-h-40 w-full resize-none bg-transparent px-1 py-1 text-sm text-text-primary outline-none placeholder:text-text-tertiary disabled:cursor-not-allowed disabled:opacity-70"
          disabled={disabled || isSubmitting}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKeyDown}
          placeholder={disabled ? "Connecting to Hermes…" : "Message Hermes…"}
          rows={2}
          value={text}
        />

        {localError ? <div className="px-1 pb-2 text-xs text-destructive">{localError}</div> : null}

        <div className="flex items-center justify-between gap-2 pt-1">
          <Button
            type="button"
            ghost
            size="icon"
            aria-label="Attach files"
            disabled={controlsDisabled}
            onClick={() => fileInputRef.current?.click()}
          >
            <Paperclip className="h-4 w-4" />
          </Button>

          {isGenerating ? (
            <Button
              type="button"
              ghost
              className="text-destructive"
              onClick={onStop}
              disabled={disabled}
            >
              <Square className="h-4 w-4" />
              Stop
            </Button>
          ) : (
            <Button type="submit" disabled={!canSend}>
              <Send className="h-4 w-4" />
              {isSubmitting ? "Sending…" : "Send"}
            </Button>
          )}
        </div>
      </div>
    </form>
  );
}

function createClientId(prefix: string): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}
