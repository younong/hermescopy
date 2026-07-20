#!/usr/bin/env python3
"""Log a Playwright CLI session into the Hermes dashboard without exposing secrets."""

from __future__ import annotations

import argparse
import io
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from urllib.parse import unquote, urlsplit, urlunsplit

DEFAULT_URL = "https://abinllm.xyz/hermes/"
DEFAULT_SESSION = "hermes-validation"
CREDENTIALS_FILENAME = ".env.local"
USERNAME_KEY = "HERMES_DASHBOARD_BROWSER_USERNAME"
PASSWORD_KEY = "HERMES_DASHBOARD_BROWSER_PASSWORD"
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class LoginError(RuntimeError):
    """A safe-to-display browser login failure."""


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str


@dataclass(frozen=True)
class DashboardUrls:
    base: str
    login: str
    auth_me: str
    origin: str
    path_prefix: str


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _verify_credentials_git_state(repo_root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(repo_root)
    except ValueError as exc:
        raise LoginError("Credential file must be inside the repository.") from exc

    tracked = _run_git(repo_root, "ls-files", "--error-unmatch", "--", str(relative))
    if tracked.returncode == 0:
        raise LoginError(f"Refusing to read tracked credential file {relative}.")
    if tracked.returncode not in (1,):
        raise LoginError("Could not verify that the credential file is untracked.")

    ignored = _run_git(repo_root, "check-ignore", "-q", "--", str(relative))
    if ignored.returncode == 1:
        raise LoginError(f"Credential file {relative} is not covered by .gitignore.")
    if ignored.returncode != 0:
        raise LoginError("Could not verify that the credential file is ignored by Git.")


def _read_credentials_file(path: Path) -> str:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)

    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise LoginError(
            f"Missing {path.name}; add {USERNAME_KEY} and {PASSWORD_KEY}, then chmod 600 {path.name}."
        ) from exc
    except OSError as exc:
        raise LoginError(f"Cannot safely open {path.name}: {exc.strerror or 'open failed'}.") from exc

    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise LoginError(f"Credential path {path.name} must be a regular file.")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise LoginError(f"Credential file {path.name} must be owned by the current user.")
        if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600:
            raise LoginError(f"Credential file {path.name} must have permissions 0600.")

        with os.fdopen(fd, "r", encoding="utf-8", errors="strict") as handle:
            fd = -1
            return handle.read()
    except UnicodeDecodeError as exc:
        raise LoginError(f"Credential file {path.name} must be UTF-8 text.") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _parse_dotenv_value(raw: str, *, line_number: int) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        quote = value[0]
        if len(value) < 2 or value[-1] != quote:
            raise LoginError(f"Credential file has an unterminated quote on line {line_number}.")
        return value[1:-1]
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def _parse_credentials(raw: str) -> dict[str, str]:
    allowed = {USERNAME_KEY, PASSWORD_KEY}
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(io.StringIO(raw), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise LoginError(f"Credential file has an invalid assignment on line {line_number}.")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key not in allowed:
            raise LoginError(f"Credential file contains unsupported key {key!r}.")
        if key in values:
            raise LoginError(f"Credential file contains duplicate key {key}.")
        values[key] = _parse_dotenv_value(raw_value, line_number=line_number)
    return values


def load_credentials(repo_root: Path) -> Credentials:
    path = repo_root / CREDENTIALS_FILENAME
    _verify_credentials_git_state(repo_root, path)
    raw = _read_credentials_file(path)
    values = _parse_credentials(raw)

    missing = [key for key in (USERNAME_KEY, PASSWORD_KEY) if not values.get(key, "").strip()]
    if missing:
        raise LoginError(f"Credential file {path.name} is missing non-empty key(s): {', '.join(missing)}.")

    return Credentials(username=values[USERNAME_KEY].strip(), password=values[PASSWORD_KEY])


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def normalize_dashboard_url(raw_url: str) -> DashboardUrls:
    try:
        parsed = urlsplit(raw_url.strip())
        port = parsed.port
    except ValueError as exc:
        raise LoginError("Dashboard URL has an invalid host or port.") from exc

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LoginError("Dashboard URL must be an absolute HTTP(S) URL.")
    if parsed.username is not None or parsed.password is not None:
        raise LoginError("Dashboard URL must not contain user information.")
    if parsed.query or parsed.fragment:
        raise LoginError("Dashboard URL must not contain a query or fragment.")
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
        raise LoginError("Remote dashboard URLs must use HTTPS; HTTP is allowed only for loopback hosts.")

    decoded_segments = unquote(parsed.path).split("/")
    if any(segment in {".", ".."} for segment in decoded_segments):
        raise LoginError("Dashboard URL path must not contain dot segments.")

    path_prefix = parsed.path or "/"
    if not path_prefix.startswith("/"):
        path_prefix = f"/{path_prefix}"
    if not path_prefix.endswith("/"):
        path_prefix = f"{path_prefix}/"

    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if port is not None:
        netloc = f"{netloc}:{port}"

    base = urlunsplit((parsed.scheme, netloc, path_prefix, "", ""))
    origin = urlunsplit((parsed.scheme, netloc, "", "", ""))
    return DashboardUrls(
        base=base,
        login=f"{base}login",
        auth_me=f"{base}api/auth/me",
        origin=origin,
        path_prefix=path_prefix,
    )


def validate_session_name(session: str) -> str:
    if not _SESSION_RE.fullmatch(session):
        raise LoginError("Session name must be 1-64 letters, digits, dots, underscores, or hyphens.")
    return session


def _login_javascript(urls: DashboardUrls, credentials: Credentials) -> str:
    config = json.dumps(
        {
            "loginUrl": urls.login,
            "authMeUrl": urls.auth_me,
            "expectedOrigin": urls.origin,
            "pathPrefix": urls.path_prefix,
            "loginPath": f"{urls.path_prefix}login",
            "username": credentials.username,
            "password": credentials.password,
        },
        ensure_ascii=False,
    )
    return f"""async (page) => {{
  const config = {config};
  await page.goto(config.loginUrl, {{ waitUntil: 'domcontentloaded' }});

  const currentLocation = await page.evaluate(() => ({{
    origin: window.location.origin,
    pathname: window.location.pathname,
  }}));
  if (currentLocation.origin !== config.expectedOrigin || !currentLocation.pathname.startsWith(config.pathPrefix)) {{
    throw new Error('Login redirected outside the configured dashboard origin or path prefix.');
  }}

  const form = page.locator('form.provider-form[data-provider="basic"]');
  await form.waitFor({{ state: 'visible', timeout: 15000 }});
  await form.locator('input[name="username"]').fill(config.username);
  await form.locator('input[name="password"]').fill(config.password);
  await form.locator('button[type="submit"]').click();

  await page.waitForFunction(
    (loginPath) => {{
      const error = document.querySelector('form.provider-form[data-provider="basic"] .form-error');
      return window.location.pathname !== loginPath || (error && !error.hidden);
    }},
    config.loginPath,
    {{ timeout: 15000 }}
  );

  const error = page.locator('form.provider-form[data-provider="basic"] .form-error');
  if (await error.count() && await error.isVisible()) {{
    throw new Error('Dashboard rejected the configured credentials.');
  }}

  const response = await page.request.get(config.authMeUrl);
  if (response.status() !== 200) {{
    throw new Error(`Authentication check failed with HTTP ${{response.status()}}.`);
  }}
  let identity;
  try {{
    identity = await response.json();
  }} catch (_) {{
    throw new Error('Authentication check returned invalid JSON.');
  }}
  if (!identity || !identity.user_id || identity.provider !== 'basic') {{
    throw new Error('Authentication check returned an unexpected identity.');
  }}

  return {{ ok: true, status: response.status(), provider: identity.provider, currentUrl: page.url() }};
}}
"""


def _redact(value: str, credentials: Credentials | None) -> str:
    if credentials is None:
        return value
    redacted = value
    for secret in (credentials.password, credentials.username):
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _run_playwright(
    playwright_cli: str,
    args: Sequence[str],
    *,
    credentials: Credentials | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    run_kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "check": False,
    }
    if timeout is not None:
        run_kwargs["timeout"] = timeout
    try:
        completed = subprocess.run([playwright_cli, *args], **run_kwargs)
    except subprocess.TimeoutExpired as exc:
        raise LoginError("playwright-cli command timed out.") from exc
    if completed.returncode != 0:
        detail = _redact((completed.stderr or completed.stdout).strip(), credentials)
        if len(detail) > 1200:
            detail = f"{detail[:1200]}..."
        suffix = f" {detail}" if detail else ""
        raise LoginError(f"playwright-cli command failed.{suffix}")
    return completed


def run_secure_playwright_code(
    *,
    playwright_cli: str,
    session: str,
    javascript: str,
    credentials: Credentials | None = None,
    timeout: float | None = None,
    prefix: str = "hermes-playwright-",
) -> str:
    """Run JavaScript from a mode-0600 temporary file, then remove it."""
    validate_session_name(session)
    fd, temporary_name = tempfile.mkstemp(prefix=prefix, suffix=".js")
    script_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(javascript)
        completed = _run_playwright(
            playwright_cli,
            [f"-s={session}", "--raw", "run-code", f"--filename={script_path}"],
            credentials=credentials,
            timeout=timeout,
        )
        return _redact(completed.stdout.strip(), credentials)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            script_path.unlink()
        except FileNotFoundError:
            pass



def login_dashboard(
    *,
    repo_root: Path,
    raw_url: str,
    session: str,
    playwright_cli: str | None = None,
    credentials: Credentials | None = None,
) -> dict[str, object]:
    session = validate_session_name(session)
    urls = normalize_dashboard_url(raw_url)
    credentials = credentials or load_credentials(repo_root)
    cli = playwright_cli or shutil.which("playwright-cli")
    if not cli:
        raise LoginError("playwright-cli is not installed or is not available on PATH.")

    # A stale session may contain an expired cookie or a half-filled password form.
    subprocess.run(
        [cli, f"-s={session}", "close"],
        capture_output=True,
        text=True,
        check=False,
    )

    opened = False
    try:
        _run_playwright(cli, [f"-s={session}", "open", "about:blank"], credentials=credentials)
        opened = True

        output = run_secure_playwright_code(
            playwright_cli=cli,
            session=session,
            javascript=_login_javascript(urls, credentials),
            credentials=credentials,
            prefix="hermes-dashboard-login-",
        )
        if output.startswith("### Error"):
            detail = output.removeprefix("### Error").strip()
            raise LoginError(f"playwright-cli login failed. {detail}" if detail else "playwright-cli login failed.")
        try:
            result = json.loads(output)
        except json.JSONDecodeError as exc:
            raise LoginError("playwright-cli returned an invalid login result.") from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise LoginError("playwright-cli did not confirm dashboard authentication.")
        return result
    except Exception:
        if opened:
            subprocess.run(
                [cli, f"-s={session}", "close"],
                capture_output=True,
                text=True,
                check=False,
            )
        raise


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Open an authenticated Hermes dashboard Playwright CLI session."
    )
    parser.add_argument("--url", default=DEFAULT_URL, help=f"dashboard base URL (default: {DEFAULT_URL})")
    parser.add_argument(
        "--session",
        default=DEFAULT_SESSION,
        help=f"Playwright CLI session to create (default: {DEFAULT_SESSION})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent
    try:
        result = login_dashboard(repo_root=repo_root, raw_url=args.url, session=args.session)
    except LoginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("error: browser authentication failed unexpectedly.", file=sys.stderr)
        return 1

    print(
        f"Authenticated Playwright session '{args.session}' "
        f"(provider={result['provider']}, status={result['status']})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
