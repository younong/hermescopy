import { Button } from "@nous-research/ui/ui/components/button";
import { Input } from "@nous-research/ui/ui/components/input";
import { ListItem } from "@nous-research/ui/ui/components/list-item";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Check, Search, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";

import { ConfirmDialog } from "@/components/ConfirmDialog";
import {
  api,
  type ModelRegistration,
  type ModelRegistrationKind,
  type ModelRegistrationsResponse,
} from "@/lib/api";
import { cn, themedBody } from "@/lib/utils";

import type { GuiChatModelSwitchResponse } from "../api";

interface RegisteredModelPickerDialogProps {
  busy: boolean;
  currentModel?: string;
  currentProvider?: string;
  onClose(): void;
  onSwitchChat(
    registration: ModelRegistration,
    confirmExpensiveModel?: boolean,
    persistGlobally?: boolean,
  ): Promise<GuiChatModelSwitchResponse>;
  profile?: string;
}

interface PendingChatSwitch {
  message: string;
  persistGlobally: boolean;
  registration: ModelRegistration;
}

const KIND_LABELS: Record<ModelRegistrationKind, string> = {
  chat: "Chat",
  image: "Image",
  video: "Video",
};

export function RegisteredModelPickerDialog({
  busy,
  currentModel,
  currentProvider,
  onClose,
  onSwitchChat,
  profile,
}: RegisteredModelPickerDialogProps) {
  const [payload, setPayload] = useState<ModelRegistrationsResponse | null>(null);
  const [selectedKind, setSelectedKind] = useState<ModelRegistrationKind>("chat");
  const [selectedId, setSelectedId] = useState("");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [applying, setApplying] = useState(false);
  const [persistGlobally, setPersistGlobally] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingChatSwitch, setPendingChatSwitch] = useState<PendingChatSwitch | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void api.getModelRegistrations(profile).then((response) => {
      if (cancelled) return;
      setPayload(response);
      setSelectedId(defaultSelectionId(response, "chat", currentProvider, currentModel));
      setLoading(false);
    }).catch((cause: unknown) => {
      if (cancelled) return;
      setError(cause instanceof Error ? cause.message : String(cause));
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [currentModel, currentProvider, profile]);

  useEffect(() => {
    if (busy) {
      setPendingChatSwitch(null);
      setPersistGlobally(false);
    }
  }, [busy]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || pendingChatSwitch) return;
      event.preventDefault();
      onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, pendingChatSwitch]);

  const registrations = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return (payload?.registrations ?? []).filter((item) => {
      if (item.kind !== selectedKind) return false;
      if (!normalized) return true;
      return `${item.name} ${item.provider} ${item.model}`.toLocaleLowerCase().includes(normalized);
    });
  }, [payload, query, selectedKind]);
  const selected = payload?.registrations.find(
    (item) => item.kind === selectedKind && item.id === selectedId,
  ) ?? null;
  const selectedIsActive = selected ? isActiveRegistration(
    selected,
    payload,
    currentProvider,
    currentModel,
  ) : false;
  const apply = async (
    registration: ModelRegistration,
    confirmExpensiveModel = false,
    persist = persistGlobally,
  ) => {
    if (applying || isActiveRegistration(registration, payload, currentProvider, currentModel)) return;
    if (busy) {
      setError("Stop the current response before switching models.");
      return;
    }
    setApplying(true);
    setError(null);
    try {
      if (registration.kind === "chat") {
        const result = await onSwitchChat(
          registration,
          confirmExpensiveModel,
          persist,
        );
        if (result.confirm_required) {
          setPendingChatSwitch({
            message:
              result.confirm_message ||
              result.warning ||
              "This model has unusually high known pricing.",
            persistGlobally: persist,
            registration,
          });
          return;
        }
      } else {
        await api.activateModelRegistration(registration.id, profile);
      }
      onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setApplying(false);
    }
  };

  return createPortal(
    <div
      aria-labelledby="registered-model-picker-title"
      aria-modal="true"
      className="fixed inset-0 z-[100] flex items-center justify-center bg-background/85 p-4"
      onClick={(event) => event.target === event.currentTarget && !applying && onClose()}
      role="dialog"
    >
      <div className={cn(themedBody, "relative flex max-h-[80vh] w-full max-w-2xl flex-col border border-border bg-card shadow-2xl")}>
        <Button
          aria-label="Close"
          className="absolute right-2 top-2 text-muted-foreground hover:text-foreground"
          disabled={applying}
          ghost
          onClick={onClose}
          size="icon"
        >
          <X />
        </Button>
        <header className="border-b border-border p-5 pb-3">
          <h2 className="font-mondwest text-base text-display tracking-wider" id="registered-model-picker-title">
            Switch registered model
          </h2>
          <p className="mt-1 text-xs text-muted-foreground">
            Chat changes this conversation. Image and video update the active generation model.
          </p>
        </header>
        <div className="flex gap-1 border-b border-border px-5 py-3" role="tablist">
          {(Object.keys(KIND_LABELS) as ModelRegistrationKind[]).map((kind) => (
            <Button
              key={kind}
              aria-selected={selectedKind === kind}
              outlined={selectedKind !== kind}
              onClick={() => {
                setSelectedKind(kind);
                setSelectedId(payload ? defaultSelectionId(
                  payload,
                  kind,
                  currentProvider,
                  currentModel,
                ) : "");
                setPersistGlobally(false);
                setQuery("");
                setError(null);
              }}
              role="tab"
              size="sm"
            >
              {KIND_LABELS[kind]}
            </Button>
          ))}
        </div>
        <div className="border-b border-border px-5 py-3">
          <div className="relative">
            <Search className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              autoFocus
              className="h-8 pl-7 text-sm"
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Filter registered models…"
              value={query}
            />
          </div>
        </div>
        <div className="min-h-48 flex-1 overflow-y-auto p-2" role="listbox">
          {loading ? (
            <div className="flex items-center gap-2 p-3 text-xs text-muted-foreground"><Spinner /> loading…</div>
          ) : registrations.length === 0 ? (
            <div className="p-3 text-xs italic text-muted-foreground">
              {query ? "No registered models match your filter." : `No ${selectedKind} models registered.`}
            </div>
          ) : registrations.map((registration) => (
            <ListItem
              active={registration.id === selectedId}
              aria-selected={registration.id === selectedId}
              className="items-start text-xs"
              key={registration.id}
              onClick={() => setSelectedId(registration.id)}
              onDoubleClick={() => void apply(registration)}
              role="option"
            >
              <Check className={cn("mt-0.5 h-3.5 w-3.5 shrink-0", registration.id === selectedId ? "text-primary" : "text-transparent")} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate font-medium">{registration.name}</span>
                  {isActiveRegistration(registration, payload, currentProvider, currentModel) ? <span className="text-display text-xs text-primary">active</span> : null}
                </div>
                <div className="truncate font-mono text-muted-foreground">{registration.provider} · {registration.model}</div>
              </div>
            </ListItem>
          ))}
        </div>
        {selected?.kind === "chat" ? (
          <label className="flex items-center gap-2 border-t border-border px-5 py-3 text-xs">
            <input
              checked={persistGlobally}
              disabled={applying || busy}
              onChange={(event) => setPersistGlobally(event.target.checked)}
              type="checkbox"
            />
            Also use this model for new conversations
          </label>
        ) : null}
        {error || busy ? (
          <div className="border-t border-border px-5 py-2 text-xs text-destructive" role="alert">
            {error ?? "Stop the current response before switching models."}
          </div>
        ) : null}
        <footer className="flex items-center justify-end gap-2 border-t border-border p-3">
          <Button disabled={applying} onClick={onClose} outlined>Cancel</Button>
          <Button
            disabled={!selected || applying || busy || selectedIsActive}
            onClick={() => selected && void apply(selected)}
          >
            {applying ? <Spinner /> : selectedIsActive ? "Active" : selected?.kind === "chat" ? "Switch" : "Activate"}
          </Button>
        </footer>
      </div>
      <ConfirmDialog
        cancelLabel="Cancel"
        confirmLabel="Switch anyway"
        description={pendingChatSwitch?.message}
        destructive
        loading={applying}
        onCancel={() => setPendingChatSwitch(null)}
        onConfirm={() => {
          const pending = pendingChatSwitch;
          if (!pending) return;
          setPendingChatSwitch(null);
          void apply(
            pending.registration,
            true,
            pending.persistGlobally,
          );
        }}
        open={!!pendingChatSwitch}
        title="Expensive Model Warning"
      />
    </div>,
    document.body,
  );
}

function defaultSelectionId(
  payload: ModelRegistrationsResponse,
  kind: ModelRegistrationKind,
  currentProvider?: string,
  currentModel?: string,
): string {
  const activeId = kind === "chat"
    ? payload.registrations.find(
      (item) => item.kind === "chat" && item.provider === currentProvider && item.model === currentModel,
    )?.id
    : payload.active[kind]?.registration_id;
  return activeId ?? payload.registrations.find((item) => item.kind === kind)?.id ?? "";
}

function isActiveRegistration(
  registration: ModelRegistration,
  payload: ModelRegistrationsResponse | null,
  currentProvider?: string,
  currentModel?: string,
): boolean {
  if (registration.kind === "chat") {
    return registration.provider === currentProvider && registration.model === currentModel;
  }
  return payload?.active[registration.kind]?.registration_id === registration.id;
}
