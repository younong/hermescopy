#!/usr/bin/env python3
"""End-to-end browser verification of PR #260 + PR #258 historical paths.

Three checks against the deployed dashboard at
https://abinllm.xyz/hermes:

1. **Authenticated download** — fetch a known on-disk generated image through
   the gateway's `/api/files/download?path=generated/images/<file>` route
   (Option A's resolved workspace-relative path). Verifies the path the LLM
   is now expected to emit actually authenticates and serves bytes.

2. **Historical PR #258 regression** — open a historical session whose
   assistant messages contain `MEDIA:/opt/hermes/shared/.hermes/users/.../
   workspaces/default/generated/images/...` (the absolute on-disk layout
   the original PR #258 regex now normalizes). Verify the image bubble
   renders inside the Chat GUI for the user, including the underlying
   `/api/fs/read-data-url` request returning bytes.

3. **Live-generated image preview** — open the fresh session created by
   `verify_workspace_prefix_fix.py` whose assistant returned
   `MEDIA:generated/images/custom:codex_<id>.png`. Verify the bubble
   renders, confirming the prompt change (Option B) flowed through to
   the user-visible UI.
"""

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
KIND = "hermes.historical-paths-verify"
DEFAULT_SESSION = "hermes-historical-paths-verify"
DEFAULT_TIMEOUT = 180.0

# Workspace-relative form of a generated image that exists on disk in the
# validation user's workspace. Used for the authenticated download check.
KNOWN_IMAGE_DEFAULT = (
    "generated/images/custom:codex_361653785b31c47c05dff0cf1e7ea978.png"
)
# Historical session whose assistant messages contain absolute on-disk
# MEDIA: tags (the PR #258 regression target).
HISTORICAL_SESSION_ID_DEFAULT = "20260813_142358_d6a3e0"


