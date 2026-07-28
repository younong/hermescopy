import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type FormEvent,
} from "react";
import { Check, Pencil, Plus, Trash2, X } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Select, SelectOption } from "@nous-research/ui/ui/components/select";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { DeleteConfirmDialog } from "@/components/DeleteConfirmDialog";
import { usePageHeader } from "@/contexts/usePageHeader";
import { useModalBehavior } from "@/hooks/useModalBehavior";
import { useI18n } from "@/i18n";
import { en } from "@/i18n/en";
import { api } from "@/lib/api";
import type {
  ModelRegistration,
  ModelRegistrationChatCatalogProvider,
  ModelRegistrationKind,
  ModelRegistrationMediaCatalogProvider,
  ModelRegistrationRequest,
  ModelRegistrationSource,
  ModelRegistrationsResponse,
} from "@/lib/api";
import { cn, themedBody } from "@/lib/utils";

interface RegistrationFormState {
  name: string;
  kind: ModelRegistrationKind;
  source: ModelRegistrationSource;
  provider: string;
  model: string;
  baseUrl: string;
  apiMode: string;
  apiKey: string;
  contextLength: string;
  useGateway: boolean;
}

const EMPTY_FORM: RegistrationFormState = {
  name: "",
  kind: "chat",
  source: "catalog",
  provider: "",
  model: "",
  baseUrl: "",
  apiMode: "openai",
  apiKey: "",
  contextLength: "",
  useGateway: false,
};

export function registrationRequestFromForm(
  form: RegistrationFormState,
): ModelRegistrationRequest {
  const request: ModelRegistrationRequest = {
    name: form.name.trim(),
    kind: form.kind,
    source: form.source,
    model: form.model.trim(),
  };
  if (form.kind === "chat" && form.source === "custom") {
    request.base_url = form.baseUrl.trim();
    request.api_mode = form.apiMode.trim() || "openai";
    request.api_key = form.apiKey.trim();
    const contextLength = Number.parseInt(form.contextLength, 10);
    if (Number.isInteger(contextLength) && contextLength > 0) {
      request.context_length = contextLength;
    }
  } else {
    request.provider = form.provider;
    if (form.kind !== "chat") request.use_gateway = form.useGateway;
  }
  return request;
}

function errorDetail(error: unknown): string {
  if (!(error instanceof Error)) return String(error);
  const jsonStart = error.message.indexOf("{");
  if (jsonStart >= 0) {
    try {
      const payload = JSON.parse(error.message.slice(jsonStart)) as {
        detail?: unknown;
      };
      if (typeof payload.detail === "string") return payload.detail;
    } catch {
      // Fall through to the original message for non-JSON responses.
    }
  }
  return error.message;
}

