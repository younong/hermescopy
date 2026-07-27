#!/usr/bin/env python3
"""Run an authenticated public-dashboard conversation smoke test."""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from playwright_dashboard_login import (
    DEFAULT_URL,
    Credentials,
    LoginError,
    _redact,
    login_dashboard,
    load_credentials,
    normalize_dashboard_url,
    run_secure_playwright_code,
    validate_session_name,
)

SCHEMA_VERSION = 1
KIND = "hermes.public-conversation-smoke"
CONTINUITY_KIND = "hermes.public-continuity-smoke"
CONTINUITY_STATE_KEY = "__hermesReleaseContinuitySmoke"
DEFAULT_SESSION = "hermes-release-smoke"
DEFAULT_TIMEOUT = 180.0


def _bounded(value: object, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _smoke_javascript(*, base: str, path_prefix: str, marker: str, timeout_ms: int) -> str:
    config = json.dumps(
        {
            "base": base,
            "pathPrefix": path_prefix,
            "marker": marker,
            "browserId": f"release-smoke-{marker}",
            "timeoutMs": timeout_ms,
        },
        ensure_ascii=False,
    )
    return rf"""async (page) => {{
  const config = {config};
  return await page.evaluate(async (config) => {{
  const checks = [];
  let socket = null;
  let liveSessionId = '';
  let storedSessionId = '';
  let requestId = 0;
  let pending = new Map();
  let events = [];
  let cleanup = {{ sessionClosed: false, sessionDeleted: false, socketClosed: false }};
  let activeCheck = 'public_login';
  const now = () => Date.now();
  const pass = (name, started, details = {{}}) => checks.push({{
    name, status: 'passed', durationMs: Date.now() - started, ...details,
  }});
  const timeout = (label, ms = config.timeoutMs) => new Promise((_, reject) =>
    setTimeout(() => reject(new Error(`${{label}} timed out`)), ms));
  const withTimeout = (promise, label, ms = config.timeoutMs) =>
    Promise.race([promise, timeout(label, ms)]);
  const readerJSON = async (path, label) => {{
    const deadline = now() + Math.min(config.timeoutMs, 10_000);
    while (true) {{
      const response = await fetch(new URL(path, config.base), {{
        credentials: 'include',
        cache: 'no-store',
      }});
      if (response.status !== 503 || now() >= deadline) {{
        if (!response.ok) throw new Error(`${{label}} HTTP ${{response.status}}`);
        try {{ return await response.json(); }}
        catch (_) {{ throw new Error(`${{label}} returned invalid JSON`); }}
      }}
      const retrySeconds = Number(response.headers.get('Retry-After') || '1');
      const retryMs = Number.isFinite(retrySeconds)
        ? Math.min(1000, Math.max(100, retrySeconds * 1000))
        : 1000;
      await new Promise((resolve) => setTimeout(resolve, retryMs));
    }}
  }};

  const baseParams = (generation) => ({{
    browser_id: config.browserId,
    close_on_disconnect: false,
    source: 'dashboard-gui',
    switch_generation: generation,
  }});

  const waitAnyEvent = async (types, sessionId = '') => {{
    const find = () => events.findIndex((event) =>
      types.includes(event.type) && (!sessionId || event.session_id === sessionId));
    const existing = find();
    if (existing >= 0) return events.splice(existing, 1)[0];
    let poll = null;
    try {{
      return await withTimeout(new Promise((resolve) => {{
        poll = setInterval(() => {{
          const index = find();
          if (index >= 0) {{
            clearInterval(poll);
            poll = null;
            resolve(events.splice(index, 1)[0]);
          }}
        }}, 20);
      }}), `event ${{types.join('|')}}`);
    }} finally {{
      if (poll) clearInterval(poll);
    }}
  }};
  const waitEvent = (type, sessionId = '') => waitAnyEvent([type], sessionId);

  const request = (method, params, ms = config.timeoutMs) => {{
    if (!socket || socket.readyState !== WebSocket.OPEN) {{
      return Promise.reject(new Error(`socket unavailable for ${{method}}`));
    }}
    const id = `release-smoke-${{++requestId}}`;
    return withTimeout(new Promise((resolve, reject) => {{
      pending.set(id, {{ resolve, reject }});
      socket.send(JSON.stringify({{ jsonrpc: '2.0', id, method, params }}));
    }}), `RPC ${{method}}`, ms).finally(() => pending.delete(id));
  }};

  const connect = async () => {{
    const ticketUrl = new URL('api/auth/ws-ticket', config.base).toString();
    const response = await fetch(ticketUrl, {{
      method: 'POST',
      credentials: 'include',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ audience: 'browser-ws:/api/ws' }}),
    }});
    if (!response.ok) throw new Error(`ticket HTTP ${{response.status}}`);
    const payload = await response.json();
    if (!payload || typeof payload.ticket !== 'string' || !payload.ticket) {{
      throw new Error('ticket response missing ticket');
    }}
    const ticketResponse = payload.ticket;
    const endpoint = new URL(config.base);
    endpoint.protocol = endpoint.protocol === 'https:' ? 'wss:' : 'ws:';
    endpoint.pathname = `${{config.pathPrefix.replace(/\/$/, '')}}/api/ws`;
    endpoint.search = `?ticket=${{encodeURIComponent(ticketResponse)}}`;
    const ws = new WebSocket(endpoint.toString());
    socket = ws;
    pending = new Map();
    events = [];
    ws.addEventListener('message', (message) => {{
      let frame;
      try {{ frame = JSON.parse(String(message.data)); }} catch (_) {{ return; }}
      if (frame.id !== undefined && frame.id !== null) {{
        const call = pending.get(frame.id);
        if (!call) return;
        if (frame.error) call.reject(new Error(`RPC rejected: ${{frame.error.code || 'error'}}`));
        else call.resolve(frame.result || {{}});
        return;
      }}
      if (frame.method === 'event' && frame.params && frame.params.type) {{
        events.push(frame.params);
      }}
    }});
    await withTimeout(new Promise((resolve, reject) => {{
      ws.addEventListener('open', resolve, {{ once: true }});
      ws.addEventListener('error', () => reject(new Error('WebSocket connection failed')), {{ once: true }});
    }}), 'WebSocket open', 15000);
    return ws;
  }};

  const closeSocket = async () => {{
    if (!socket) return;
    const current = socket;
    socket = null;
    if (current.readyState === WebSocket.CLOSED) {{ cleanup.socketClosed = true; return; }}
    const closed = new Promise((resolve) => current.addEventListener('close', resolve, {{ once: true }}));
    current.close();
    await Promise.race([closed, new Promise((resolve) => setTimeout(resolve, 1500))]);
    cleanup.socketClosed = true;
  }};

  try {{
    let started = now();
    activeCheck = 'public_ws_ticket';
    await connect();
    const ready = await waitEvent('gateway.ready');
    if (!ready) throw new Error('gateway.ready missing');
    pass('public_ws_ticket', started, {{ path: `${{config.pathPrefix}}api/ws` }});

    started = now();
    activeCheck = 'public_session_create';
    const created = await request('session.create', baseParams(1));
    liveSessionId = String(created.session_id || '');
    storedSessionId = String(created.stored_session_id || '');
    if (!liveSessionId || !storedSessionId) throw new Error('session.create omitted an ID');
    await waitEvent('session.info', liveSessionId);
    pass('public_session_create', started);

    started = now();
    activeCheck = 'public_model_response';
    await request('prompt.submit', {{
      session_id: liveSessionId,
      text: `Release smoke ${{config.marker}}. Reply briefly and include this marker exactly: ${{config.marker}}`,
    }});
    let deltaCount = 0;
    let completed = null;
    while (!completed) {{
      const event = await waitAnyEvent(['message.delta', 'message.complete'], liveSessionId);
      if (event.type === 'message.delta') {{
        if (event.payload && typeof event.payload.text === 'string' && event.payload.text) deltaCount++;
      }} else {{
        completed = event;
      }}
    }}
    const completion = completed.payload || {{}};
    if (deltaCount < 1 || completion.status !== 'complete') {{
      throw new Error('model response did not stream to completion');
    }}
    if (!String(completion.text || '').includes(config.marker)) {{
      throw new Error('model response omitted the release marker');
    }}
    pass('public_model_response', started, {{ deltaCount }});

    started = now();
    activeCheck = 'public_cold_resume';
    const closed = await request('session.close', {{ session_id: liveSessionId }});
    if (closed.closed !== true) throw new Error('session.close did not close the live session');
    cleanup.sessionClosed = true;
    liveSessionId = '';
    await closeSocket();

    await connect();
    await waitEvent('gateway.ready');
    const resumed = await request('session.resume', {{
      ...baseParams(2),
      session_id: storedSessionId,
    }});
    liveSessionId = String(resumed.session_id || '');
    const restored = JSON.stringify(resumed.messages || []);
    if (!liveSessionId || !restored.includes(config.marker)) {{
      throw new Error('cold resume did not restore the smoke transcript');
    }}
    pass('public_cold_resume', started);

    started = now();
    activeCheck = 'public_session_reader_list';
    const listPath = `${{config.pathPrefix}}api/sessions?limit=30&offset=0&order=recent&compact=true`;
    const listed = await readerJSON(listPath, activeCheck);
    if (!Array.isArray(listed.sessions) ||
        !listed.sessions.some((session) => String(session && session.id || '') === storedSessionId)) {{
      throw new Error('session reader list omitted the smoke session');
    }}
    pass('public_session_reader_list', started);

    started = now();
    activeCheck = 'public_session_reader_messages';
    const messagesPath = `${{config.pathPrefix}}api/sessions/${{encodeURIComponent(storedSessionId)}}/messages?limit=100`;
    const history = await readerJSON(messagesPath, activeCheck);
    if (!Array.isArray(history.messages) || !JSON.stringify(history.messages).includes(config.marker)) {{
      throw new Error('session reader messages omitted the smoke marker');
    }}
    pass('public_session_reader_messages', started);

    started = now();
    activeCheck = 'public_cleanup';
    const resumedClosed = await request('session.close', {{ session_id: liveSessionId }});
    if (resumedClosed.closed !== true) throw new Error('resumed session did not close');
    cleanup.sessionClosed = true;
    liveSessionId = '';
    const deleted = await request('session.delete', {{ session_id: storedSessionId }});
    if (deleted.deleted !== storedSessionId) throw new Error('session.delete did not remove the smoke session');
    cleanup.sessionDeleted = true;
    storedSessionId = '';
    await closeSocket();
    pass('public_cleanup', started);
    return {{ ok: true, checks, cleanup }};
  }} catch (error) {{
    return {{
      ok: false,
      checks,
      cleanup,
      failure: {{
        code: String(error && error.message || '').includes('timed out') ? 'timeout' : 'public_smoke_failed',
        check: activeCheck,
        message: String(error && error.message || 'public smoke failed').slice(0, 500),
      }},
    }};
  }} finally {{
    if (socket && socket.readyState === WebSocket.OPEN) {{
      try {{
        if (liveSessionId) {{
          await request('session.close', {{ session_id: liveSessionId }}, 5000);
          cleanup.sessionClosed = true;
        }}
      }} catch (_) {{}}
      liveSessionId = '';
      if (storedSessionId) {{
        try {{
          await request('session.delete', {{ session_id: storedSessionId }}, 5000);
          cleanup.sessionDeleted = true;
        }} catch (_) {{}}
      }}
      try {{ await closeSocket(); }} catch (_) {{}}
    }}
  }}
  }}, config);
}}
"""


def _continuity_javascript(
    *,
    base: str,
    path_prefix: str,
    marker: str,
    timeout_ms: int,
    phase: str,
) -> str:
    config = json.dumps(
        {
            "base": base,
            "pathPrefix": path_prefix,
            "marker": marker,
            "browserId": f"release-continuity-{marker}",
            "timeoutMs": timeout_ms,
            "phase": phase,
            "stateKey": CONTINUITY_STATE_KEY,
        },
        ensure_ascii=False,
    )
    return rf"""async (page) => {{
  const config = {config};
  return await page.evaluate(async (config) => {{
    const checks = [];
    let socket = null;
    let pending = new Map();
    let events = [];
    let requestId = 0;
    let activeCheck = config.phase === 'prepare' ? 'continuity_prepare' : 'continuity_reconnect';
    const cleanup = {{ sessionClosed: false, sessionDeleted: false, socketClosed: false }};
    const now = () => Date.now();
    let deadline = now() + config.timeoutMs;
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const pass = (name, started, details = {{}}) => checks.push({{
      name, status: 'passed', durationMs: now() - started, ...details,
    }});
    const withTimeout = (promise, label, ms = config.timeoutMs) => Promise.race([
      promise,
      new Promise((_, reject) => setTimeout(() => reject(new Error(`${{label}} timed out`)), ms)),
    ]);
    const state = window[config.stateKey] || {{
      browserId: config.browserId,
      marker: config.marker,
      storedSessionId: '',
      liveSessionId: '',
      socket: null,
      closeCode: null,
    }};
    window[config.stateKey] = state;

    const waitAnyEvent = async (types, sessionId = '') => {{
      const find = () => events.findIndex((event) =>
        types.includes(event.type) && (!sessionId || event.session_id === sessionId));
      while (now() < deadline) {{
        const index = find();
        if (index >= 0) return events.splice(index, 1)[0];
        await sleep(20);
      }}
      throw new Error(`event ${{types.join('|')}} timed out`);
    }};
    const waitEvent = (type, sessionId = '') => waitAnyEvent([type], sessionId);
    const request = (method, params, ms = config.timeoutMs) => {{
      if (!socket || socket.readyState !== WebSocket.OPEN) {{
        return Promise.reject(new Error(`socket unavailable for ${{method}}`));
      }}
      const id = `release-continuity-${{++requestId}}`;
      return withTimeout(new Promise((resolve, reject) => {{
        pending.set(id, {{ resolve, reject }});
        socket.send(JSON.stringify({{ jsonrpc: '2.0', id, method, params }}));
      }}), `RPC ${{method}}`, ms).finally(() => pending.delete(id));
    }};
    const connectOnce = async () => {{
      const ticketUrl = new URL('api/auth/ws-ticket', config.base).toString();
      const response = await fetch(ticketUrl, {{
        method: 'POST', credentials: 'include',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ audience: 'browser-ws:/api/ws' }}),
      }});
      if (!response.ok) throw new Error(`ticket HTTP ${{response.status}}`);
      const payload = await response.json();
      if (!payload || typeof payload.ticket !== 'string' || !payload.ticket) {{
        throw new Error('ticket response missing ticket');
      }}
      const endpoint = new URL(config.base);
      endpoint.protocol = endpoint.protocol === 'https:' ? 'wss:' : 'ws:';
      endpoint.pathname = `${{config.pathPrefix.replace(/\/$/, '')}}/api/ws`;
      endpoint.search = `?ticket=${{encodeURIComponent(payload.ticket)}}`;
      const ws = new WebSocket(endpoint.toString());
      socket = ws;
      pending = new Map();
      events = [];
      ws.addEventListener('message', (message) => {{
        let frame;
        try {{ frame = JSON.parse(String(message.data)); }} catch (_) {{ return; }}
        if (frame.id !== undefined && frame.id !== null) {{
          const call = pending.get(frame.id);
          if (!call) return;
          if (frame.error) call.reject(new Error(`RPC rejected: ${{frame.error.code || 'error'}}`));
          else call.resolve(frame.result || {{}});
        }} else if (frame.method === 'event' && frame.params && frame.params.type) {{
          events.push(frame.params);
        }}
      }});
      await withTimeout(new Promise((resolve, reject) => {{
        ws.addEventListener('open', resolve, {{ once: true }});
        ws.addEventListener('error', () => reject(new Error('WebSocket connection failed')), {{ once: true }});
      }}), 'WebSocket open', 15_000);
      state.socket = ws;
      return ws;
    }};
    const connectUntilReady = async () => {{
      let lastError = null;
      while (now() < deadline) {{
        try {{
          await connectOnce();
          await waitEvent('gateway.ready');
          return;
        }} catch (error) {{
          lastError = error;
          if (socket) try {{ socket.close(); }} catch (_) {{}}
          socket = null;
          state.socket = null;
          await sleep(500);
        }}
      }}
      throw lastError || new Error('continuity reconnect timed out');
    }};
    const closeSocket = async () => {{
      if (!socket) return;
      const current = socket;
      socket = null;
      state.socket = null;
      if (current.readyState !== WebSocket.CLOSED) current.close();
      cleanup.socketClosed = true;
    }};
    const submitAndWait = async (sessionId, prompt) => {{
      await request('prompt.submit', {{ session_id: sessionId, text: prompt }});
      let completion = null;
      while (!completion) {{
        const event = await waitAnyEvent(['message.delta', 'message.complete'], sessionId);
        if (event.type === 'message.complete') completion = event.payload || {{}};
      }}
      if (completion.status !== 'complete') throw new Error('model response did not complete');
      return String(completion.text || '');
    }};

    try {{
      if (config.phase === 'prepare') {{
        const started = now();
        await connectUntilReady();
        const created = await request('session.create', {{
          browser_id: state.browserId,
          close_on_disconnect: false,
          source: 'dashboard-gui',
          switch_generation: 1,
        }});
        state.liveSessionId = String(created.session_id || '');
        state.storedSessionId = String(created.stored_session_id || '');
        if (!state.liveSessionId || !state.storedSessionId) throw new Error('session.create omitted an ID');
        await waitEvent('session.info', state.liveSessionId);
        const response = await submitAndWait(
          state.liveSessionId,
          `Release continuity setup. Reply briefly and include this marker exactly: ${{state.marker}}`,
        );
        if (!response.includes(state.marker)) throw new Error('model response omitted continuity marker');
        state.closeCode = null;
        socket.addEventListener('close', (event) => {{ state.closeCode = event.code; }}, {{ once: true }});
        pass('continuity_prepare', started);
        return {{ ok: true, checks, cleanup }};
      }}

      const closeStarted = now();
      activeCheck = 'continuity_planned_restart';
      const oldSocket = state.socket;
      if (!oldSocket) throw new Error('continuity preparation state is missing');
      if (state.closeCode === null && oldSocket.readyState !== WebSocket.CLOSED) {{
        await withTimeout(new Promise((resolve) =>
          oldSocket.addEventListener('close', resolve, {{ once: true }})), 'planned restart close');
      }}
      if (state.closeCode !== 1012) throw new Error('planned restart did not close with code 1012');
      cleanup.socketClosed = true;
      state.socket = null;
      pass('continuity_planned_restart', closeStarted, {{ closeCode: 1012 }});

      const reconnectStarted = now();
      deadline = now() + config.timeoutMs;
      await connectUntilReady();
      const resumed = await request('session.resume', {{
        browser_id: state.browserId,
        close_on_disconnect: false,
        source: 'dashboard-gui',
        switch_generation: 2,
        session_id: state.storedSessionId,
      }});
      state.liveSessionId = String(resumed.session_id || '');
      if (!state.liveSessionId || !JSON.stringify(resumed.messages || []).includes(state.marker)) {{
        throw new Error('continuity resume did not restore the prepared transcript');
      }}
      pass('continuity_reconnect', reconnectStarted);

      const continueStarted = now();
      activeCheck = 'continuity_continue';
      await submitAndWait(state.liveSessionId, 'Continue the release continuity check with a brief acknowledgement.');
      pass('continuity_continue', continueStarted);

      const coldStarted = now();
      activeCheck = 'continuity_cold_resume';
      const closed = await request('session.close', {{ session_id: state.liveSessionId }});
      if (closed.closed !== true) throw new Error('session.close did not close continuity session');
      cleanup.sessionClosed = true;
      state.liveSessionId = '';
      await closeSocket();
      await connectUntilReady();
      const cold = await request('session.resume', {{
        browser_id: state.browserId,
        close_on_disconnect: false,
        source: 'dashboard-gui',
        switch_generation: 3,
        session_id: state.storedSessionId,
      }});
      state.liveSessionId = String(cold.session_id || '');
      if (!state.liveSessionId || !JSON.stringify(cold.messages || []).includes(state.marker)) {{
        throw new Error('continuity cold resume did not restore the prepared transcript');
      }}
      pass('continuity_cold_resume', coldStarted);

      const cleanupStarted = now();
      activeCheck = 'continuity_cleanup';
      await request('session.close', {{ session_id: state.liveSessionId }});
      cleanup.sessionClosed = true;
      state.liveSessionId = '';
      const deleted = await request('session.delete', {{ session_id: state.storedSessionId }});
      if (deleted.deleted !== state.storedSessionId) throw new Error('session.delete did not remove continuity session');
      cleanup.sessionDeleted = true;
      state.storedSessionId = '';
      await closeSocket();
      delete window[config.stateKey];
      pass('continuity_cleanup', cleanupStarted);
      return {{ ok: true, checks, cleanup }};
    }} catch (error) {{
      return {{
        ok: false,
        checks,
        cleanup,
        failure: {{
          code: String(error && error.message || '').includes('timed out') ? 'timeout' : 'continuity_smoke_failed',
          check: activeCheck,
          message: String(error && error.message || 'continuity smoke failed').slice(0, 500),
        }},
      }};
    }}
  }}, config);
}}"""


def run_public_smoke(
    *,
    repo_root: Path,
    raw_url: str,
    session: str,
    playwright_cli: str | None,
    timeout: float,
) -> tuple[dict[str, Any], int]:
    started = time.monotonic()
    cli = playwright_cli or shutil.which("playwright-cli")
    credentials: Credentials | None = None
    failure: dict[str, str] | None = None
    checks: list[dict[str, Any]] = []
    cleanup: dict[str, bool] = {
        "sessionClosed": False,
        "sessionDeleted": False,
        "socketClosed": False,
        "browserClosed": False,
    }

    try:
        if not cli:
            raise LoginError("playwright-cli is not installed or is not available on PATH.")
        session = validate_session_name(session)
        urls = normalize_dashboard_url(raw_url)
        credentials = load_credentials(repo_root)
        login_started = time.monotonic()
        login_dashboard(
            repo_root=repo_root,
            raw_url=raw_url,
            session=session,
            playwright_cli=cli,
            credentials=credentials,
        )
        checks.append(
            {
                "name": "public_login",
                "status": "passed",
                "durationMs": round((time.monotonic() - login_started) * 1000),
            }
        )
        marker = f"hermes-release-smoke-{secrets.token_hex(6)}"
        output = run_secure_playwright_code(
            playwright_cli=cli,
            session=session,
            javascript=_smoke_javascript(
                base=urls.base,
                path_prefix=urls.path_prefix,
                marker=marker,
                timeout_ms=max(10_000, round(timeout * 1000)),
            ),
            credentials=credentials,
            timeout=timeout + 30,
            prefix="hermes-dashboard-smoke-",
        )
        if output.startswith("### Error"):
            raise LoginError("playwright-cli public smoke failed.")
        try:
            browser_result = json.loads(output)
        except json.JSONDecodeError as exc:
            raise LoginError("playwright-cli returned an invalid public smoke result.") from exc
        if not isinstance(browser_result, dict):
            raise LoginError("playwright-cli returned an invalid public smoke result.")
        browser_checks = browser_result.get("checks")
        if isinstance(browser_checks, list):
            checks.extend(item for item in browser_checks if isinstance(item, dict))
        browser_cleanup = browser_result.get("cleanup")
        if isinstance(browser_cleanup, dict):
            cleanup.update({key: bool(value) for key, value in browser_cleanup.items() if key in cleanup})
        if browser_result.get("ok") is not True:
            reported = browser_result.get("failure") or {}
            failure = {
                "code": _bounded(reported.get("code") or "public_smoke_failed", 80),
                "check": _bounded(reported.get("check") or "public_conversation", 80),
                "message": _bounded(
                    _redact(
                        str(reported.get("message") or "Public conversation smoke failed."),
                        credentials,
                    )
                ),
            }
    except LoginError as exc:
        failure = {
            "code": "browser_auth_or_runtime_failed",
            "check": "public_login" if not checks else "public_conversation",
            "message": _bounded(_redact(str(exc), credentials)),
        }
    except Exception as exc:
        failure = {
            "code": "unexpected_error",
            "check": "runner",
            "message": f"{type(exc).__name__}: {_bounded(_redact(str(exc), credentials))}",
        }
    finally:
        if cli:
            subprocess.run(
                [cli, f"-s={session}", "close"],
                capture_output=True,
                text=True,
                check=False,
            )
            cleanup["browserClosed"] = True

    result: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": KIND,
        "status": "failed" if failure else "passed",
        "checks": checks,
        "cleanup": cleanup,
        "durationMs": round((time.monotonic() - started) * 1000),
    }
    if failure:
        result["failure"] = failure
    return result, 1 if failure else 0


