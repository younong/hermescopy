import { useState, type KeyboardEvent } from "react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Send, Square } from "lucide-react";

export function Composer({
  disabled,
  isGenerating,
  onSend,
  onStop,
}: {
  disabled?: boolean;
  isGenerating: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
}) {
  const [text, setText] = useState("");
  const canSend = text.trim().length > 0 && !disabled && !isGenerating;

  const submit = () => {
    const next = text.trim();
    if (!next || disabled || isGenerating) return;
    setText("");
    onSend(next);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey) return;
    event.preventDefault();
    submit();
  };

  return (
    <form
      className="flex shrink-0 gap-2 border-t border-current/15 bg-background-base/95 p-3"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <textarea
        aria-label="GUI chat message"
        className="min-h-12 max-h-40 flex-1 resize-none border border-current/20 bg-background-base px-3 py-2 text-sm text-text-primary outline-none placeholder:text-text-tertiary focus:border-midground"
        disabled={disabled}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={onKeyDown}
        placeholder={disabled ? "Connecting to Hermes…" : "Message Hermes…"}
        rows={2}
        value={text}
      />
      {isGenerating ? (
        <Button
          type="button"
          ghost
          className="text-destructive"
          onClick={onStop}
          disabled={disabled}
        >
          <Square className="h-4 w-4" />
          Stop
        </Button>
      ) : (
        <Button type="submit" disabled={!canSend}>
          <Send className="h-4 w-4" />
          Send
        </Button>
      )}
    </form>
  );
}
