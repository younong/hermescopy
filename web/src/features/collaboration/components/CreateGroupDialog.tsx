import { Button } from "@nous-research/ui/ui/components/button";
import { Input } from "@nous-research/ui/ui/components/input";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { X } from "lucide-react";
import { useMemo, useState } from "react";
import { guiChatTranslations, useI18n } from "@/i18n";
import { employeeDisplayName, employeeDisplayRole, type Employee } from "@/lib/api";

interface CreateGroupDialogProps {
  employees: Employee[];
  onClose(): void;
  onCreate(name: string, employeeIds: string[]): Promise<void>;
}

export function CreateGroupDialog({ employees, onClose, onCreate }: CreateGroupDialogProps) {
  const { t } = useI18n();
  const copy = guiChatTranslations(t).collaboration;
  const [name, setName] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const eligible = useMemo(
    () => employees.filter((employee) =>
      employee.lifecycle_status === "active" &&
      employee.collaboration_policy.may_participate
    ),
    [employees],
  );

  const submit = async () => {
    if (!name.trim()) {
      setError(copy.groupNameRequired);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onCreate(name.trim(), selected);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/35 p-4" role="dialog" aria-modal="true" aria-label={copy.createGroup}>
      <div className="w-full max-w-md rounded-xl border border-[#e1e3e7] bg-white p-5 shadow-2xl">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-[15px] font-semibold text-[#25282d]">{copy.createGroup}</h2>
            <p className="mt-1 text-[11px] text-[#969aa1]">{copy.createGroupDescription}</p>
          </div>
          <button aria-label={t.common.close} className="gui-chat-icon-button" onClick={onClose} type="button"><X /></button>
        </div>
        <Input className="mt-4" aria-label={copy.groupName} placeholder={copy.groupName} value={name} onChange={(event) => setName(event.target.value)} />
        <div className="mt-4 max-h-64 space-y-1 overflow-auto">
          {eligible.length === 0 ? (
            <p className="rounded-lg border border-dashed border-[#dfe2e7] p-4 text-center text-xs text-[#969aa1]">{copy.noEligibleEmployees}</p>
          ) : eligible.map((employee) => {
            const checked = selected.includes(employee.employee_id);
            return (
              <label className="flex cursor-pointer items-center gap-3 rounded-lg px-2 py-2 hover:bg-[#f6f7f9]" key={employee.employee_id}>
                <input
                  checked={checked}
                  onChange={() => setSelected((current) => checked
                    ? current.filter((id) => id !== employee.employee_id)
                    : [...current, employee.employee_id])}
                  type="checkbox"
                />
                <span className="min-w-0">
                  <span className="block truncate text-sm font-medium text-[#303238]">{employeeDisplayName(employee, copy.builtinAssistant, copy.unnamedEmployee)}</span>
                  <span className="block truncate text-[11px] text-[#969aa1]">{employeeDisplayRole(employee, copy.builtinDescription, copy.aiEmployee)}</span>
                </span>
              </label>
            );
          })}
        </div>
        {error ? <p className="mt-3 text-xs text-[#b42318]" role="alert">{error}</p> : null}
        <div className="mt-5 flex justify-end gap-2">
          <Button ghost size="sm" onClick={onClose}>{t.common.cancel}</Button>
          <Button size="sm" disabled={saving} onClick={() => void submit()}>{saving ? <><Spinner /> {t.common.creating}</> : t.common.create}</Button>
        </div>
      </div>
    </div>
  );
}
