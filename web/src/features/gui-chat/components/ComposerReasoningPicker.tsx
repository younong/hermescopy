import { useEffect, useRef, useState } from "react";
import { Brain, Check, ChevronDown } from "lucide-react";

import { guiChatTranslations, useI18n } from "@/i18n";
import type { ReasoningLevel } from "@/lib/api";

const LABELS: Record<ReasoningLevel, string> = {
  high: "High",
  xhigh: "XHigh",
  max: "Max",
};

export function ComposerReasoningPicker({
  busy,
  currentLevel,
  levels,
  onChange,
}: {
  busy: boolean;
  currentLevel?: string;
  levels: ReasoningLevel[];
  onChange(level: ReasoningLevel): Promise<void>;
}) {
  const { t } = useI18n();
  const copy = guiChatTranslations(t).composer.reasoningPicker;
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  if (levels.length === 0) return null;
  const selected = levels.includes(currentLevel as ReasoningLevel)
    ? currentLevel as ReasoningLevel
    : undefined;

  const apply = async (level: ReasoningLevel) => {
    if (saving || busy || level === selected) return;
    setSaving(true);
    setError(null);
    try {
      await onChange(level);
      setOpen(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div ref={rootRef} className="relative" data-composer-reasoning-picker>
      <button
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label={copy.changeLevel}
        className="flex h-7 items-center gap-1 rounded-full px-2 text-[0.6875rem] font-medium text-[#686d75] transition hover:bg-[#f0f1f3] disabled:cursor-not-allowed disabled:opacity-60"
        disabled={busy || saving}
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        <Brain aria-hidden className="h-3 w-3" />
        <span>{selected ? LABELS[selected] : copy.levels}</span>
        <ChevronDown aria-hidden className="h-3 w-3" />
      </button>

      {open ? (
        <div
          aria-label={copy.levels}
          className="absolute bottom-full right-0 z-20 mb-2 w-32 rounded-xl border border-[#c8d2df] bg-white p-1 shadow-[0_8px_28px_rgba(31,41,55,0.12)]"
          role="listbox"
        >
          {levels.map((level) => (
            <button
              aria-selected={level === selected}
              className="flex w-full items-center justify-between rounded-lg px-2 py-1.5 text-left text-[13px] font-medium text-[#26292e] transition hover:bg-[#f0f1f3] disabled:opacity-50"
              disabled={saving || level === selected}
              key={level}
              onClick={() => void apply(level)}
              role="option"
              type="button"
            >
              {LABELS[level]}
              {level === selected ? <Check aria-hidden className="h-3.5 w-3.5" /> : null}
            </button>
          ))}
          {error ? <div className="px-2 py-1 text-xs text-destructive">{error}</div> : null}
        </div>
      ) : null}
    </div>
  );
}
