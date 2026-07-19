import {
  GatewayClient,
  type ConnectionState,
  type GatewayConnectTiming,
  type GatewayEvent,
} from "@/lib/gatewayClient";
import { getHermesBrowserId } from "@/lib/browserIdentity";
import { base64FromDataUrl, readFileAsDataUrl } from "./attachments";
import type { SessionCreateResponse, SessionResumeResponse } from "./protocol";

export interface ConnectGuiChatOptions {
  profile?: string;
  ownerKey?: string;
  resumeSessionId?: string | null;
  timing?: GatewayConnectTiming;
}

export interface GuiChatEventSource {
  onEvent(handler: (event: GatewayEvent) => void): () => void;
  onState(handler: (state: ConnectionState) => void): () => void;
}

export interface ImageAttachResponse {
  attached?: boolean;
  path?: string;
  count?: number;
  remainder?: string;
  text?: string;
  bytes?: number;
  name?: string;
  width?: number;
  height?: number;
  token_estimate?: number;
  message?: string;
}

export interface PdfAttachResponse {
  attached?: boolean;
  filename?: string;
  path?: string;
  pages_attached?: number;
  pages?: Array<{
    path?: string;
    page?: number;
    name?: string;
    width?: number;
    height?: number;
    token_estimate?: number;
  }>;
  count?: number;
  text?: string;
  message?: string;
}

export interface FileAttachResponse {
  attached?: boolean;
  name?: string;
  path?: string;
  ref_path?: string;
  ref_text?: string;
  uploaded?: boolean;
  message?: string;
}

export interface GuiChatConnection {
  client: GuiChatEventSource;
  close(): void;
  createOrResume(signal?: AbortSignal): Promise<SessionCreateResponse | SessionResumeResponse>;
  attachImage(sessionId: string, file: File): Promise<ImageAttachResponse>;
  attachPdf(sessionId: string, file: File): Promise<PdfAttachResponse>;
  attachFile(sessionId: string, file: File): Promise<FileAttachResponse>;
  send(sessionId: string, text: string): Promise<void>;
  stop(sessionId: string): Promise<void>;
  respondToApproval(sessionId: string, request: unknown, approved: boolean): Promise<void>;
}

export function connectGuiChat(options: ConnectGuiChatOptions): GuiChatConnection {
  const client = new GatewayClient();
  const browserId = getHermesBrowserId(options.ownerKey);

  return {
    client,
    close: () => client.close(),
    createOrResume: async (signal) => {
      await client.connect(undefined, signal, options.timing);
      const baseParams = {
        browser_id: browserId,
        close_on_disconnect: false,
        source: "dashboard-gui",
        ...(options.timing?.traceId
          ? { latency_trace_id: options.timing.traceId }
          : {}),
        ...(options.profile ? { profile: options.profile } : {}),
      };
      if (options.resumeSessionId) {
        return client.request<SessionResumeResponse>("session.resume", {
          ...baseParams,
          session_id: options.resumeSessionId,
        }, undefined, signal);
      }
      return client.request<SessionCreateResponse>("session.create", baseParams, undefined, signal);
    },
    attachImage: async (sessionId, file) => {
      const dataUrl = await readFileAsDataUrl(file);
      const contentBase64 = base64FromDataUrl(dataUrl);
      if (!contentBase64) throw new Error(`Could not read ${file.name}`);
      return client.request<ImageAttachResponse>("image.attach_bytes", {
        content_base64: contentBase64,
        filename: file.name,
        session_id: sessionId,
      });
    },
    attachPdf: async (sessionId, file) => {
      const dataUrl = await readFileAsDataUrl(file);
      const contentBase64 = base64FromDataUrl(dataUrl);
      if (!contentBase64) throw new Error(`Could not read ${file.name}`);
      return client.request<PdfAttachResponse>("pdf.attach", {
        content_base64: contentBase64,
        filename: file.name,
        session_id: sessionId,
      });
    },
    attachFile: async (sessionId, file) => {
      const dataUrl = await readFileAsDataUrl(file);
      return client.request<FileAttachResponse>("file.attach", {
        data_url: dataUrl,
        name: file.name,
        path: file.name,
        session_id: sessionId,
      });
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
