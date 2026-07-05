import {
  GatewayClient,
  type ConnectionState,
  type GatewayEvent,
} from "@/lib/gatewayClient";
import { getHermesBrowserId } from "@/lib/browserIdentity";
import type { SessionCreateResponse, SessionResumeResponse } from "./protocol";

export interface ConnectGuiChatOptions {
  profile?: string;
  resumeSessionId?: string | null;
}

export interface GuiChatEventSource {
  onEvent(handler: (event: GatewayEvent) => void): () => void;
  onState(handler: (state: ConnectionState) => void): () => void;
}

export interface GuiChatConnection {
  client: GuiChatEventSource;
  close(): void;
  createOrResume(): Promise<SessionCreateResponse | SessionResumeResponse>;
  send(sessionId: string, text: string): Promise<void>;
  stop(sessionId: string): Promise<void>;
  respondToApproval(sessionId: string, request: unknown, approved: boolean): Promise<void>;
}

export function connectGuiChat(options: ConnectGuiChatOptions): GuiChatConnection {
  const client = new GatewayClient();
  const browserId = getHermesBrowserId();

  return {
    client,
    close: () => client.close(),
    createOrResume: async () => {
      await client.connect();
      const baseParams = {
        browser_id: browserId,
        close_on_disconnect: false,
        source: "dashboard-gui",
        ...(options.profile ? { profile: options.profile } : {}),
      };
      if (options.resumeSessionId) {
        return client.request<SessionResumeResponse>("session.resume", {
          ...baseParams,
          session_id: options.resumeSessionId,
        });
      }
      return client.request<SessionCreateResponse>("session.create", baseParams);
    },
    respondToApproval: async (sessionId, _request, approved) => {
      await client.request("approval.respond", {
        choice: approved ? "allow" : "deny",
        session_id: sessionId,
      });
    },
    send: async (sessionId, text) => {
      await client.request("prompt.submit", { session_id: sessionId, text });
    },
    stop: async (sessionId) => {
      await client.request("session.interrupt", { session_id: sessionId }, 30_000);
    },
  };
}
