/**
 * Browser WebSocket client for the tui_gateway JSON-RPC protocol.
 *
 * Speaks the exact same newline-delimited JSON-RPC dialect that the Ink TUI
 * drives over stdio. The server-side transport abstraction
 * (tui_gateway/transport.py + ws.py) routes the same dispatcher's writes
 * onto either stdout or a WebSocket depending on how the client connected.
 *
 *   const gw = new GatewayClient()
 *   await gw.connect()
 *   const { session_id } = await gw.request<{ session_id: string }>("session.create")
 *   gw.on("message.delta", (ev) => console.log(ev.payload?.text))
 *   await gw.request("prompt.submit", { session_id, text: "hi" })
 */

import {
  JsonRpcGatewayClient,
  JsonRpcGatewayError,
  buildHermesWebSocketUrl,
  type ConnectionState,
  type GatewayEvent,
  type GatewayEventName,
} from "@hermes/shared";

import { HERMES_BASE_PATH, buildWsAuthParam } from "@/lib/api";
import { diagnosticId, emitChatDiagnostic } from "@/lib/chatDiagnostics";

export { JsonRpcGatewayError };
export type { ConnectionState, GatewayEvent, GatewayEventName };

export interface GatewayConnectTiming {
  onStage?: (stage: "ticket.start" | "ticket.end" | "websocket.construct" | "websocket.open") => void;
  traceId?: string;
}

export class GatewayClient extends JsonRpcGatewayClient {
  constructor() {
    const connectionId = diagnosticId("gui");
    super({
      closedErrorMessage: "WebSocket closed",
      connectErrorMessage: "WebSocket connection failed",
      notConnectedErrorMessage: "gateway not connected",
      onLifecycle: (event) => emitChatDiagnostic({
        clientInitiated: event.clientInitiated,
        closeCode: event.code,
        connectionId,
        event: "closed",
        opened: event.opened,
        pendingCount: event.pendingCount,
        surface: "gui_gateway",
        wasClean: event.wasClean,
      }),
      requestIdPrefix: "w",
    });
  }

  async connect(
    token?: string,
    signal?: AbortSignal,
    timing?: GatewayConnectTiming,
  ): Promise<void> {
    if (this.connectionState === "open" || this.connectionState === "connecting") {
      return;
    }
    if (signal?.aborted) {
      throw new DOMException("Aborted", "AbortError");
    }

    // Gated mode: legacy ``?token=`` is rejected by ``_ws_auth_ok``; the SPA
    // must fetch a single-use ticket. Explicit ``token`` keeps the test-only
    // override path.
    timing?.onStage?.("ticket.start");
    const authParam = token
      ? (["token", token] as const)
      : await buildWsAuthParam("/api/ws", timing?.traceId, signal);
    timing?.onStage?.("ticket.end");
    if (signal?.aborted) {
      throw new DOMException("Aborted", "AbortError");
    }
    if (!authParam[1]) {
      throw new Error(
        "Session token not available — page must be served by the Hermes dashboard server",
      );
    }

    timing?.onStage?.("websocket.construct");
    await super.connect(
      buildHermesWebSocketUrl({
        authParam,
        basePath: HERMES_BASE_PATH,
        path: "/api/ws",
        params: timing?.traceId ? { ws_trace: timing.traceId } : undefined,
      }),
      signal,
    );
    timing?.onStage?.("websocket.open");
  }
}
