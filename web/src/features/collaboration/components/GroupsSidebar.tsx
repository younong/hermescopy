import { Archive, ChevronDown, ChevronRight, Plus, UsersRound } from "lucide-react";
import { useMemo, useState } from "react";
import type { CollaborationGroup } from "../types";

interface GroupsSidebarProps {
  groups: CollaborationGroup[];
  activeGroupId: string | null;
  loading: boolean;
  query?: string;
  onCreate(): void;
  onPick(groupId: string): void;
}

export function GroupsSidebar({
  activeGroupId,
  groups,
  loading,
  onCreate,
  onPick,
  query = "",
}: GroupsSidebarProps) {
  const [archivedOpen, setArchivedOpen] = useState(false);
  const filtered = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return normalized
      ? groups.filter((group) => group.name.toLocaleLowerCase().includes(normalized))
      : groups;
  }, [groups, query]);
  const active = filtered.filter((group) => group.status === "active");
  const archived = filtered.filter((group) => group.status === "archived");

  return (
    <section aria-label="Groups" className="mt-4 px-3">
      <div className="gui-chat-section-heading">
        <span>Groups</span>
        <button aria-label="Create group" className="gui-chat-icon-button" onClick={onCreate} type="button">
          <Plus className="h-3.5 w-3.5" />
        </button>
      </div>
      <div className="space-y-[3px]">
        {active.map((group) => (
          <GroupButton
            active={group.group_id === activeGroupId}
            group={group}
            key={group.group_id}
            onPick={onPick}
          />
        ))}
        {!loading && active.length === 0 ? (
          <p className="px-2 py-2 text-[11px] text-[#969aa1]">No active groups</p>
        ) : null}
        {archived.length > 0 ? (
          <>
            <button
              aria-expanded={archivedOpen}
              className="flex w-full items-center gap-1.5 rounded-[7px] px-2 py-1.5 text-[11px] font-medium text-[#777c84] hover:bg-black/[0.04]"
              onClick={() => setArchivedOpen((value) => !value)}
              type="button"
            >
              {archivedOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
              <Archive className="h-3 w-3" />
              Archived ({archived.length})
            </button>
            {archivedOpen
              ? archived.map((group) => (
                  <GroupButton
                    active={group.group_id === activeGroupId}
                    group={group}
                    key={group.group_id}
                    onPick={onPick}
                  />
                ))
              : null}
          </>
        ) : null}
      </div>
    </section>
  );
}

function GroupButton({
  active,
  group,
  onPick,
}: {
  active: boolean;
  group: CollaborationGroup;
  onPick(groupId: string): void;
}) {
  return (
    <button
      aria-current={active ? "page" : undefined}
      className={`gui-chat-nav-item ${active ? "bg-white text-black" : ""}`}
      onClick={() => onPick(group.group_id)}
      type="button"
    >
      <UsersRound />
      <span className="truncate">{group.name}</span>
    </button>
  );
}
