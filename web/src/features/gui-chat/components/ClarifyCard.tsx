import { useEffect, useMemo, useState } from "react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { CircleHelp } from "lucide-react";

import type { ClarificationState } from "../types";

export function ClarifyCard({
  clarification,
  disabled,
  onRespond,
}: {
  clarification: ClarificationState;
  disabled?: boolean;
  onRespond: (answer: string) => void;
}) {
  const [customAnswer, setCustomAnswer] = useState("");
  const [showCustom, setShowCustom] = useState(!clarification.choices?.length);
  const [now, setNow] = useState(() => Date.now());
  const pending = clarification.status === "pending";
  const submitting = clarification.status === "submitting";

  useEffect(() => {
    if (!pending || !clarification.expiresAtMs) return;
    const interval = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(interval);
  }, [clarification.expiresAtMs, pending]);

  const remainingSeconds = useMemo(() => {
    if (!clarification.expiresAtMs) return null;
    return Math.max(0, Math.ceil((clarification.expiresAtMs - now) / 1_000));
  }, [clarification.expiresAtMs, now]);
  const locallyExpired = remainingSeconds === 0;
  const controlsDisabled = disabled || submitting || !pending || locallyExpired;
  const statusLabel = locallyExpired && pending
    ? "timed out"
    : clarification.status.replace("_", " ");

  return (
    <section
      aria-live="polite"
      className="border border-warning/30 bg-warning/10 px-4 py-3"
      data-clarify-request-id={clarification.id}
    >
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <CircleHelp className="h-4 w-4 text-warning" />
        <span className="font-display text-sm uppercase tracking-[0.12em] text-warning">
          Hermes needs your answer
        </span>
        <Badge tone={pending && !locallyExpired ? "warning" : "secondary"}>{statusLabel}</Badge>
        {pending && remainingSeconds !== null ? (
          <span className="text-xs text-text-tertiary">{remainingSeconds}s remaining</span>
        ) : null}
      </div>
      <p className="mb-3 whitespace-pre-wrap text-sm text-text-primary">
        {clarification.question}
      </p>

      {pending && !locallyExpired ? (
        <div className="space-y-3">
          {clarification.choices?.length && !showCustom ? (
            <div className="flex flex-wrap gap-2">
              {clarification.choices.map((choice) => (
                <Button
                  disabled={controlsDisabled}
                  key={choice}
                  onClick={() => onRespond(choice)}
                  size="sm"
                >
                  {choice}
                </Button>
              ))}
              <Button
                disabled={controlsDisabled}
                ghost
                onClick={() => setShowCustom(true)}
                size="sm"
              >
                Custom answer
              </Button>
            </div>
          ) : (
            <form
              className="flex flex-col gap-2 sm:flex-row"
              onSubmit={(event) => {
                event.preventDefault();
                const answer = customAnswer.trim();
                if (answer && !controlsDisabled) onRespond(answer);
              }}
            >
              <input
                aria-label="Clarification answer"
                autoFocus={!clarification.choices?.length}
                className="min-w-0 flex-1 border border-current/20 bg-background-base px-3 py-2 text-sm text-text-primary outline-none focus:border-warning"
                disabled={controlsDisabled}
                onChange={(event) => setCustomAnswer(event.target.value)}
                placeholder="Type your answer…"
                value={customAnswer}
              />
              <Button disabled={controlsDisabled || !customAnswer.trim()} size="sm" type="submit">
                Answer
              </Button>
              {clarification.choices?.length ? (
                <Button ghost onClick={() => setShowCustom(false)} size="sm" type="button">
                  Back to choices
                </Button>
              ) : null}
            </form>
          )}
        </div>
      ) : null}
    </section>
  );
}
