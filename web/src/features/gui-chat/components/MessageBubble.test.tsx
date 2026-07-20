// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import type { ChatMessage } from "../types";
import { MessageBubble } from "./MessageBubble";

afterEach(() => {
  document.body.innerHTML = "";
});

describe("MessageBubble", () => {
  it("renders assistant Markdown while streaming and catches up on completion", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const message: ChatMessage = {
      artifactIds: [],
      id: "assistant-1",
      role: "assistant",
      streaming: true,
      text:
        "## Streaming reply\n\n" +
        "**Bold text** with `inline code`.\n\n" +
        "- First item\n\n" +
        "| Platform | Direction |\n|---|---|\n| Bilibili | Technology |",
    };

    await act(async () => {
      root.render(<MessageBubble artifacts={[]} message={message} />);
    });

    expect(container.querySelector("h2")?.textContent).toContain("Streaming reply");
    expect(container.querySelector("strong")?.textContent).toBe("Bold text");
    expect(container.querySelector("p code")?.textContent).toBe("inline code");
    expect(container.querySelector("li")?.textContent).toContain("First item");
    expect(container.querySelector("table")?.textContent).toContain("Technology");
    expect(container.querySelector("[data-markdown-streaming=\"true\"]")).not.toBeNull();
    expect(container.textContent).not.toContain("## Streaming reply");
    expect(container.textContent).not.toContain("**Bold text**");

    await act(async () => {
      root.render(
        <MessageBubble
          artifacts={[]}
          message={{
            ...message,
            status: "complete",
            streaming: false,
            text: `${message.text}\n\n\`\`\`ts\nconst complete = true;\n\`\`\``,
          }}
        />,
      );
    });

    expect(container.querySelector("pre code")?.textContent).toBe("const complete = true;\n");
    expect(container.querySelector("[data-markdown-streaming]")).toBeNull();
    expect(container.querySelector("table")?.textContent).toContain("Technology");
    expect(container.textContent).not.toContain("```ts");

    await act(async () => root.unmount());
  });
});
