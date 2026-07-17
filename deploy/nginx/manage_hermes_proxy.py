#!/usr/bin/env python3
"""Install and reconcile the repository-owned Hermes Nginx location.

The site vhost remains operator/Certbot owned. This helper recognizes the one
legacy production shape, replaces only its Hermes locations with an include,
and refuses ambiguous or partially migrated input.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

DEFAULT_VHOST = Path("/etc/nginx/conf.d/abinllm.conf")
DEFAULT_SNIPPET = Path("/etc/nginx/snippets/hermes-dashboard.conf")
DEFAULT_INCLUDE = "include /etc/nginx/snippets/hermes-dashboard.conf;"

_LOCATION_RE = re.compile(r"^\s*location\s+(?P<target>[^\s{]+(?:\s+[^\s{]+)?)\s*\{")
_LEGACY_TARGETS = (
    "= /__hermes_remember_check",
    "= /hermes",
    "/hermes/api/",
    "/hermes/",
)


class ProxyConfigError(RuntimeError):
    pass


def _location_blocks(text: str) -> dict[str, tuple[int, int, str]]:
    """Return location target -> (start, end, block), rejecting duplicates."""
    lines = text.splitlines(keepends=True)
    blocks: dict[str, tuple[int, int, str]] = {}
    offset = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        match = _LOCATION_RE.match(line)
        if not match:
            offset += len(line)
            index += 1
            continue
        target = " ".join(match.group("target").split())
        start = offset
        depth = line.count("{") - line.count("}")
        end_index = index
        while depth > 0:
            end_index += 1
            if end_index >= len(lines):
                raise ProxyConfigError(f"unterminated Nginx location: {target}")
            depth += lines[end_index].count("{") - lines[end_index].count("}")
        end = offset + sum(len(item) for item in lines[index : end_index + 1])
        if target in blocks:
            raise ProxyConfigError(f"duplicate Nginx location: {target}")
        blocks[target] = (start, end, text[start:end])
        offset = end
        index = end_index + 1
    return blocks


def migration_status(text: str, *, include_line: str = DEFAULT_INCLUDE) -> str:
    include_count = text.count(include_line)
    blocks = _location_blocks(text)
    legacy = set(_LEGACY_TARGETS)
    hermes_targets = {
        target for target in blocks if target in legacy or "/hermes" in target
    }
    if include_count == 1 and not hermes_targets:
        return "current"
    if include_count:
        return "divergent"
    if hermes_targets == legacy and _legacy_blocks_match(blocks):
        return "migration-ready"
    return "unmanaged" if not hermes_targets else "divergent"


def _legacy_blocks_match(blocks: dict[str, tuple[int, int, str]]) -> bool:
    try:
        remember = blocks["= /__hermes_remember_check"][2]
        shell = blocks["/hermes/"][2]
        api = blocks["/hermes/api/"][2]
        redirect = blocks["= /hermes"][2]
    except KeyError:
        return False
    return all(
        (
            "hermes_remember" in remember,
            "auth_basic" in shell,
            "auth_request /__hermes_remember_check" in shell,
            ".htpasswd-hermes" in shell,
            "proxy_pass http://127.0.0.1:9119/" in shell,
            "proxy_pass http://127.0.0.1:9119/api/" in api,
            "auth_basic off" in api,
            "return 301 /hermes/" in redirect,
        )
    )


def migrate_text(text: str, *, include_line: str = DEFAULT_INCLUDE) -> str:
    if migration_status(text, include_line=include_line) != "migration-ready":
        raise ProxyConfigError("Nginx vhost is not the recognized legacy Hermes shape")
    blocks = _location_blocks(text)
    ranges = sorted((blocks[target][0], blocks[target][1]) for target in _LEGACY_TARGETS)
    insertion = ranges[0][0]
    result: list[str] = []
    cursor = 0
    inserted = False
    for start, end in ranges:
        result.append(text[cursor:start])
        if not inserted:
            result.append(f"    {include_line}\n")
            inserted = True
        cursor = end
    result.append(text[cursor:])
    return "".join(result)


def _atomic_write(path: Path, data: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _validate(nginx: str) -> None:
    subprocess.run([nginx, "-t"], check=True)


def _reload() -> None:
    subprocess.run(["systemctl", "reload", "nginx"], check=True)


def migrate(
    *, vhost: Path, snippet_source: Path, snippet_target: Path, nginx: str
) -> Path:
    original_vhost = vhost.read_text(encoding="utf-8")
    migrated = migrate_text(original_vhost)
    snippet = snippet_source.read_text(encoding="utf-8")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = vhost.with_name(f"{vhost.name}.hermes-backup-{stamp}")
    shutil.copy2(vhost, backup)
    old_snippet = snippet_target.read_bytes() if snippet_target.exists() else None
    snippet_mode = snippet_target.stat().st_mode & 0o777 if snippet_target.exists() else 0o644
    reload_attempted = False
    try:
        _atomic_write(snippet_target, snippet, mode=snippet_mode)
        _atomic_write(vhost, migrated, mode=vhost.stat().st_mode & 0o777)
        _validate(nginx)
        reload_attempted = True
        _reload()
    except Exception:
        shutil.copy2(backup, vhost)
        if old_snippet is None:
            snippet_target.unlink(missing_ok=True)
        else:
            _atomic_write(snippet_target, old_snippet.decode("utf-8"), mode=snippet_mode)
        try:
            _validate(nginx)
            if reload_attempted:
                _reload()
        except Exception as rollback_exc:
            raise ProxyConfigError(
                f"Nginx update failed and rollback reload failed: {rollback_exc}"
            ) from rollback_exc
        raise
    return backup


def reconcile(
    *, vhost: Path, snippet_source: Path, snippet_target: Path, nginx: str
) -> bool:
    text = vhost.read_text(encoding="utf-8")
    if migration_status(text) != "current":
        raise ProxyConfigError(
            "Hermes Nginx include is missing or divergent; run the explicit migration"
        )
    desired = snippet_source.read_text(encoding="utf-8")
    current = snippet_target.read_text(encoding="utf-8") if snippet_target.exists() else None
    if current == desired:
        return False
    old = current
    mode = snippet_target.stat().st_mode & 0o777 if snippet_target.exists() else 0o644
    reload_attempted = False
    try:
        _atomic_write(snippet_target, desired, mode=mode)
        _validate(nginx)
        reload_attempted = True
        _reload()
    except Exception:
        if old is None:
            snippet_target.unlink(missing_ok=True)
        else:
            _atomic_write(snippet_target, old, mode=mode)
        try:
            _validate(nginx)
            if reload_attempted:
                _reload()
        except Exception as rollback_exc:
            raise ProxyConfigError(
                f"Nginx update failed and rollback reload failed: {rollback_exc}"
            ) from rollback_exc
        raise
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("status", "migrate", "reconcile"))
    parser.add_argument("--vhost", type=Path, default=DEFAULT_VHOST)
    parser.add_argument("--snippet-source", type=Path)
    parser.add_argument("--snippet-target", type=Path, default=DEFAULT_SNIPPET)
    parser.add_argument("--nginx", default="nginx")
    args = parser.parse_args()

    if args.action == "status":
        print(migration_status(args.vhost.read_text(encoding="utf-8")))
        return 0
    if args.snippet_source is None:
        parser.error(f"{args.action} requires --snippet-source")
    if args.action == "migrate":
        backup = migrate(
            vhost=args.vhost,
            snippet_source=args.snippet_source,
            snippet_target=args.snippet_target,
            nginx=args.nginx,
        )
        print(f"Hermes Nginx migration complete; backup: {backup}")
        return 0
    changed = reconcile(
        vhost=args.vhost,
        snippet_source=args.snippet_source,
        snippet_target=args.snippet_target,
        nginx=args.nginx,
    )
    print("Hermes Nginx snippet updated." if changed else "Hermes Nginx snippet is current.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ProxyConfigError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Hermes Nginx management failed: {exc}") from None
