import { Button } from "@nous-research/ui/ui/components/button";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { ShieldCheck } from "lucide-react";
import type { ApprovalState } from "../types";

export function ApprovalCard({
  approval,
  disabled,
  onRespond,
}: {
  approval: ApprovalState;
  disabled?: boolean;
  onRespond: (approved: boolean) => void;
}) {
  const pending = approval.status === "pending";
  return (
    <section className="border border-warning/25 bg-warning/10 px-4 py-3">
      <div className="mb-2 flex items-center gap-2">
        <ShieldCheck className="h-4 w-4 text-warning" />
        <span className="font-display text-sm uppercase tracking-[0.12em] text-warning">
          Approval required
        </span>
        <Badge tone={pending ? "warning" : "secondary"}>{approval.status}</Badge>
      </div>
      {approval.description ? (
        <p className="mb-2 text-sm text-text-secondary">{approval.description}</p>
      ) : null}
      {approval.command ? (
        <pre className="mb-3 overflow-auto whitespace-pre-wrap bg-background-base/70 px-3 py-2 text-xs text-text-secondary">
          {approval.command}
        </pre>
      ) : null}
      {pending ? (
        <div className="flex gap-2">
          <Button size="sm" onClick={() => onRespond(true)} disabled={disabled}>
            Approve
          </Button>
          <Button ghost size="sm" onClick={() => onRespond(false)} disabled={disabled}>
            Deny
          </Button>
        </div>
      ) : null}
    </section>
  );
}
