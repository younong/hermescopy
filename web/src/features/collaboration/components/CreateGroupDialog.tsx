import { Button } from "@nous-research/ui/ui/components/button";
import { Input } from "@nous-research/ui/ui/components/input";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { X } from "lucide-react";
import { useMemo, useState } from "react";
import type { FeishuEmployee } from "@/lib/api";

interface CreateGroupDialogProps {
  employees: FeishuEmployee[];
  onClose(): void;
  onCreate(name: string, accountIds: string[]): Promise<void>;
}

export function CreateGroupDialog({ employees, onClose, onCreate }: CreateGroupDialogProps) {
  const [name, setName] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const eligible = useMemo(
    () => employees.filter((employee) =>
      employee.lifecycle_status === "active" &&
      employee.profile !== null &&
      employee.collaboration_policy.may_participate
    ),
    [employees],
  );

  const submit = async () => {
    if (!name.trim()) {
      setError("Group name is required");
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
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/35 p-4" role="dialog" aria-modal="true" aria-label="Create group">
      <div className="w-full max-w-md rounded-xl border border-[#e1e3e7] bg-white p-5 shadow-2xl">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-[15px] font-semibold text-[#25282d]">Create group</h2>
            <p className="mt-1 text-[11px] text-[#969aa1]">Choose managed employees. Their current profiles are pinned by the server.</p>
          </div>
          <button aria-label="Close" className="gui-chat-icon-button" onClick={onClose} type="button"><X /></button>
        </div>
        <Input className="mt-4" aria-label="Group name" placeholder="Group name" value={name} onChange={(event) => setName(event.target.value)} />
        <div className="mt-4 max-h-64 space-y-1 overflow-auto">
          {eligible.length === 0 ? (
            <p className="rounded-lg border border-dashed border-[#dfe2e7] p-4 text-center text-xs text-[#969aa1]">No active employees can participate.</p>
          ) : eligible.map((employee) => {
            const checked = selected.includes(employee.account_id);
            return (
              <label className="flex cursor-pointer items-center gap-3 rounded-lg px-2 py-2 hover:bg-[#f6f7f9]" key={employee.account_id}>
                <input
                  checked={checked}
                  onChange={() => setSelected((current) => checked
                    ? current.filter((id) => id !== employee.account_id)
                    : [...current, employee.account_id])}
                  type="checkbox"
                />
                <span className="min-w-0">
                  <span className="block truncate text-sm font-medium text-[#303238]">{employee.profile?.name || employee.app_id}</span>
                  <span className="block truncate text-[11px] text-[#969aa1]">{employee.profile?.role || "AI employee"}</span>
                </span>
              </label>
            );
          })}
        </div>
        {error ? <p className="mt-3 text-xs text-[#b42318]" role="alert">{error}</p> : null}
        <div className="mt-5 flex justify-end gap-2">
          <Button ghost size="sm" onClick={onClose}>Cancel</Button>
          <Button size="sm" disabled={saving} onClick={() => void submit()}>{saving ? <><Spinner /> Creating…</> : "Create"}</Button>
        </div>
      </div>
    </div>
  );
}
