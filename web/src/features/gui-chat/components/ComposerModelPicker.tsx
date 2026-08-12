import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { Check, ChevronDown, RefreshCw, Settings2 } from "lucide-react";

import { guiChatTranslations, useI18n } from "@/i18n";
import { api, type ModelRegistration } from "@/lib/api";

import type { GuiChatModelSwitchResponse } from "../api";
import { GuiChatWorkspaceDialog } from "./GuiChatWorkspaceDialog";

interface PendingConfirm {
  message: string;
  registration: ModelRegistration;
}

export function ComposerModelPicker({
  busy,
  canSwitch,
  currentModel,
  currentProvider,
  onManageModels,
  onSwitchChat,
}: {
  busy: boolean;
  canSwitch: boolean;
  currentModel?: string;
  currentProvider?: string;
  onManageModels(): void;
  onSwitchChat(
    registration: ModelRegistration,
    confirmExpensiveModel?: boolean,
  ): Promise<GuiChatModelSwitchResponse>;
}) {
  const { t } = useI18n();
  const chatCopy = guiChatTranslations(t);
  const copy = chatCopy.composer.modelPicker;
  const [open, setOpen] = useState(false);
  const [registrations, setRegistrations] = useState<ModelRegistration[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [switchingId, setSwitchingId] = useState<string | null>(null);
  const [pendingConfirm, setPendingConfirm] = useState<PendingConfirm | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await api.getModelRegistrations();
      setRegistrations(
        response.registrations.filter((registration) => registration.kind === "chat"),
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      setRegistrations((current) => current ?? []);
    } finally {
      setLoading(false);
    }
  }, []);

  // The gateway rejects mid-turn switches; fold the popover when switching
  // becomes unavailable so a stale selection can never be attempted.
  const switchDisabled = busy || !canSwitch;
  const [wasSwitchDisabled, setWasSwitchDisabled] = useState(switchDisabled);
  if (switchDisabled !== wasSwitchDisabled) {
    setWasSwitchDisabled(switchDisabled);
    if (switchDisabled) {
      setOpen(false);
      setPendingConfirm(null);
    }
  }

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

  const applySwitch = async (
    registration: ModelRegistration,
    confirmExpensiveModel = false,
  ) => {
    if (switchingId || (!confirmExpensiveModel && busy) || !canSwitch) return;
    setSwitchingId(registration.id);
    setError(null);
    try {
      const result = await onSwitchChat(registration, confirmExpensiveModel);
      if (result.confirm_required) {
        setPendingConfirm({
          message:
            result.confirm_message ||
            result.warning ||
            copy.highPriceWarning,
          registration,
        });
        return;
      }
      setOpen(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSwitchingId(null);
    }
  };

  const disabled = switchDisabled;
  const shortName = (currentModel ?? "").split("/").pop() || copy.selectModel;

  const toggleOpen = () => {
    const next = !open;
    setOpen(next);
    // Lazy-load the registration list the first time the popover opens.
    if (next && registrations === null && !loading) void load();
  };

  return (
    <div ref={rootRef} className="relative" data-composer-model-picker>
      <button
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label={copy.switchModel}
        className="flex h-7 max-w-36 items-center gap-1 rounded-full px-2 text-[0.6875rem] font-medium text-[#686d75] transition hover:bg-[#f0f1f3] disabled:cursor-not-allowed disabled:opacity-60 sm:max-w-48"
        disabled={disabled}
        onClick={toggleOpen}
        title={currentModel}
        type="button"
      >
        <span className="truncate">{shortName}</span>
        <ChevronDown aria-hidden className="h-3 w-3 shrink-0" />
      </button>

      {open ? (
        <div
          aria-label={copy.chatModels}
          className="absolute bottom-full right-0 z-20 mb-2 max-h-72 w-64 overflow-y-auto rounded-xl border border-[#c8d2df] bg-white p-1 shadow-[0_8px_28px_rgba(31,41,55,0.12)]"
          role="listbox"
        >
          {error ? (
            <div className="px-2 py-1.5 text-xs text-destructive" role="alert">
              <div>{error}</div>
              <button
                className="mt-1 flex items-center gap-1 font-medium text-[#686d75] hover:text-[#26292e]"
                disabled={loading}
                onClick={() => void load()}
                type="button"
              >
                <RefreshCw aria-hidden className={`h-3 w-3 ${loading ? "animate-spin" : ""}`} />
                {t.common.retry}
              </button>
            </div>
          ) : null}

          {registrations === null && loading ? (
            <div className="px-2 py-1.5 text-xs text-[#9a9ea5]" role="status">
              {t.common.loading}
            </div>
          ) : null}

          {registrations !== null && registrations.length === 0 && !error ? (
            <div className="px-2 py-1.5 text-xs text-[#9a9ea5]">
              {copy.noModels}
            </div>
          ) : null}

          {registrations?.map((registration) => {
            // The gateway reports the active provider as the raw agent
            // provider (e.g. bare "custom"), which need not equal the
            // registration's slug ("custom:kimi-code"). When the model id is
            // unique across registrations, a model match alone is enough.
            const modelIsUnique = !registrations.some(
              (other) => other !== registration && other.model === currentModel,
            );
            const isCurrent =
              registration.model === currentModel &&
              (registration.provider === currentProvider || modelIsUnique);
            const switching = switchingId === registration.id;
            return (
              <button
                aria-selected={isCurrent}
                className="flex w-full items-center justify-between gap-2 rounded-lg px-2 py-1.5 text-left transition hover:bg-[#f0f1f3] disabled:cursor-not-allowed disabled:opacity-50"
                disabled={Boolean(switchingId) || isCurrent}
                key={registration.id}
                onClick={() => void applySwitch(registration)}
                role="option"
                type="button"
              >
                <span className="min-w-0 truncate text-[13px] font-medium text-[#26292e]">
                  {registration.model.split("/").pop()}
                  {switching ? "…" : ""}
                </span>
                {isCurrent ? (
                  <Check aria-hidden className="h-3.5 w-3.5 shrink-0 text-[#26292e]" />
                ) : null}
              </button>
            );
          })}

          <div className="mt-1 border-t border-[#eef0f2] pt-1">
            <button
              className="flex w-full items-center gap-1.5 rounded-lg px-2 py-1.5 text-[13px] text-[#686d75] transition hover:bg-[#f0f1f3]"
              onClick={() => {
                setOpen(false);
                onManageModels();
              }}
              type="button"
            >
              <Settings2 aria-hidden className="h-3.5 w-3.5" />
              {chatCopy.shell.manageModels}…
            </button>
          </div>
        </div>
      ) : null}

      {pendingConfirm ? (
        <GuiChatWorkspaceDialog
          busy={switchingId === pendingConfirm.registration.id}
          description={pendingConfirm.message}
          onClose={() => setPendingConfirm(null)}
          title={copy.highPriceWarning}
        >
          <div className="gui-chat-workspace-dialog-actions">
            <button onClick={() => setPendingConfirm(null)} type="button">
              {t.common.cancel}
            </button>
            <button
              className="is-destructive"
              onClick={() => {
                const pending = pendingConfirm;
                setPendingConfirm(null);
                void applySwitch(pending.registration, true);
              }}
              type="button"
            >
              {copy.useModel}
            </button>
          </div>
        </GuiChatWorkspaceDialog>
      ) : null}
    </div>
  );
}
