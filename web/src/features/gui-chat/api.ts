import {
  GatewayClient,
  type ConnectionState,
  JsonRpcGatewayError,
  type GatewayConnectTiming,
  type GatewayEvent,
} from "@/lib/gatewayClient";
import { getHermesBrowserId } from "@/lib/browserIdentity";
import { base64FromDataUrl, readFileAsDataUrl } from "./attachments";
import type {
  SessionAttachResponse,
  SessionCreateResponse,
  SessionResumeResponse,
} from "./protocol";

export interface ConnectGuiChatOptions {
  profile?: string;
  ownerKey?: string;
  timing?: GatewayConnectTiming;
}

export interface GuiChatSwitchTiming extends GatewayConnectTiming {
  onSwitchStage?: (
    stage:
      | "connection.reused"
      | "session.attach.start"
      | "session.attach.end"
      | "session.attach.live"
      | "session.attach.cold"
      | "session.create.start"
      | "session.create.end",
  ) => void;
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
  createOrAttach(
    targetSessionId: string | null,
    generation: number,
    signal?: AbortSignal,
    timing?: GuiChatSwitchTiming,
  ): Promise<SessionCreateResponse | SessionResumeResponse>;
  attachImage(sessionId: string, file: File): Promise<ImageAttachResponse>;
  attachPdf(sessionId: string, file: File): Promise<PdfAttachResponse>;
  attachFile(sessionId: string, file: File): Promise<FileAttachResponse>;
  loadEarlier(
    sessionId: string,
    cursor: string,
    signal?: AbortSignal,
  ): Promise<SessionResumeResponse>;
  send(sessionId: string, text: string): Promise<void>;
  stop(sessionId: string): Promise<void>;
  respondToApproval(sessionId: string, request: unknown, approved: boolean): Promise<void>;
  respondToClarify(sessionId: string, requestId: string, answer: string): Promise<void>;
  ping(): Promise<void>;
}

export function connectGuiChat(options: ConnectGuiChatOptions): GuiChatConnection {
  const client = new GatewayClient();
  const browserId = getHermesBrowserId(options.ownerKey);
  let connectPromise: Promise<void> | null = null;
  let attachSupported = true;

  const ensureConnected = async (
    signal?: AbortSignal,
    timing?: GatewayConnectTiming,
  ): Promise<void> => {
    if (client.connectionState === "open") {
      return;
    }
    if (!connectPromise) {
      // The shared physical handshake belongs to the scoped connection, not to
      // whichever switch happened to start it. Callers can stop awaiting it,
      // but superseding one attach must not tear down the socket all later
      // generations intend to reuse.
      connectPromise = client.connect(undefined, undefined, timing).finally(() => {
        connectPromise = null;
      });
    }
    await abortable(connectPromise, signal);
  };

  const baseParams = (timing?: GuiChatSwitchTiming) => ({
    browser_id: browserId,
    close_on_disconnect: false,
    source: "dashboard-gui",
    display_history: { limit: 100 },
    ...(timing?.traceId ? { latency_trace_id: timing.traceId } : {}),
    ...(options.profile ? { profile: options.profile } : {}),
  });

  return {
    client,
    close: () => client.close(),
    createOrAttach: async (targetSessionId, generation, signal, timing) => {
      const reused = client.connectionState === "open";
      await ensureConnected(signal, timing);
      if (reused) timing?.onSwitchStage?.("connection.reused");

      if (targetSessionId) {
        timing?.onSwitchStage?.("session.attach.start");
        try {
          if (!attachSupported) throw new JsonRpcGatewayError("Method not found", -32601);
          const response = await client.request<SessionAttachResponse>(
            "session.attach",
            {
              ...baseParams(timing),
              session_id: targetSessionId,
              switch_generation: generation,
            },
            undefined,
            signal,
          );
          timing?.onSwitchStage?.("session.attach.end");
          timing?.onSwitchStage?.(
            response.resume_kind === "live" ? "session.attach.live" : "session.attach.cold",
          );
          return response;
        } catch (error) {
          if (!(error instanceof JsonRpcGatewayError) || error.code !== -32601) throw error;
          // Mixed-version compatibility only: an older backend does not know
          // session.attach, so abandon the reusable socket and restore the old
          // fresh-socket session.resume path. Auth/fence failures never fall back.
          attachSupported = false;
          client.close();
          await ensureConnected(signal, timing);
          const response = await client.request<SessionResumeResponse>(
            "session.resume",
            {
              ...baseParams(timing),
              session_id: targetSessionId,
            },
            undefined,
            signal,
          );
          timing?.onSwitchStage?.("session.attach.end");
          return response;
        }
      }

      timing?.onSwitchStage?.("session.create.start");
      const response = await client.request<SessionCreateResponse>(
        "session.create",
        {
          ...baseParams(timing),
          switch_generation: generation,
        },
        undefined,
        signal,
      );
      timing?.onSwitchStage?.("session.create.end");
      return response;
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
    loadEarlier: async (sessionId, cursor, signal) => {
      await ensureConnected(signal);
      return client.request<SessionResumeResponse>(
        "session.history",
        { cursor, limit: 100, session_id: sessionId },
        undefined,
        signal,
      );
    },
    ping: async () => {
      await client.request("gateway.ping", {}, 10_000);
    },
    respondToApproval: async (sessionId, _request, approved) => {
      await client.request("approval.respond", {
        choice: approved ? "allow" : "deny",
        session_id: sessionId,
      });
    },
    respondToClarify: async (sessionId, requestId, answer) => {
      await client.request("clarify.respond", {
        answer,
        request_id: requestId,
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

async function abortable<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return promise;
  if (signal.aborted) throw new DOMException("Aborted", "AbortError");
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(new DOMException("Aborted", "AbortError"));
    signal.addEventListener("abort", onAbort, { once: true });
    void promise.then(
      (value) => {
        signal.removeEventListener("abort", onAbort);
        resolve(value);
      },
      (error: unknown) => {
        signal.removeEventListener("abort", onAbort);
        reject(error);
      },
    );
  });
}
