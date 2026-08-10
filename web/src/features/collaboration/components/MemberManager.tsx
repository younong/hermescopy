import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { UserRoundCog, X } from "lucide-react";
import { useMemo, useState } from "react";
import type { Employee } from "@/lib/api";
import type { CollaborationMembership } from "../types";

interface MemberManagerProps {
  employees: Employee[];
  memberships: CollaborationMembership[];
  onClose(): void;
  onSave(employeeIds: string[]): Promise<void>;
}

export function MemberManager({ employees, memberships, onClose, onSave }: MemberManagerProps) {
  const initial = useMemo(
    () => memberships.filter((member) => member.leave_sequence === null).map((member) => member.employee_id),
    [memberships],
  );
  const [selected, setSelected] = useState(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const selectable = employees.filter((employee) =>
    employee.profile !== null &&
    (employee.collaboration_policy.may_participate || initial.includes(employee.employee_id))
  );

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await onSave(selected);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/35 p-4" role="dialog" aria-modal="true" aria-label="Manage group members">
      <div className="w-full max-w-md rounded-xl border border-[#e1e3e7] bg-white p-5 shadow-2xl">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2"><UserRoundCog className="h-4 w-4" /><h2 className="text-[15px] font-semibold">Group members</h2></div>
          <button aria-label="Close" className="gui-chat-icon-button" onClick={onClose} type="button"><X /></button>
        </div>
        <p className="mt-1 text-[11px] text-[#969aa1]">Adding a member pins its current employee profile. Removing it keeps conversation history.</p>
        <div className="mt-4 max-h-72 space-y-1 overflow-auto">
          {selectable.map((employee) => {
            const checked = selected.includes(employee.employee_id);
            const revoked = !employee.collaboration_policy.may_participate;
            return (
              <label className="flex cursor-pointer items-center gap-3 rounded-lg px-2 py-2 hover:bg-[#f6f7f9]" key={employee.employee_id}>
                <input
                  checked={checked}
                  disabled={revoked && !checked}
                  onChange={() => setSelected((current) => checked
                    ? current.filter((id) => id !== employee.employee_id)
                    : [...current, employee.employee_id])}
                  type="checkbox"
                />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium">{employee.profile?.name || "Unnamed employee"}</span>
                  <span className="block truncate text-[11px] text-[#969aa1]">{employee.profile?.role || "AI employee"}{revoked ? " · participation revoked" : ""}</span>
                </span>
              </label>
            );
          })}
        </div>
        {error ? <p className="mt-3 text-xs text-[#b42318]" role="alert">{error}</p> : null}
        <div className="mt-5 flex justify-end gap-2">
          <Button ghost size="sm" onClick={onClose}>Cancel</Button>
          <Button size="sm" disabled={saving} onClick={() => void save()}>{saving ? <><Spinner /> Saving…</> : "Save members"}</Button>
        </div>
      </div>
    </div>
  );
}
