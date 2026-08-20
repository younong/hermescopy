#!/usr/bin/env python3
"""End-to-end browser verification of PR #260: normalize /workspace/ prefix on
assistant-generated MEDIA: tags.

Boots a real conversation via the dashboard WebSocket, asks the assistant to
generate an image, and asserts the assistant's reply contains a `MEDIA:`
reference whose path is workspace-relative (no `/workspace/` prefix). This
catches Option B regression: a renewed prompt that still allows the LLM to
narrate `MEDIA:/workspace/generated/images/...`.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright_dashboard_login import (  # noqa: E402
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
KIND = "hermes.workspace-prefix-verify"
DEFAULT_SESSION = "hermes-workspace-prefix-verify"
DEFAULT_TIMEOUT = 240.0


def _bounded(value: object, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _smoke_javascript(
    *,
    base: str,
    path_prefix: str,
    prompt: str,
    timeout_ms: int,
) -> str:
    config = json.dumps(
        {
            "base": base,
            "pathPrefix": path_prefix,
            "prompt": prompt,
            "browserId": f"workspace-prefix-verify-{secrets.token_hex(4)}",
            "timeoutMs": timeout_ms,
        },
        ensure_ascii=False,
    )
    return rf"""async (page) => {{
  const config = {config};
  return await page.evaluate(async (config) => {{
  const checks = [];
  const cleanup = {{ sessionClosed: false, sessionDeleted: false, socketClosed: false }};
  const now = () => Date.now();
  const pass = (name, started, details = {{}}) => checks.push({{
    name, status: 'passed', durationMs: Date.now() - started, ...details,
  }});
  const fail = (name, started, error) => checks.push({{
    name, status: 'failed', durationMs: Date.now() - started,
    error: String(error && error.message || error),
  }});
  const withTimeout = (promise, label, ms) => new Promise((resolve, reject) => {{
    const timer = setTimeout(() => reject(new Error(`${{label}} timed out`)), ms || config.timeoutMs);
    Promise.resolve(promise).then((v) => {{ clearTimeout(timer); resolve(v); }}).catch((e) => {{
      clearTimeout(timer); reject(e);
    }});
  }});

  let events = [];
  const pending = new Map();
  let requestId = 0;
  let socket = null;
  let liveSessionId = '';

  const baseParams = (generation) => ({{
    browser_id: config.browserId,
    close_on_disconnect: false,
    source: 'dashboard-gui',
    switch_generation: generation,
  }});

  const request = (method, params, ms = config.timeoutMs) => {{
    if (!socket || socket.readyState !== WebSocket.OPEN) {{
      return Promise.reject(new Error(`socket unavailable for ${{method}}`));
    }}
    const id = `workspace-prefix-verify-${{++requestId}}`;
    return withTimeout(new Promise((resolve, reject) => {{
      pending.set(id, {{ resolve, reject }});
      socket.send(JSON.stringify({{ jsonrpc: '2.0', id, method, params }}));
    }}), `RPC ${{method}}`, ms).finally(() => pending.delete(id));
  }};

  const waitAnyEvent = async (types, sessionId = '') => {{
    const find = () => events.findIndex((event) =>
      types.includes(event.type) && (!sessionId || event.session_id === sessionId));
    const existing = find();
    if (existing >= 0) return events.splice(existing, 1)[0];
    return await withTimeout(new Promise((resolve) => {{
      const poll = setInterval(() => {{
        const index = find();
        if (index >= 0) {{
          clearInterval(poll);
          resolve(events.splice(index, 1)[0]);
        }}
      }}, 20);
    }}), `event ${{types.join('|')}}`);
  }};
  const waitEvent = (type, sessionId = '') => waitAnyEvent([type], sessionId);

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
    if (!payload || typeof payload.ticket !== 'string') {{
      throw new Error('ticket payload missing ticket');
    }}
    const endpoint = new URL(config.base);
    endpoint.protocol = endpoint.protocol === 'https:' ? 'wss:' : 'ws:';
    endpoint.pathname = `${{config.pathPrefix.replace(/\/$/, '')}}/api/ws`;
    endpoint.search = `?ticket=${{encodeURIComponent(payload.ticket)}}`;
    const ws = new WebSocket(endpoint.toString());
    socket = ws;
    pending.clear();
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

  let activeCheck = 'public_submit_and_collect';

  try {{
    let started = now();
    await connect();
    pass('public_ws_ticket_mint', started);

    started = now();
    activeCheck = 'public_ws_admission';
    const ready = await waitEvent('gateway.ready');
    if (!ready) throw new Error('gateway.ready missing');
    pass('public_ws_admission', started, {{ path: `${{config.pathPrefix}}api/ws` }});

    started = now();
    activeCheck = 'public_session_create';
    const created = await request('session.create', baseParams(1));
    liveSessionId = String(created.session_id || '');
    if (!liveSessionId) throw new Error('session.create did not return session_id');
    await waitEvent('session.info', liveSessionId);
    pass('public_session_create', started);

    started = now();
    activeCheck = 'public_prompt_complete';
    await request('prompt.submit', {{
      session_id: liveSessionId,
      text: config.prompt,
    }});
    let completed = null;
    let assistantText = '';
    while (!completed) {{
      const event = await waitAnyEvent(['message.delta', 'message.complete'], liveSessionId);
      if (event.type === 'message.delta') {{
        if (event.payload && typeof event.payload.text === 'string') {{
          assistantText += event.payload.text;
        }}
      }} else {{
        completed = event;
      }}
    }}
    if (completed.status !== 'complete' && completed.payload?.status !== 'complete') {{
      throw new Error(`model response did not stream to completion (status=${{completed.status || completed.payload?.status}})`);
    }}
    const completion = completed.payload || completed;
    const finalText = (completion.text || assistantText || '').toString();
    const mediaMatches = (finalText.match(/MEDIA:\s*(\S+)/gi) || []).map(s =>
      s.replace(/^MEDIA:\s*/i, '').replace(/[.,;:!?。，、；：！？]+$/g, ''));
    pass('public_prompt_complete', started, {{
      textLength: finalText.length,
      mediaMatches,
    }});

    started = now();
    activeCheck = 'public_media_format';
    if (mediaMatches.length === 0) {{
      throw new Error('no MEDIA: tag found in assistant response');
    }}
    const badPaths = mediaMatches.filter(p =>
      p.startsWith('/workspace/') || p.startsWith('/workspaces/'));
    if (badPaths.length > 0) {{
      throw new Error(`MEDIA: tag still has /workspace/ prefix: ${{JSON.stringify(badPaths)}}`);
    }}
    pass('public_media_format', started, {{
      mediaCount: mediaMatches.length,
      sample: mediaMatches[0],
    }});

    started = now();
    activeCheck = 'public_cleanup';
    const closed = await request('session.close', {{ session_id: liveSessionId }});
    if (closed.closed !== true) throw new Error('session.close did not close');
    cleanup.sessionClosed = true;
    liveSessionId = '';
    await closeSocket();
    pass('public_cleanup', started);
  }} catch (err) {{
    fail(activeCheck, now(), err);
    try {{ await closeSocket(); }} catch {{}}
  }}

  return {{
    schemaVersion: 1,
    kind: 'hermes.workspace-prefix-verify',
    status: checks.every(c => c.status === 'passed') ? 'passed' : 'failed',
    durationMs: checks.reduce((acc, c) => acc + c.durationMs, 0),
    checks,
    cleanup: {{ ...cleanup, browserClosed: true }},
  }};
  }}, config);
}}"""


def run_workspace_prefix_verify(
    *,
    repo_root: Path,
    raw_url: str,
    session: str,
    playwright_cli: str | None,
    timeout: float,
    prompt: str,
    credentials_file: str | Path = ".env.local",
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
        credentials = load_credentials(repo_root, credentials_file)
        login_started = time.monotonic()
        login_dashboard(
            repo_root=repo_root,
            raw_url=raw_url,
            session=session,
            playwright_cli=cli,
            credentials=credentials,
        )
        checks.append({
            "name": "public_login",
            "status": "passed",
            "durationMs": round((time.monotonic() - login_started) * 1000),
        })
        output = run_secure_playwright_code(
            playwright_cli=cli,
            session=session,
            javascript=_smoke_javascript(
                base=urls.base,
                path_prefix=urls.path_prefix,
                prompt=prompt,
                timeout_ms=max(10_000, round(timeout * 1000)),
            ),
            credentials=credentials,
            timeout=timeout + 30,
            prefix="hermes-workspace-prefix-verify-",
        )
        if output.startswith("### Error"):
            raise LoginError("playwright-cli workspace-prefix-verify failed.")
        try:
            browser_result = json.loads(output)
        except json.JSONDecodeError as exc:
            raise LoginError("playwright-cli returned an invalid result.") from exc
        if not isinstance(browser_result, dict):
            raise LoginError("playwright-cli returned an invalid result.")
        browser_checks = browser_result.get("checks")
        if isinstance(browser_checks, list):
            checks.extend(item for item in browser_checks if isinstance(item, dict))
        browser_cleanup = browser_result.get("cleanup")
        if isinstance(browser_cleanup, dict):
            cleanup.update({key: bool(value) for key, value in browser_cleanup.items() if key in cleanup})
        if browser_result.get("status") != "passed":
            failing = next(
                (c for c in reversed(checks) if isinstance(c, dict) and c.get("status") == "failed"),
                None,
            )
            failure = {
                "code": "workspace_prefix_verify_failed",
                "check": failing.get("name") if failing else "public_media_format",
                "message": failing.get("error", "workspace-prefix verification failed") if failing else "verification failed",
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--session", default=DEFAULT_SESSION)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--credentials-file", default=".env.local")
    parser.add_argument("--playwright-cli", help=argparse.SUPPRESS)
    parser.add_argument(
        "--prompt",
        default=(
            "Generate a small image of a red apple on a white background. "
            "When you finish, deliver it back to me using the MEDIA: file-delivery "
            "convention exactly as the tool description specifies. Reply in one short line."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent
    result, status = run_workspace_prefix_verify(
        repo_root=repo_root,
        raw_url=args.url,
        session=args.session,
        playwright_cli=args.playwright_cli,
        timeout=max(10.0, args.timeout),
        credentials_file=args.credentials_file,
        prompt=args.prompt,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
