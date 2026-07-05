import type { GuiChatState } from "../types";
import { ApprovalCard } from "./ApprovalCard";
import { MessageBubble } from "./MessageBubble";
import { ToolCallCard } from "./ToolCallCard";

export function MessageList({
  disabled,
  onApprovalRespond,
  state,
}: {
  disabled?: boolean;
  onApprovalRespond: (id: string, approved: boolean) => void;
  state: GuiChatState;
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-3 py-4 sm:px-5">
      {state.messages.length === 0 && state.toolOrder.length === 0 ? (
        <div className="m-auto max-w-xl border border-current/15 bg-midground/5 px-6 py-5 text-center">
          <h2 className="mb-2 font-display text-lg uppercase tracking-[0.12em] text-midground">
            Hermes GUI Chat beta
          </h2>
          <p className="text-sm text-text-secondary">
            Structured chat over /api/ws. Terminal Chat remains available at /chat.
          </p>
        </div>
      ) : null}

      {state.messages.map((message) => (
        <MessageBubble
          artifacts={message.artifactIds.map((id) => state.artifacts[id]).filter(Boolean)}
          key={message.id}
          message={message}
        />
      ))}

      {state.toolOrder.length > 0 ? (
        <div className="space-y-3">
          {state.toolOrder.map((id) => {
            const tool = state.toolCalls[id];
            if (!tool) return null;
            return (
              <ToolCallCard
                artifacts={tool.artifactIds.map((artifactId) => state.artifacts[artifactId]).filter(Boolean)}
                key={id}
                tool={tool}
              />
            );
          })}
        </div>
      ) : null}

      {state.approvalOrder.map((id) => {
        const approval = state.approvals[id];
        if (!approval) return null;
        return (
          <ApprovalCard
            approval={approval}
            disabled={disabled}
            key={id}
            onRespond={(approved) => onApprovalRespond(id, approved)}
          />
        );
      })}

      {state.statusLines.length > 0 ? (
        <div className="space-y-1 text-xs text-text-tertiary">
          {state.statusLines.slice(-3).map((line, index) => (
            <div key={`${index}-${line}`} className="truncate">
              {line}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
