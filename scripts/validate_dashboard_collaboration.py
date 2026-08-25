#!/usr/bin/env python3
"""Validate public Dashboard collaboration pagination without mutating group state."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from hermes_cli.history_pagination import DEFAULT_HISTORY_PAGE_SIZE
from hermes_cli.owner_worker.tokens import OWP1_MAX_MESSAGE_BYTES
from playwright_dashboard_login import (
    DEFAULT_URL,
    Credentials,
    LoginError,
    _redact,
    load_credentials,
    login_dashboard,
    normalize_dashboard_url,
    run_secure_playwright_code,
    validate_session_name,
)

SCHEMA_VERSION = 1
KIND = "hermes.dashboard-collaboration-validation"
DEFAULT_SESSION = "hermes-collaboration-validation"
DEFAULT_TIMEOUT = 120.0
DEFAULT_LIMIT = DEFAULT_HISTORY_PAGE_SIZE
MAX_MESSAGE_BYTES = OWP1_MAX_MESSAGE_BYTES


def _bounded(value: object, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _validate_group_id(group_id: str) -> str:
    if not group_id.startswith("cg_") or not group_id[3:].isalnum() or len(group_id) > 128:
        raise LoginError("Group ID must be a valid collaboration group durable ID.")
    return group_id


def _validation_javascript(
    *,
    base: str,
    path_prefix: str,
    group_id: str,
    timeout_ms: int,
    limit: int = DEFAULT_LIMIT,
) -> str:
    config = json.dumps(
        {
            "base": base,
            "pathPrefix": path_prefix,
            "groupId": group_id,
            "browserId": "collaboration-validation",
            "timeoutMs": timeout_ms,
            "limit": limit,
            "maxMessageBytes": MAX_MESSAGE_BYTES,
        },
        ensure_ascii=False,
    )
    return rf"""async (page) => {{
  const config = {config};
  return await page.evaluate(async (config) => {{
    const checks = [];
    let socket = null;
    let pending = new Map();
    let requestId = 0;
    let maxFrameBytes = 0;
    let closeCode = null;
    let activeCheck = 'public_login';
    const now = () => Date.now();
    const pass = (name, started, details = {{}}) => checks.push({{
      name, status: 'passed', durationMs: now() - started, ...details,
    }});
    const timeout = (label, ms = config.timeoutMs) => new Promise((_, reject) =>
      setTimeout(() => reject(new Error(`${{label}} timed out`)), ms));
    const withTimeout = (promise, label, ms = config.timeoutMs) =>
      Promise.race([promise, timeout(label, ms)]);

    const request = (method, params) => {{
      if (!socket || socket.readyState !== WebSocket.OPEN) {{
        return Promise.reject(new Error(`socket unavailable for ${{method}}`));
      }}
      const id = `collaboration-validation-${{++requestId}}`;
      return withTimeout(new Promise((resolve, reject) => {{
        pending.set(id, {{ resolve, reject }});
        socket.send(JSON.stringify({{ jsonrpc: '2.0', id, method, params }}));
      }}), `RPC ${{method}}`).finally(() => pending.delete(id));
    }};

    const connect = async () => {{
      const response = await fetch(new URL('api/auth/ws-ticket', config.base), {{
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
      const endpoint = new URL(config.base);
      endpoint.protocol = endpoint.protocol === 'https:' ? 'wss:' : 'ws:';
      endpoint.pathname = `${{config.pathPrefix.replace(/\/$/, '')}}/api/ws`;
      endpoint.search = `?ticket=${{encodeURIComponent(payload.ticket)}}`;
      const ws = new WebSocket(endpoint.toString());
      socket = ws;
      pending = new Map();
      ws.addEventListener('message', (message) => {{
        const raw = typeof message.data === 'string' ? message.data : '';
        maxFrameBytes = Math.max(maxFrameBytes, new TextEncoder().encode(raw).byteLength);
        let frame;
        try {{ frame = JSON.parse(raw); }} catch (_) {{ return; }}
        if (frame.id === undefined || frame.id === null) return;
        const call = pending.get(frame.id);
        if (!call) return;
        if (frame.error) call.reject(new Error(`RPC rejected with code ${{frame.error.code || 'error'}}`));
        else call.resolve(frame.result || {{}});
      }});
      ws.addEventListener('close', (event) => {{ closeCode = event.code; }});
      await withTimeout(new Promise((resolve, reject) => {{
        ws.addEventListener('open', resolve, {{ once: true }});
        ws.addEventListener('error', () => reject(new Error('WebSocket connection failed')), {{ once: true }});
      }}), 'WebSocket open', 15_000);
      await request('session.owner_attach', {{ browser_id: config.browserId }});
    }};

    const closeSocket = async () => {{
      if (!socket) return;
      const current = socket;
      socket = null;
      if (current.readyState === WebSocket.CLOSED) return;
      const closed = new Promise((resolve) => current.addEventListener('close', resolve, {{ once: true }}));
      current.close();
      await Promise.race([closed, new Promise((resolve) => setTimeout(resolve, 1500))]);
    }};

    const validatePage = (snapshot, expectedDirection) => {{
      if (!snapshot || !snapshot.group || snapshot.group.group_id !== config.groupId) {{
        throw new Error('group.get returned the wrong group');
      }}
      const page = snapshot.history_page;
      if (!page || page.direction !== expectedDirection) throw new Error('history page direction mismatch');
      const events = Array.isArray(snapshot.events) ? snapshot.events : [];
      if (events.length > config.limit) throw new Error('history page exceeded requested limit');
      let previous = null;
      const pageIds = new Set();
      for (const event of events) {{
        if (event.group_id !== config.groupId || !Number.isInteger(event.sequence) || event.sequence <= 0) {{
          throw new Error('history page contained an invalid event');
        }}
        if (previous !== null && event.sequence <= previous) throw new Error('history page is not strictly ordered');
        if (!event.event_id || pageIds.has(event.event_id)) throw new Error('history page contained duplicate events');
        previous = event.sequence;
        pageIds.add(event.event_id);
      }}
      if ((events[0]?.sequence ?? null) !== page.range_start_sequence
          || (events.at(-1)?.sequence ?? null) !== page.range_end_sequence) {{
        throw new Error('history page range metadata mismatch');
      }}
      for (const attachment of snapshot.attachments || []) {{
        if (attachment.group_id !== config.groupId || !pageIds.has(attachment.event_id)) {{
          throw new Error('attachment escaped the event page window');
        }}
      }}
      for (const membership of snapshot.memberships || []) {{
        if (membership.group_id !== config.groupId) throw new Error('membership escaped the requested group');
      }}
      for (const approval of snapshot.approvals || []) {{
        if (approval.group_id !== config.groupId) throw new Error('approval escaped the requested group');
      }}
      return {{ events, page }};
    }};

    const collectBackwardHistory = async (initial) => {{
      const seenSequences = new Set();
      const seenEventIds = new Set();
      let pageCount = 1;
      let loadEarlierCount = 0;
      let eventCount = 0;
      const accept = (events) => {{
        for (const event of events) {{
          if (seenSequences.has(event.sequence) || seenEventIds.has(event.event_id)) {{
            throw new Error('history pagination overlapped');
          }}
          seenSequences.add(event.sequence);
          seenEventIds.add(event.event_id);
          eventCount += 1;
        }}
      }};
      let current = validatePage(initial, 'initial');
      accept(current.events);
      const snapshotSequence = current.page.snapshot_sequence;
      let before = current.page.next_before_sequence;
      while (current.page.has_more) {{
        if (!Number.isInteger(before) || before <= 0) throw new Error('backward cursor did not advance');
        const response = await request('collaboration.group.get', {{
          group_id: config.groupId,
          before_sequence: before,
          limit: config.limit,
        }});
        const next = validatePage(response, 'backward');
        if (next.events.length && next.events.at(-1).sequence >= before) {{
          throw new Error('backward page crossed its exclusive cursor');
        }}
        accept(next.events);
        pageCount += 1;
        loadEarlierCount += 1;
        if (next.page.has_more && (!Number.isInteger(next.page.next_before_sequence)
            || next.page.next_before_sequence >= before)) {{
          throw new Error('backward cursor did not decrease');
        }}
        before = next.page.next_before_sequence;
        current = next;
      }}
      const ordered = [...seenSequences].sort((a, b) => a - b);
      if (ordered.length !== snapshotSequence) throw new Error('history did not cover the captured high-water');
      for (let index = 0; index < ordered.length; index += 1) {{
        if (ordered[index] !== index + 1) throw new Error('history pagination contained a sequence gap');
      }}
      return {{ eventCount, loadEarlierCount, pageCount, snapshotSequence }};
    }};

    const reconcileForward = async (after) => {{
      let through = null;
      let pages = 0;
      let events = 0;
      const seen = new Set();
      while (true) {{
        const params = {{ group_id: config.groupId, after_sequence: after, limit: config.limit }};
        if (through !== null) params.through_sequence = through;
        const response = await request('collaboration.group.get', params);
        const current = validatePage(response, 'forward');
        through ??= current.page.through_sequence;
        if (current.page.through_sequence !== through) throw new Error('forward high-water changed between pages');
        for (const event of current.events) {{
          if (event.sequence <= after || event.sequence > through || seen.has(event.sequence)) {{
            throw new Error('forward reconciliation returned an invalid sequence');
          }}
          seen.add(event.sequence);
          events += 1;
        }}
        pages += 1;
        if (!current.page.has_more) break;
        const next = current.page.next_after_sequence;
        if (!Number.isInteger(next) || next <= after) throw new Error('forward cursor did not advance');
        after = next;
      }}
      return {{ events, pages, throughSequence: through }};
    }};

    try {{
      let started = now();
      activeCheck = 'collaboration_ws_admission';
      await connect();
      pass('collaboration_ws_admission', started, {{ path: `${{config.pathPrefix}}api/ws` }});

      started = now();
      activeCheck = 'collaboration_latest_page';
      const initial = await request('collaboration.group.get', {{
        group_id: config.groupId,
        limit: config.limit,
      }});
      const latest = validatePage(initial, 'initial');
      pass('collaboration_latest_page', started, {{
        eventCount: latest.events.length,
        rangeStartSequence: latest.page.range_start_sequence,
        rangeEndSequence: latest.page.range_end_sequence,
        snapshotSequence: latest.page.snapshot_sequence,
      }});

      started = now();
      activeCheck = 'collaboration_backward_history';
      const history = await collectBackwardHistory(initial);
      pass('collaboration_backward_history', started, history);

      started = now();
      activeCheck = 'collaboration_reconnect';
      await closeSocket();
      closeCode = null;
      await connect();
      const forward = await reconcileForward(history.snapshotSequence);
      pass('collaboration_reconnect', started, forward);

      if (maxFrameBytes > config.maxMessageBytes) throw new Error('received frame exceeded Owner Worker message limit');
      await closeSocket();
      return {{
        ok: true,
        checks,
        cleanup: {{ socketClosed: true }},
        transport: {{ closeCode, maxFrameBytes, maxMessageBytes: config.maxMessageBytes }},
      }};
    }} catch (error) {{
      await closeSocket();
      return {{
        ok: false,
        checks,
        cleanup: {{ socketClosed: !socket || socket.readyState === WebSocket.CLOSED }},
        transport: {{ closeCode, maxFrameBytes, maxMessageBytes: config.maxMessageBytes }},
        failure: {{
          code: String(error && error.message || '').includes('timed out') ? 'timeout' : 'collaboration_validation_failed',
          check: activeCheck,
          message: String(error && error.message || 'collaboration validation failed').slice(0, 500),
        }},
      }};
    }}
  }}, config);
}}"""


def run_validation(
    *,
    repo_root: Path,
    raw_url: str,
    group_id: str,
    session: str,
    playwright_cli: str | None,
    timeout: float,
    credentials_file: str | Path = ".env.local",
) -> tuple[dict[str, Any], int]:
    started = time.monotonic()
    cli = playwright_cli or shutil.which("playwright-cli")
    credentials: Credentials | None = None
    checks: list[dict[str, Any]] = []
    cleanup = {"socketClosed": False, "browserClosed": False}
    transport: dict[str, Any] = {
        "closeCode": None,
        "maxFrameBytes": 0,
        "maxMessageBytes": MAX_MESSAGE_BYTES,
    }
    failure: dict[str, str] | None = None

    try:
        if not cli:
            raise LoginError("playwright-cli is not installed or is not available on PATH.")
        group_id = _validate_group_id(group_id)
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
        checks.append(
            {
                "name": "public_login",
                "status": "passed",
                "durationMs": round((time.monotonic() - login_started) * 1000),
            }
        )
        output = run_secure_playwright_code(
            playwright_cli=cli,
            session=session,
            javascript=_validation_javascript(
                base=urls.base,
                path_prefix=urls.path_prefix,
                group_id=group_id,
                timeout_ms=max(10_000, round(timeout * 1000)),
            ),
            credentials=credentials,
            timeout=timeout + 30,
            prefix="hermes-collaboration-validation-",
        )
        if output.startswith("### Error"):
            raise LoginError("playwright-cli collaboration validation failed.")
        try:
            browser_result = json.loads(output)
        except json.JSONDecodeError as exc:
            raise LoginError("playwright-cli returned an invalid collaboration validation result.") from exc
        if not isinstance(browser_result, dict):
            raise LoginError("playwright-cli returned an invalid collaboration validation result.")
        browser_checks = browser_result.get("checks")
        if isinstance(browser_checks, list):
            checks.extend(item for item in browser_checks if isinstance(item, dict))
        browser_cleanup = browser_result.get("cleanup")
        if isinstance(browser_cleanup, dict):
            cleanup["socketClosed"] = bool(browser_cleanup.get("socketClosed"))
        browser_transport = browser_result.get("transport")
        if isinstance(browser_transport, dict):
            transport.update(
                {
                    key: browser_transport[key]
                    for key in ("closeCode", "maxFrameBytes", "maxMessageBytes")
                    if key in browser_transport
                }
            )
        if browser_result.get("ok") is not True:
            reported = browser_result.get("failure") or {}
            failure = {
                "code": _bounded(reported.get("code") or "collaboration_validation_failed", 80),
                "check": _bounded(reported.get("check") or "collaboration", 80),
                "message": _bounded(
                    _redact(
                        str(reported.get("message") or "Collaboration validation failed."),
                        credentials,
                    )
                ),
            }
    except LoginError as exc:
        failure = {
            "code": "browser_auth_or_runtime_failed",
            "check": "public_login" if not checks else "collaboration",
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
        "groupId": group_id,
        "checks": checks,
        "transport": transport,
        "cleanup": cleanup,
        "durationMs": round((time.monotonic() - started) * 1000),
    }
    if failure:
        result["failure"] = failure
    return result, 1 if failure else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--url", default=DEFAULT_URL, help=f"dashboard base URL (default: {DEFAULT_URL})")
    parser.add_argument("--session", default=DEFAULT_SESSION)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--credentials-file", default=".env.local")
    parser.add_argument("--playwright-cli", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent
    result, status = run_validation(
        repo_root=repo_root,
        raw_url=args.url,
        group_id=args.group_id,
        session=args.session,
        playwright_cli=args.playwright_cli,
        timeout=max(10.0, args.timeout),
        credentials_file=args.credentials_file,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