export default function ModelRegistrationsPage() {
  const { t } = useI18n();
  const L: NonNullable<typeof en.modelRegistrations> =
    t.modelRegistrations ?? en.modelRegistrations!;
  const common = { ...en.common, ...t.common };
  const { setTitle } = usePageHeader();
  const { toast, showToast } = useToast();
  const [data, setData] = useState<ModelRegistrationsResponse | null>(null);
  const [catalogs, setCatalogs] = useState<
    Partial<
      Record<
        ModelRegistrationKind,
        Array<
          ModelRegistrationChatCatalogProvider | ModelRegistrationMediaCatalogProvider
        >
      >
    >
  >({});
  const [catalogsLoading, setCatalogsLoading] = useState<
    Partial<Record<ModelRegistrationKind, boolean>>
  >({});
  const [loading, setLoading] = useState(true);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<ModelRegistration | null>(null);
  const [form, setForm] = useState<RegistrationFormState>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [activatingId, setActivatingId] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<ModelRegistration | null>(null);
  const [deleting, setDeleting] = useState(false);

  const closeForm = useCallback(() => {
    if (saving) return;
    setFormOpen(false);
    setEditing(null);
    setForm(EMPTY_FORM);
  }, [saving]);
  const modalRef = useModalBehavior({ open: formOpen, onClose: closeForm });

  const load = useCallback(async () => {
    try {
      setData(await api.getModelRegistrations());
    } catch (error) {
      showToast(`${L.loadFailed}: ${errorDetail(error)}`, "error");
    } finally {
      setLoading(false);
    }
  }, [L.loadFailed, showToast]);

  useEffect(() => {
    void load();
  }, [load]);

  const loadCatalog = useCallback(async (kind: ModelRegistrationKind) => {
    if (catalogs[kind] || catalogsLoading[kind]) return;
    setCatalogsLoading((current) => ({ ...current, [kind]: true }));
    try {
      const response = await api.getModelRegistrationCatalog(kind);
      setCatalogs((current) => ({ ...current, [kind]: response.providers }));
    } catch (error) {
      showToast(`${L.loadFailed}: ${errorDetail(error)}`, "error");
    } finally {
      setCatalogsLoading((current) => ({ ...current, [kind]: false }));
    }
  }, [L.loadFailed, catalogs, catalogsLoading, showToast]);

  const openCreate = useCallback(() => {
    setEditing(null);
    setForm(EMPTY_FORM);
    setFormOpen(true);
    void loadCatalog("chat");
  }, [loadCatalog]);

  const openEdit = useCallback((registration: ModelRegistration) => {
    setEditing(registration);
    setForm({
      ...EMPTY_FORM,
      name: registration.name,
      kind: registration.kind,
      source: registration.source,
      provider: registration.source === "catalog" ? registration.provider : "",
      model: registration.model,
      useGateway: registration.use_gateway,
    });
    setFormOpen(true);
    if (registration.source === "catalog") {
      void loadCatalog(registration.kind);
    }
  }, [loadCatalog]);

  useEffect(() => {
    setTitle(L.title);
    return () => setTitle(null);
  }, [L.title, setTitle]);

  const providers = useMemo(
    () => catalogs[form.kind] ?? [],
    [catalogs, form.kind],
  );

  const models = useMemo(() => {
    if (form.source === "custom") return [];
    if (form.kind === "chat") {
      const provider = (providers as ModelRegistrationChatCatalogProvider[]).find(
        (item) => item.slug === form.provider,
      );
      return provider?.models.map((id) => ({ id, label: id })) ?? [];
    }
    const provider = (providers as ModelRegistrationMediaCatalogProvider[]).find(
      (item) => item.provider === form.provider,
    );
    return provider?.models.map((item) => ({
      id: item.id,
      label: item.display || item.id,
    })) ?? [];
  }, [form.kind, form.provider, form.source, providers]);

  const updateKind = (kind: ModelRegistrationKind) => {
    void loadCatalog(kind);
    setForm((current) => ({
      ...current,
      kind,
      source: kind === "chat" ? current.source : "catalog",
      provider: "",
      model: "",
      useGateway: false,
    }));
  };

  const updateSource = (source: ModelRegistrationSource) => {
    if (source === "catalog") void loadCatalog(form.kind);
    setForm((current) => ({
      ...current,
      source,
      provider: "",
      model: "",
    }));
  };

  const updateProvider = (provider: string) => {
    let model = "";
    if (form.kind !== "chat") {
      const selected = (providers as ModelRegistrationMediaCatalogProvider[]).find(
        (item) => item.provider === provider,
      );
      model = selected?.default_model || selected?.models[0]?.id || "";
    }
    setForm((current) => ({ ...current, provider, model }));
  };

  const validate = (): string | null => {
    if (!form.name.trim()) return L.nameRequired;
    if (form.kind === "chat" && form.source === "custom") {
      if (!form.model.trim()) return L.modelRequired;
      if (!form.baseUrl.trim()) return L.baseUrlRequired;
      return null;
    }
    if (!form.provider) return L.providerRequired;
    if (!form.model) return L.modelRequired;
    return null;
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    const validationError = validate();
    if (validationError) {
      showToast(validationError, "error");
      return;
    }
    setSaving(true);
    try {
      const request = registrationRequestFromForm(form);
      if (editing) {
        await api.updateModelRegistration(editing.id, request);
      } else {
        await api.createModelRegistration(request);
      }
      showToast(L.saved, "success");
      setFormOpen(false);
      setEditing(null);
      setForm(EMPTY_FORM);
      await load();
    } catch (error) {
      showToast(`${L.saveFailed}: ${errorDetail(error)}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const activate = async (registration: ModelRegistration) => {
    setActivatingId(registration.id);
    try {
      await api.activateModelRegistration(registration.id);
      showToast(L.activated, "success");
      await load();
    } catch (error) {
      showToast(`${L.activateFailed}: ${errorDetail(error)}`, "error");
    } finally {
      setActivatingId(null);
    }
  };

  const remove = async () => {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      await api.deleteModelRegistration(pendingDelete.id);
      showToast(L.deleted, "success");
      setPendingDelete(null);
      await load();
    } catch (error) {
      showToast(`${L.deleteFailed}: ${errorDetail(error)}`, "error");
    } finally {
      setDeleting(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  const registrations = data?.registrations ?? [];

  return (
    <div className="flex flex-col gap-6">
      <Toast toast={toast} />
      <div className="flex justify-end">
        <Button
          className="uppercase"
          onClick={openCreate}
          prefix={<Plus />}
          size="sm"
        >
          {L.add}
        </Button>
      </div>
      <DeleteConfirmDialog
        description={
          pendingDelete
            ? L.deleteMessage.replace("{name}", pendingDelete.name)
            : undefined
        }
        loading={deleting}
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => void remove()}
        open={pendingDelete !== null}
        title={L.deleteTitle}
      />

      {formOpen && (
        <RegistrationModal
          catalogLoading={Boolean(catalogsLoading[form.kind])}
          editing={editing}
          form={form}
          modalRef={modalRef}
          models={models}
          onClose={closeForm}
          onKindChange={updateKind}
          onProviderChange={updateProvider}
          onSourceChange={updateSource}
          onSubmit={save}
          providers={providers}
          saving={saving}
          setForm={setForm}
          strings={L}
          commonStrings={common}
        />
      )}

      <p className="max-w-3xl text-sm text-muted-foreground">{L.description}</p>

      {registrations.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center gap-2 py-12 text-center">
            <span className="text-sm font-medium">{L.noRegistrations}</span>
            <span className="text-xs text-muted-foreground">
              {L.noRegistrationsHint}
            </span>
          </CardContent>
        </Card>
      ) : (
        (["chat", "image", "video"] as const).map((kind) => {
          const rows = registrations.filter((item) => item.kind === kind);
          if (rows.length === 0) return null;
          return (
            <section className="flex flex-col gap-3" key={kind}>
              <H2 variant="sm" className="text-muted-foreground">
                {L.kinds[kind]} ({rows.length})
              </H2>
              {rows.map((registration) => {
                const active =
                  data?.active[kind]?.registration_id === registration.id;
                return (
                  <Card key={registration.id}>
                    <CardContent className="flex flex-col gap-4 py-4 sm:flex-row sm:items-center">
                      <div className="min-w-0 flex-1">
                        <div className="mb-1 flex flex-wrap items-center gap-2">
                          <span className="truncate text-sm font-medium">
                            {registration.name}
                          </span>
                          <Badge tone="secondary">{L.kinds[kind]}</Badge>
                          <Badge tone="outline">
                            {L.sources[registration.source]}
                          </Badge>
                          {active && (
                            <Badge tone="success">
                              <Check className="h-3 w-3" /> {L.active}
                            </Badge>
                          )}
                          {registration.source === "custom" && (
                            <Badge
                              tone={
                                registration.credential_configured
                                  ? "success"
                                  : "warning"
                              }
                            >
                              {registration.credential_configured
                                ? L.credentialReady
                                : L.credentialMissing}
                            </Badge>
                          )}
                        </div>
                        <p className="break-all font-mono text-xs text-muted-foreground">
                          {registration.provider} / {registration.model}
                        </p>
                        {registration.use_gateway && (
                          <p className="mt-1 text-xs text-muted-foreground">
                            {L.useGateway}
                          </p>
                        )}
                      </div>
                      <div className="flex shrink-0 items-center gap-1">
                        {kind !== "chat" && !active && (
                          <Button
                            ghost
                            size="sm"
                            className="uppercase"
                            disabled={activatingId === registration.id}
                            onClick={() => void activate(registration)}
                            prefix={
                              activatingId === registration.id ? (
                                <Spinner />
                              ) : undefined
                            }
                          >
                            {activatingId === registration.id
                              ? L.activating
                              : L.activate}
                          </Button>
                        )}
                        <Button
                          ghost
                          size="icon"
                          aria-label={`${L.edit}: ${registration.name}`}
                          title={L.edit}
                          onClick={() => openEdit(registration)}
                        >
                          <Pencil />
                        </Button>
                        <Button
                          ghost
                          destructive
                          size="icon"
                          aria-label={`${common.delete}: ${registration.name}`}
                          disabled={active}
                          title={active ? L.deleteActiveHint : common.delete}
                          onClick={() => setPendingDelete(registration)}
                        >
                          <Trash2 />
                        </Button>
                      </div>
                    </CardContent>
                  </Card>
                );
              })}
            </section>
          );
        })
      )}
    </div>
  );
}

type Strings = NonNullable<typeof en.modelRegistrations>;

function RegistrationModal({
  catalogLoading,
  editing,
  form,
  modalRef,
  models,
  onClose,
  onKindChange,
  onProviderChange,
  onSourceChange,
  onSubmit,
  providers,
  saving,
  setForm,
  strings: L,
  commonStrings,
}: {
  catalogLoading: boolean;
  editing: ModelRegistration | null;
  form: RegistrationFormState;
  modalRef: React.RefObject<HTMLDivElement | null>;
  models: Array<{ id: string; label: string }>;
  onClose: () => void;
  onKindChange: (kind: ModelRegistrationKind) => void;
  onProviderChange: (provider: string) => void;
  onSourceChange: (source: ModelRegistrationSource) => void;
  onSubmit: (event: FormEvent) => void;
  providers: Array<
    ModelRegistrationChatCatalogProvider | ModelRegistrationMediaCatalogProvider
  >;
  saving: boolean;
  setForm: React.Dispatch<React.SetStateAction<RegistrationFormState>>;
  strings: Strings;
  commonStrings: typeof en.common;
}) {
  const selectedMediaProvider =
    form.kind === "chat"
      ? undefined
      : (providers as ModelRegistrationMediaCatalogProvider[]).find(
          (item) => item.provider === form.provider,
        );

  return (
    <div
      ref={modalRef}
      className="fixed inset-0 z-[100] flex items-center justify-center bg-background/85 p-4"
      onClick={(event) => event.target === event.currentTarget && onClose()}
      role="dialog"
      aria-modal="true"
      aria-labelledby="model-registration-form-title"
    >
      <div
        className={cn(
          themedBody,
          "relative flex max-h-[90vh] w-full max-w-xl flex-col overflow-y-auto border border-border bg-card shadow-2xl",
        )}
      >
        <Button
          ghost
          size="icon"
          aria-label={commonStrings.close}
          className="absolute right-2 top-2 text-muted-foreground hover:text-foreground"
          disabled={saving}
          onClick={onClose}
        >
          <X />
        </Button>
        <header className="border-b border-border p-5 pb-3">
          <h2
            id="model-registration-form-title"
            className="font-mondwest text-display text-base tracking-wider"
          >
            {editing ? L.editTitle : L.createTitle}
          </h2>
        </header>
        <form className="grid gap-4 p-5" onSubmit={onSubmit}>
          <div className="grid gap-2">
            <Label htmlFor="registration-name">{L.name}</Label>
            <Input
              autoFocus
              id="registration-name"
              placeholder={L.namePlaceholder}
              value={form.name}
              onChange={(event) =>
                setForm((current) => ({
                  ...current,
                  name: event.target.value,
                }))
              }
            />
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="grid gap-2">
              <Label htmlFor="registration-kind">{L.kind}</Label>
              <Select
                id="registration-kind"
                disabled={editing !== null}
                value={form.kind}
                onValueChange={(value) =>
                  onKindChange(value as ModelRegistrationKind)
                }
              >
                {(["chat", "image", "video"] as const).map((kind) => (
                  <SelectOption key={kind} value={kind}>
                    {L.kinds[kind]}
                  </SelectOption>
                ))}
              </Select>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="registration-source">{L.source}</Label>
              <Select
                id="registration-source"
                disabled={form.kind !== "chat" || editing !== null}
                value={form.source}
                onValueChange={(value) =>
                  onSourceChange(value as ModelRegistrationSource)
                }
              >
                <SelectOption value="catalog">{L.sources.catalog}</SelectOption>
                {form.kind === "chat" && (
                  <SelectOption value="custom">{L.sources.custom}</SelectOption>
                )}
              </Select>
            </div>
          </div>

          {form.source === "catalog" ? (
            <>
              {catalogLoading ? (
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Spinner /> {commonStrings.loading}
                </div>
              ) : null}
              <div className="grid gap-2">
                <Label htmlFor="registration-provider">{L.provider}</Label>
                <Select
                  disabled={catalogLoading}
                  id="registration-provider"
                  value={form.provider}
                  onValueChange={onProviderChange}
                >
                  {providers.map((provider) => {
                    const id =
                      "slug" in provider ? provider.slug : provider.provider;
                    return (
                      <SelectOption key={id} value={id}>
                        {provider.name}
                      </SelectOption>
                    );
                  })}
                </Select>
              </div>
              <div className="grid gap-2">
                <Label htmlFor="registration-model">{L.model}</Label>
                <Select
                  id="registration-model"
                  value={form.model}
                  onValueChange={(model) =>
                    setForm((current) => ({ ...current, model }))
                  }
                >
                  {models.map((model) => (
                    <SelectOption key={model.id} value={model.id}>
                      {model.label}
                    </SelectOption>
                  ))}
                </Select>
              </div>
              {form.kind !== "chat" && (
                <label className="flex items-start gap-3 text-sm">
                  <input
                    checked={form.useGateway}
                    className="mt-1"
                    type="checkbox"
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        useGateway: event.target.checked,
                      }))
                    }
                  />
                  <span>
                    <span className="block font-medium">{L.useGateway}</span>
                    <span className="block text-xs text-muted-foreground">
                      {L.useGatewayHint}
                    </span>
                  </span>
                </label>
              )}
              {selectedMediaProvider && !selectedMediaProvider.available && (
                <p className="text-xs text-warning">{L.credentialMissing}</p>
              )}
            </>
          ) : (
            <>
              {editing && (
                <p className="text-xs text-warning">{L.editCustomHint}</p>
              )}
              <div className="grid gap-2">
                <Label htmlFor="registration-model">{L.model}</Label>
                <Input
                  id="registration-model"
                  placeholder="model-id"
                  value={form.model}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      model: event.target.value,
                    }))
                  }
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="registration-base-url">{L.baseUrl}</Label>
                <Input
                  id="registration-base-url"
                  placeholder="https://api.example.com/v1"
                  value={form.baseUrl}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      baseUrl: event.target.value,
                    }))
                  }
                />
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="grid gap-2">
                  <Label htmlFor="registration-api-mode">{L.apiMode}</Label>
                  <Input
                    id="registration-api-mode"
                    value={form.apiMode}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        apiMode: event.target.value,
                      }))
                    }
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="registration-context-length">
                    {L.contextLength}
                  </Label>
                  <Input
                    id="registration-context-length"
                    inputMode="numeric"
                    min="1"
                    type="number"
                    value={form.contextLength}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        contextLength: event.target.value,
                      }))
                    }
                  />
                </div>
              </div>
              <div className="grid gap-2">
                <Label htmlFor="registration-api-key">{L.apiKey}</Label>
                <Input
                  id="registration-api-key"
                  autoComplete="new-password"
                  type="password"
                  value={form.apiKey}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      apiKey: event.target.value,
                    }))
                  }
                />
                <p className="text-xs text-muted-foreground">
                  {editing ? L.apiKeyEditHint : L.apiKeyCreateHint}
                </p>
              </div>
            </>
          )}

          <div className="flex justify-end gap-2 pt-2">
            <Button ghost size="sm" type="button" disabled={saving} onClick={onClose}>
              {commonStrings.cancel}
            </Button>
            <Button
              className="uppercase"
              size="sm"
              type="submit"
              disabled={saving}
              prefix={saving ? <Spinner /> : undefined}
            >
              {saving ? commonStrings.saving : commonStrings.save}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
