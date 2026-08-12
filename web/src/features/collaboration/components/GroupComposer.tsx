import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { AtSign, Paperclip, Send, UsersRound, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { CollaborationAttachment, CollaborationMembership } from "../types";
import { normalizeMentionSelection, recipientLabel, type MentionSelection } from "../mentions";

export interface GroupComposerSubmit {
  text: string;
  selection: MentionSelection;
  attachments: CollaborationAttachment[];
}

interface GroupComposerProps {
  employeeName(employeeId: string): string;
  archived: boolean;
  defaultSelection: MentionSelection;
  disabled: boolean;
  memberships: CollaborationMembership[];
  onSubmit(value: GroupComposerSubmit): Promise<void>;
  onUpload(file: File): Promise<CollaborationAttachment>;
}

export function GroupComposer({
  employeeName,
  archived,
  defaultSelection,
  disabled,
  memberships,
  onSubmit,
  onUpload,
}: GroupComposerProps) {
  const activeMembers = useMemo(
    () => memberships.filter((member) => member.leave_sequence === null),
    [memberships],
  );
  const membershipsById = useMemo(
    () => Object.fromEntries(activeMembers.map((member) => [member.membership_id, member])),
    [activeMembers],
  );
  const [text, setText] = useState("");
  const [selection, setSelection] = useState<MentionSelection>({ mentionAll: false, membershipIds: [] });
  const [pickerOpen, setPickerOpen] = useState(false);
  const [attachments, setAttachments] = useState<CollaborationAttachment[]>([]);
  const [uploading, setUploading] = useState<string[]>([]);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const pickerRootRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const normalized = normalizeMentionSelection(selection, activeMembers);
  const effectiveSelection = normalized.mentionAll || normalized.membershipIds.length > 0
    ? normalized
    : defaultSelection;

  useEffect(() => {
    if (!pickerOpen) return;

    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!pickerRootRef.current?.contains(event.target as Node)) setPickerOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [pickerOpen]);

  const submit = async () => {
    if (!text.trim() || sending || uploading.length > 0) return;
    setSending(true);
    setError(null);
    try {
      await onSubmit({ attachments, selection: effectiveSelection, text: text.trim() });
      setText("");
      setSelection({ mentionAll: false, membershipIds: [] });
      setAttachments([]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSending(false);
    }
  };

  const uploadFiles = async (files: FileList | null) => {
    if (!files) return;
    for (const file of Array.from(files)) {
      setUploading((current) => [...current, file.name]);
      setError(null);
      try {
        const attachment = await onUpload(file);
        setAttachments((current) => [...current, attachment]);
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause));
      } finally {
        setUploading((current) => current.filter((name) => name !== file.name));
      }
    }
    if (fileRef.current) fileRef.current.value = "";
  };

  if (archived) {
    return <div className="border-t border-[#ebecef] px-4 py-4 text-center text-xs text-[#969aa1]">This group is archived and read-only.</div>;
  }

  return (
    <div className="border-t border-[#ebecef] bg-white px-4 pb-4 pt-3">
      <div className="mx-auto max-w-3xl">
        {(attachments.length > 0 || uploading.length > 0) ? (
          <div className="mb-2 flex flex-wrap gap-1.5">
            {attachments.map((attachment) => (
              <span className="inline-flex items-center gap-1 rounded-full bg-[#eef3ff] px-2 py-1 text-[10px] text-[#4d73e6]" key={attachment.attachment_id}>
                {attachment.filename}
                <button aria-label={`Remove ${attachment.filename}`} onClick={() => setAttachments((current) => current.filter((item) => item.attachment_id !== attachment.attachment_id))} type="button"><X className="h-3 w-3" /></button>
              </span>
            ))}
            {uploading.map((name) => <span className="inline-flex items-center gap-1 rounded-full bg-[#f1f3f5] px-2 py-1 text-[10px]" key={name}><Spinner /> {name}</span>)}
          </div>
        ) : null}
        <div className="relative rounded-xl border border-[#dfe2e7] bg-white shadow-[0_3px_14px_rgba(26,31,44,0.06)] focus-within:border-[#94aaf0]">
          <textarea
            aria-label="Group message"
            className="min-h-20 w-full resize-none bg-transparent px-3 pb-2 pt-3 text-sm outline-none placeholder:text-[#a1a5ac]"
            disabled={disabled || sending}
            onChange={(event) => {
              const nextText = event.target.value;
              setText(nextText);
              if (nextText.endsWith("@")) setPickerOpen(true);
            }}
            onKeyDown={(event) => {
              if (event.key === "Escape" && pickerOpen) {
                event.preventDefault();
                setPickerOpen(false);
                return;
              }
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                if (pickerOpen) setPickerOpen(false);
                else void submit();
              }
            }}
            placeholder="Message the group…"
            ref={textareaRef}
            value={text}
          />
          <div className="flex items-center justify-between gap-2 px-2 pb-2">
            <div className="relative flex items-center gap-1" ref={pickerRootRef}>
              <button aria-expanded={pickerOpen} aria-haspopup="dialog" aria-label="Choose employee mentions" className="gui-chat-icon-button" onClick={() => setPickerOpen((value) => !value)} type="button"><AtSign /></button>
              <button aria-label="Attach files" className="gui-chat-icon-button" onClick={() => fileRef.current?.click()} type="button"><Paperclip /></button>
              <input className="hidden" multiple onChange={(event) => void uploadFiles(event.target.files)} ref={fileRef} type="file" />
              <span className="max-w-64 truncate text-[10px] text-[#777c84]">Replies from {recipientLabel(effectiveSelection, membershipsById, employeeName)}</span>
              {pickerOpen ? (
                <div aria-label="Employee mentions" className="absolute bottom-9 left-0 z-20 w-64 rounded-lg border border-[#dfe2e7] bg-white p-2 shadow-xl" role="dialog">
                  <div className="mb-1 flex items-center justify-between px-2 py-1">
                    <span className="text-[10px] font-medium text-[#777c84]">Choose recipients</span>
                    <button aria-label="Close employee mentions" className="rounded p-0.5 text-[#777c84] hover:bg-[#f1f3f5] hover:text-[#282b30]" onClick={() => { setPickerOpen(false); textareaRef.current?.focus(); }} type="button"><X className="h-3.5 w-3.5" /></button>
                  </div>
                  <button
                    aria-pressed={normalized.mentionAll}
                    className={`flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-xs ${normalized.mentionAll ? "bg-[#eef3ff] text-[#3867ed]" : "hover:bg-[#f5f6f8]"}`}
                    onClick={() => setSelection({ mentionAll: !normalized.mentionAll, membershipIds: [] })}
                    type="button"
                  ><UsersRound className="h-3.5 w-3.5" /> @all</button>
                  <div className="my-1 border-t border-[#eceef1]" />
                  {activeMembers.map((member) => {
                    const checked = normalized.membershipIds.includes(member.membership_id);
                    return (
                      <label className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-2 text-xs hover:bg-[#f5f6f8]" key={member.membership_id}>
                        <input checked={checked} disabled={normalized.mentionAll} onChange={() => setSelection({ mentionAll: false, membershipIds: checked ? normalized.membershipIds.filter((id) => id !== member.membership_id) : [...normalized.membershipIds, member.membership_id] })} type="checkbox" />
                        <span className="truncate">@{employeeName(member.employee_id)}</span>
                      </label>
                    );
                  })}
                  <p className="px-2 pt-2 text-[10px] text-[#969aa1]">Press Enter to finish selecting</p>
                </div>
              ) : null}
            </div>
            <button aria-label="Send group message" className="flex h-8 w-8 items-center justify-center rounded-lg bg-[#3867ed] text-white disabled:opacity-40" disabled={disabled || sending || uploading.length > 0 || !text.trim()} onClick={() => void submit()} type="button">
              {sending ? <Spinner /> : <Send className="h-3.5 w-3.5" />}
            </button>
          </div>
        </div>
        {error ? <p className="mt-2 text-xs text-[#b42318]" role="alert">{error}</p> : null}
        <p className="mt-2 text-center text-[10px] text-[#a1a5ac]">Without an @ mention, the current employee replies. Use @ to choose someone else.</p>
      </div>
    </div>
  );
}