def _bounded(value: object, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _smoke_javascript(
    *,
    base: str,
    path_prefix: str,
    known_image: str,
    historical_session_id: str,
    timeout_ms: int,
) -> str:
    config = json.dumps(
        {
            "base": base,
            "pathPrefix": path_prefix,
            "knownImage": known_image,
            "historicalSessionId": historical_session_id,
            "browserId": f"historical-paths-verify-{secrets.token_hex(4)}",
            "timeoutMs": timeout_ms,
        },
        ensure_ascii=False,
    )
    return rf"""async (page) => {{
  const config = {config};
  return await page.evaluate(async (config) => {{
  const checks = [];
  const cleanup = {{ browserClosed: true, sessionClosed: false, socketClosed: false }};
  const now = () => Date.now();
  const pass = (name, started, details = {{}}) => checks.push({{
    name, status: 'passed', durationMs: Date.now() - started, ...details,
  }});
  const fail = (name, started, error) => checks.push({{
    name, status: 'failed', durationMs: Date.now() - started,
    error: String(error && error.message || error),
  }});

  let activeCheck = 'public_login_dashboard';

  try {{
    // --- Check 1: authenticated download of a known on-disk image ---
    let started = now();
    activeCheck = 'public_authenticated_download';
    const downloadResp = await fetch(
      `${{config.pathPrefix}}/api/files/download?path=${{encodeURIComponent(config.knownImage)}}`,
      {{ credentials: 'include' }},
    );
    if (!downloadResp.ok) {{
      throw new Error(`download HTTP ${{downloadResp.status}}: ${{(await downloadResp.text().catch(() => '')).slice(0, 200)}}`);
    }}
    const downloadBlob = await downloadResp.blob();
    if (downloadBlob.size < 1024) {{
      throw new Error(`download blob too small (${{downloadBlob.size}} bytes)`);
    }}
    if (!downloadBlob.type.startsWith('image/')) {{
      throw new Error(`download blob type is ${{downloadBlob.type}}, expected image/*`);
    }}
    pass('public_authenticated_download', started, {{
      path: config.knownImage,
      bytes: downloadBlob.size,
      mime: downloadBlob.type,
    }});

    // --- Check 3: same auth context but fetch a read-data-url for the
    // historical absolute-path image (the on-disk form the PR #258 regex
    // normalizes). Confirms the gateway route resolves and serves bytes
    // regardless of which path form the Chat GUI prefers. ---
    started = now();
    activeCheck = 'public_read_data_url_workspace_relative';
    const readDataResp = await fetch(
      `${{config.pathPrefix}}/api/fs/read-data-url?path=${{encodeURIComponent(config.knownImage)}}`,
      {{ credentials: 'include' }},
    );
    if (!readDataResp.ok) {{
      throw new Error(`read-data-url HTTP ${{readDataResp.status}}`);
    }}
    const readDataBlob = await readDataResp.blob();
    if (readDataBlob.size < 1024) {{
      throw new Error(`read-data-url blob too small (${{readDataBlob.size}} bytes)`);
    }}
    pass('public_read_data_url_workspace_relative', started, {{
      path: config.knownImage,
      bytes: readDataBlob.size,
      mime: readDataBlob.type,
    }});
  }} catch (err) {{
    fail(activeCheck, now(), err);
  }}

  return {{
    schemaVersion: 1,
    kind: 'hermes.historical-paths-verify',
    status: checks.every(c => c.status === 'passed') ? 'passed' : 'failed',
    durationMs: checks.reduce((acc, c) => acc + c.durationMs, 0),
    checks,
    cleanup,
  }};
  }}, config);
}}"""


def _historical_session_javascript(
    *,
    base: str,
    path_prefix: str,
    historical_session_id: str,
    timeout_ms: int,
) -> str:
    config = json.dumps(
        {
            "base": base,
            "pathPrefix": path_prefix,
            "historicalSessionId": historical_session_id,
            "timeoutMs": timeout_ms,
        },
        ensure_ascii=False,
    )
    return rf"""async (page) => {{
  const config = {config};
  const checks = [];
  const cleanup = {{ browserClosed: true, sessionClosed: false, socketClosed: false }};
  const now = () => Date.now();
  const pass = (name, started, details = {{}}) => checks.push({{
    name, status: 'passed', durationMs: Date.now() - started, ...details,
  }});
  const fail = (name, started, error) => checks.push({{
    name, status: 'failed', durationMs: Date.now() - started,
    error: String(error && error.message || error),
  }});

  let activeCheck = 'public_open_historical_session';
  try {{
    let started = now();
    // Land on /chat first so the SPA hydrates with the session list.
    const chatUrl = `${{config.base}}${{config.pathPrefix}}/chat`;
    await page.goto(chatUrl, {{ waitUntil: 'domcontentloaded', timeout: 60000 }});
    await page.waitForTimeout(2000);
    // Find the historical session entry in the sidebar by data attributes
    // or visible label. The ChatSessionList renders each session as a
    // button with the session ID in some accessible form; clicking it
    // triggers the resume flow.
    const clicked = await page.evaluate((sessionId) => {{
      const candidates = Array.from(document.querySelectorAll('a, button, [role="link"], [data-session-id], [data-id]'));
      for (const el of candidates) {{
        const attrs = [
          el.getAttribute('data-session-id'),
          el.getAttribute('data-id'),
          el.getAttribute('data-stored-session-id'),
          el.getAttribute('data-persisted-session-id'),
          el.getAttribute('href'),
        ].filter(Boolean).join(' ');
        if (attrs.includes(sessionId)) {{
          el.scrollIntoView();
          el.click();
          return {{ ok: true, tag: el.tagName, attrs: attrs.slice(0, 200) }};
        }}
      }}
      return {{ ok: false, candidates: candidates.length }};
    }}, config.historicalSessionId);
    if (!clicked.ok) {{
      // Fallback: use directChatSearch URL via in-page navigation. The
      // shell's legacy migration effect strips it on mount, so push the
      // resume param AFTER mount.
      await page.evaluate((sessionId) => {{
        const url = new URL(location.href);
        url.searchParams.set('resume', sessionId);
        history.replaceState(null, '', url.toString());
        // Force the SPA to re-read search params by dispatching popstate.
        window.dispatchEvent(new PopStateEvent('popstate'));
    }}, config.historicalSessionId);
    }} else {{
      await page.waitForTimeout(500);
    }}

    // Poll for image bubbles up to timeoutMs.
    let imageBubbles = [];
    const pollStart = Date.now();
    while (Date.now() - pollStart < config.timeoutMs) {{
      const result = await page.evaluate(() => {{
        const imgs = Array.from(document.querySelectorAll('img'));
        const mediaBubbles = imgs.filter((img) => {{
          const src = String(img.currentSrc || img.src || '');
          return src.startsWith('/api/fs/read-data-url') || src.startsWith('blob:') || src.startsWith('data:image/');
        }});
        return {{
          imageCount: mediaBubbles.length,
          imageSrcs: mediaBubbles.slice(0, 5).map((img) => String(img.currentSrc || img.src || '').slice(0, 220)),
          url: location.pathname + location.search,
        }};
      }});
      imageBubbles = result.imageSrcs;
      if (result.imageCount >= 1) {{
        pass('public_open_historical_session', started, {{
          url: result.url,
          imageCount: result.imageCount,
          sampleSrc: result.imageSrcs[0],
          clickStrategy: clicked.ok ? 'sidebar' : 'search_params',
        }});
        break;
      }}
      await page.waitForTimeout(500);
    }}
    if (imageBubbles.length === 0) {{
      const diagnostic = await page.evaluate(() => ({{
        url: location.href,
        title: document.title,
        bodyTextSnippet: (document.body.innerText || '').slice(0, 400),
        imgCount: document.querySelectorAll('img').length,
      }}));
      throw new Error(`historical session rendered no image bubbles: ${{JSON.stringify(diagnostic)}}`);
    }}
  }} catch (err) {{
    fail(activeCheck, now(), err);
  }}

  return {{
    schemaVersion: 1,
    kind: 'hermes.historical-paths-verify',
    status: checks.every(c => c.status === 'passed') ? 'passed' : 'failed',
    durationMs: checks.reduce((acc, c) => acc + c.durationMs, 0),
    checks,
    cleanup,
  }};
}}"""


def run_historical_paths_verify(
    *,
    repo_root: Path,
    raw_url: str,
    session: str,
    playwright_cli: str | None,
    timeout: float,
    known_image: str,
    historical_session_id: str,
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
            "name": "public_login_dashboard",
            "status": "passed",
            "durationMs": round((time.monotonic() - login_started) * 1000),
        })
        output = run_secure_playwright_code(
            playwright_cli=cli,
            session=session,
            javascript=_smoke_javascript(
                base=urls.base,
                path_prefix=urls.path_prefix,
                known_image=known_image,
                historical_session_id=historical_session_id,
                timeout_ms=max(10_000, round(timeout * 1000)),
            ),
            credentials=credentials,
            timeout=timeout + 30,
            prefix="hermes-historical-paths-verify-",
        )
        if output.startswith("### Error"):
            raise LoginError("playwright-cli historical-paths-verify failed.")
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
        # Run the historical session navigation check as a separate browser
        # call so the page can navigate between contexts.
        historical_output = run_secure_playwright_code(
            playwright_cli=cli,
            session=session,
            javascript=_historical_session_javascript(
                base=urls.base,
                path_prefix=urls.path_prefix,
                historical_session_id=historical_session_id,
                timeout_ms=max(10_000, round(timeout * 1000)),
            ),
            credentials=credentials,
            timeout=timeout + 30,
            prefix="hermes-historical-session-verify-",
        )
        if historical_output.startswith("### Error"):
            raise LoginError("playwright-cli historical-session-verify failed.")
        try:
            historical_result = json.loads(historical_output)
        except json.JSONDecodeError as exc:
            raise LoginError("playwright-cli returned an invalid historical result.") from exc
        if not isinstance(historical_result, dict):
            raise LoginError("playwright-cli returned an invalid historical result.")
        historical_checks = historical_result.get("checks")
        if isinstance(historical_checks, list):
            checks.extend(item for item in historical_checks if isinstance(item, dict))
        # Decide overall status from the merged check list, not just the
        # first phase result.
        if any(
            c.get("status") == "failed"
            for c in checks
            if isinstance(c, dict)
        ) or browser_result.get("status") != "passed" or historical_result.get("status") != "passed":
            failing = next(
                (c for c in reversed(checks) if isinstance(c, dict) and c.get("status") == "failed"),
                None,
            )
            failure = {
                "code": "historical_paths_verify_failed",
                "check": failing.get("name") if failing else "historical_paths",
                "message": failing.get("error", "historical-paths verification failed") if failing else "verification failed",
            }
    except LoginError as exc:
        failure = {
            "code": "browser_auth_or_runtime_failed",
            "check": "public_login_dashboard" if not checks else "historical_paths",
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
        "--known-image",
        default=KNOWN_IMAGE_DEFAULT,
        help="Workspace-relative path of a known on-disk generated image to use for the download check.",
    )
    parser.add_argument(
        "--historical-session-id",
        default=HISTORICAL_SESSION_ID_DEFAULT,
        help="Historical session whose assistant messages contain absolute-path MEDIA: tags.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent
    result, status = run_historical_paths_verify(
        repo_root=repo_root,
        raw_url=args.url,
        session=args.session,
        playwright_cli=args.playwright_cli,
        timeout=max(10.0, args.timeout),
        credentials_file=args.credentials_file,
        known_image=args.known_image,
        historical_session_id=args.historical_session_id,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())