def run_continuity_smoke(
    *,
    repo_root: Path,
    raw_url: str,
    session: str,
    playwright_cli: str | None,
    timeout: float,
    phase: str,
) -> tuple[dict[str, Any], int]:
    started = time.monotonic()
    cli = playwright_cli or shutil.which("playwright-cli")
    credentials: Credentials | None = None
    checks: list[dict[str, Any]] = []
    cleanup = {
        "sessionClosed": False,
        "sessionDeleted": False,
        "socketClosed": False,
        "browserClosed": False,
    }
    failure: dict[str, str] | None = None

    try:
        if not cli:
            raise LoginError("playwright-cli is not installed or is not available on PATH.")
        session = validate_session_name(session)
        urls = normalize_dashboard_url(raw_url)
        credentials = load_credentials(repo_root)
        if phase == "prepare":
            login_started = time.monotonic()
            login_dashboard(
                repo_root=repo_root,
                raw_url=raw_url,
                session=session,
                playwright_cli=cli,
                credentials=credentials,
            )
            checks.append(
                {
                    "name": "continuity_login",
                    "status": "passed",
                    "durationMs": round((time.monotonic() - login_started) * 1000),
                }
            )
        marker = f"hermes-release-continuity-{secrets.token_hex(6)}"
        output = run_secure_playwright_code(
            playwright_cli=cli,
            session=session,
            javascript=_continuity_javascript(
                base=urls.base,
                path_prefix=urls.path_prefix,
                marker=marker,
                timeout_ms=max(10_000, round(timeout * 1000)),
                phase=phase,
            ),
            credentials=credentials,
            timeout=timeout + 30,
            prefix="hermes-dashboard-continuity-",
        )
        if output.startswith("### Error"):
            raise LoginError("playwright-cli continuity smoke failed.")
        try:
            browser_result = json.loads(output)
        except json.JSONDecodeError as exc:
            raise LoginError("playwright-cli returned an invalid continuity result.") from exc
        if not isinstance(browser_result, dict):
            raise LoginError("playwright-cli returned an invalid continuity result.")
        browser_checks = browser_result.get("checks")
        if isinstance(browser_checks, list):
            checks.extend(item for item in browser_checks if isinstance(item, dict))
        browser_cleanup = browser_result.get("cleanup")
        if isinstance(browser_cleanup, dict):
            cleanup.update(
                {key: bool(value) for key, value in browser_cleanup.items() if key in cleanup}
            )
        if browser_result.get("ok") is not True:
            reported = browser_result.get("failure") or {}
            failure = {
                "code": _bounded(reported.get("code") or "continuity_smoke_failed", 80),
                "check": _bounded(reported.get("check") or f"continuity_{phase}", 80),
                "message": _bounded(
                    _redact(
                        str(reported.get("message") or "Continuity smoke failed."),
                        credentials,
                    )
                ),
            }
    except LoginError as exc:
        failure = {
            "code": "browser_auth_or_runtime_failed",
            "check": "continuity_login" if phase == "prepare" and not checks else f"continuity_{phase}",
            "message": _bounded(_redact(str(exc), credentials)),
        }
    except Exception as exc:
        failure = {
            "code": "unexpected_error",
            "check": f"continuity_{phase}",
            "message": f"{type(exc).__name__}: {_bounded(_redact(str(exc), credentials))}",
        }
    finally:
        if cli and (phase == "verify" or failure):
            subprocess.run(
                [cli, f"-s={session}", "close"],
                capture_output=True,
                text=True,
                check=False,
            )
            cleanup["browserClosed"] = True

    result: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": CONTINUITY_KIND,
        "phase": phase,
        "status": "failed" if failure else "passed",
        "checks": checks,
        "cleanup": cleanup,
        "durationMs": round((time.monotonic() - started) * 1000),
    }
    if failure:
        result["failure"] = failure
    return result, 1 if failure else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help=f"dashboard base URL (default: {DEFAULT_URL})")
    parser.add_argument("--session", default=DEFAULT_SESSION)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--continuity-phase", choices=("prepare", "verify"))
    parser.add_argument("--playwright-cli", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent
    runner = run_continuity_smoke if args.continuity_phase else run_public_smoke
    kwargs: dict[str, Any] = {
        "repo_root": repo_root,
        "raw_url": args.url,
        "session": args.session,
        "playwright_cli": args.playwright_cli,
        "timeout": max(10.0, args.timeout),
    }
    if args.continuity_phase:
        kwargs["phase"] = args.continuity_phase
    result, status = runner(**kwargs)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
