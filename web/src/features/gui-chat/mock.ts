import type { GatewayEvent } from "@/lib/gatewayClient";
import type { GuiChatConnection } from "./api";
import type { SessionCreateResponse } from "./protocol";

const MOCK_SESSION_ID = "mock-gui";
const MOCK_STORED_SESSION_ID = "mock-gui-session";
const MOCK_IMAGE_URL =
  "data:image/svg+xml;utf8," +
  encodeURIComponent(`
<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540">
  <rect width="960" height="540" fill="#081414"/>
  <rect x="72" y="72" width="816" height="396" fill="#122624" stroke="#f0e6d2" stroke-width="4"/>
  <circle cx="210" cy="210" r="70" fill="#d6ff6b" opacity="0.88"/>
  <path d="M315 360 C430 210 515 430 650 245 C705 170 775 195 830 120" fill="none" stroke="#f0e6d2" stroke-width="16" stroke-linecap="round"/>
  <text x="96" y="430" fill="#f0e6d2" font-family="monospace" font-size="42">Hermes GUI artifact</text>
</svg>`);

export function connectMockGuiChat(): GuiChatConnection {
  const eventHandlers = new Set<(event: GatewayEvent) => void>();
  const stateHandlers = new Set<(state: "idle" | "connecting" | "open" | "closed" | "error") => void>();
  const timers = new Set<ReturnType<typeof setTimeout>>();
  let closed = false;

  const emitState = (state: "idle" | "connecting" | "open" | "closed" | "error") => {
    for (const handler of stateHandlers) handler(state);
  };
  const emitEvent = (event: GatewayEvent) => {
    if (closed) return;
    for (const handler of eventHandlers) handler(event);
  };
  const schedule = (delayMs: number, event: GatewayEvent) => {
    const timer = setTimeout(() => {
      timers.delete(timer);
      emitEvent(event);
    }, delayMs);
    timers.add(timer);
  };

  return {
    client: {
      onEvent(handler) {
        eventHandlers.add(handler);
        return () => eventHandlers.delete(handler);
      },
      onState(handler) {
        stateHandlers.add(handler);
        handler("idle");
        return () => stateHandlers.delete(handler);
      },
    },
    close() {
      closed = true;
      for (const timer of timers) clearTimeout(timer);
      timers.clear();
      eventHandlers.clear();
      emitState("closed");
      stateHandlers.clear();
    },
    async createOrResume(): Promise<SessionCreateResponse> {
      closed = false;
      emitState("connecting");
      schedule(120, {
        type: "session.info",
        session_id: MOCK_SESSION_ID,
        payload: { model: "mock-opus", title: "Mock GUI Chat" },
      });
      await wait(180);
      emitState("open");
      replayIntro(schedule);
      return {
        info: { model: "mock-opus", title: "Mock GUI Chat" },
        message_count: 2,
        messages: [
          { role: "user", text: "先用 mock 数据展示一下 GUI Chat。" },
          {
            role: "assistant",
            text: "可以。这个页面会用结构化事件渲染消息、工具卡片、审批卡片和图片 artifact。",
          },
        ],
        session_id: MOCK_SESSION_ID,
        stored_session_id: MOCK_STORED_SESSION_ID,
      };
    },
    async attachImage() {
      await wait(120);
      return { attached: true, path: "/mock/image.png" };
    },
    async attachPdf() {
      await wait(180);
      return { attached: true, filename: "mock.pdf", pages_attached: 1 };
    },
    async attachFile() {
      await wait(120);
      return { attached: true, name: "mock.txt", ref_text: "@file:.hermes/desktop-attachments/mock.txt" };
    },
    async respondToApproval(_sessionId, _request, approved) {
      emitEvent({
        type: "status.update",
        session_id: MOCK_SESSION_ID,
        payload: { kind: "approval", text: approved ? "Mock approval approved" : "Mock approval denied" },
      });
      emitEvent({
        type: "approval.resolved",
        session_id: MOCK_SESSION_ID,
        payload: { id: "mock-approval" },
      });
    },
    async send(_sessionId, text) {
      replayUserTurn(schedule, text);
    },
    async stop() {
      for (const timer of timers) clearTimeout(timer);
      timers.clear();
      emitEvent({
        type: "message.complete",
        session_id: MOCK_SESSION_ID,
        payload: { status: "interrupted", text: "Mock generation stopped." },
      });
    },
  };
}

function replayIntro(schedule: (delayMs: number, event: GatewayEvent) => void) {
  schedule(300, {
    type: "tool.start",
    session_id: MOCK_SESSION_ID,
    payload: { tool_id: "mock-tool-1", name: "image_generate", args_text: "prompt: Hermes GUI artifact" },
  });
  schedule(800, {
    type: "tool.complete",
    session_id: MOCK_SESSION_ID,
    payload: {
      tool_id: "mock-tool-1",
      name: "image_generate",
      summary: "Generated image ready",
      result: { success: true, image: MOCK_IMAGE_URL },
      result_text: "> web@0.0.0 typecheck\n> tsc -p . --noEmit\n\n✓ no TypeScript errors",
      duration_s: 1.2,
    },
  });
  schedule(1100, {
    type: "artifact.image",
    session_id: MOCK_SESSION_ID,
    payload: {
      id: "mock-image-1",
      messageId: "history-1",
      mimeType: "image/svg+xml",
      title: "Mock generated image",
      url: MOCK_IMAGE_URL,
      width: 960,
      height: 540,
    },
  });
  schedule(1400, {
    type: "approval.request",
    session_id: MOCK_SESSION_ID,
    payload: {
      id: "mock-approval",
      command: "deploy --dry-run hermes-dashboard",
      description: "Mock approval: continue with a dry-run deploy?",
    },
  });
}

function replayUserTurn(
  schedule: (delayMs: number, event: GatewayEvent) => void,
  text: string,
) {
  schedule(80, { type: "message.start", session_id: MOCK_SESSION_ID });
  schedule(220, {
    type: "message.delta",
    session_id: MOCK_SESSION_ID,
    payload: { text: `收到：${text}\n\n` },
  });
  schedule(520, {
    type: "message.delta",
    session_id: MOCK_SESSION_ID,
    payload: { text: "这是 mock 流式回复，支持 **Markdown**、列表和代码块：\n\n" },
  });
  schedule(780, {
    type: "message.delta",
    session_id: MOCK_SESSION_ID,
    payload: { text: "- 消息气泡\n- 工具卡片\n- 图片 artifact\n- 审批卡片\n\n```ts\nconsole.log('gui chat mock');\n```" },
  });
  schedule(1100, {
    type: "message.complete",
    session_id: MOCK_SESSION_ID,
    payload: {
      status: "complete",
      text: `收到：${text}\n\n这是 mock 流式回复，支持 **Markdown**、列表和代码块：\n\n- 消息气泡\n- 工具卡片\n- 图片 artifact\n- 审批卡片\n\n\`\`\`ts\nconsole.log('gui chat mock');\n\`\`\``,
    },
  });
}

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
