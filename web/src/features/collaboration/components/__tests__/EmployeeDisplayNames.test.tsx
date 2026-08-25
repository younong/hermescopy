// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@/i18n";
import type { Employee } from "@/lib/api";
import type { CollaborationMembership } from "../../types";
import { CreateGroupDialog } from "../CreateGroupDialog";
import { MemberManager } from "../MemberManager";

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  localStorage.setItem("hermes-locale", "en");
  document.body.innerHTML = "";
});

afterEach(async () => {
  await act(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
});

async function render(node: React.ReactNode) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(<I18nProvider>{node}</I18nProvider>);
    await Promise.resolve();
  });
}

describe("collaboration employee display names", () => {
  it("offers the active built-in assistant in group creation with its nickname and built-in role", async () => {
    await render(
      <CreateGroupDialog
        employees={[builtinEmployee()]}
        onClose={vi.fn()}
        onCreate={vi.fn()}
      />,
    );

    expect(document.body.textContent).toContain("Nova");
    expect(document.body.textContent).toContain("Built-in general assistant");
    expect(document.querySelector<HTMLInputElement>('input[type="checkbox"]')).not.toBeNull();
  });

  it("keeps the built-in assistant visible in member management and uses its nickname", async () => {
    const employee = builtinEmployee();
    const membership: CollaborationMembership = {
      employee_id: employee.employee_id,
      group_id: "group-a",
      created_at: 1,
      join_sequence: 1,
      leave_sequence: null,
      left_at: null,
      membership_id: "membership-a",
      profile_fingerprint: employee.profile_fingerprint,
      profile_revision: employee.profile_revision,
      role: "member",
    };

    await render(
      <MemberManager
        employees={[employee]}
        memberships={[membership]}
        onClose={vi.fn()}
        onSave={vi.fn()}
      />,
    );

    expect(document.body.textContent).toContain("Nova");
    expect(document.body.textContent).toContain("Built-in general assistant");
    expect(document.querySelector<HTMLInputElement>('input[type="checkbox"]')?.checked).toBe(true);
  });
});

function builtinEmployee(): Extract<Employee, { employee_kind: "builtin_assistant" }> {
  return {
    avatar_url: null,
    builtin_assistant_personalization: {
      nickname: "Nova",
      personal_preference: "Prefer concise answers.",
    },
    channels: {},
    chat_eligible: true,
    collaboration_policy: {
      invite_quota: null,
      may_create_groups: true,
      may_create_scheduled_tasks: true,
      may_participate: true,
    },
    employee_id: "builtin-a",
    employee_kind: "builtin_assistant",
    lifecycle_status: "active",
    profile: null,
    profile_fingerprint: "sha256:builtin",
    profile_revision: 4,
    protected: true,
  };
}
