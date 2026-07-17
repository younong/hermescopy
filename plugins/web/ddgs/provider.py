"""DuckDuckGo search — plugin form (via the ``ddgs`` package).

Subclasses the plugin-facing :class:`agent.web_search_provider.WebSearchProvider`.
The legacy in-tree module ``tools.web_providers.ddgs`` was removed in the
same commit that moved this code under ``plugins/``; this file is now the
canonical implementation.

The ``ddgs`` package is an optional dependency. ``is_available()`` reflects
whether the package is importable; the plugin still registers either way so
``hermes tools`` can prompt the user to install it.
"""

from __future__ import annotations

import concurrent.futures as _cf
import logging
from typing import Any, Dict

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

# Overall wall-clock cap for a single ddgs search. The DDGS constructor's
# ``timeout`` only bounds individual HTTP requests; ddgs's multi-engine retry
# loop has no overall cap, so a slow or unreachable engine can hang the
# (single, shared) agent loop indefinitely and block every platform (#36776).
# Enforce a hard cap here via a worker thread.
_SEARCH_TIMEOUT_SECS = 30

# Keep this allowlist synchronized with the text engines shipped by the exact
# pinned ddgs version. Accepting one engine only avoids reintroducing the
# multi-engine wait that ``auto`` can trigger on filtered networks.
_DDGS_TEXT_BACKENDS = frozenset({
    "auto",
    "bing",
    "brave",
    "duckduckgo",
    "google",
    "grokipedia",
    "mojeek",
    "startpage",
    "wikipedia",
    "yahoo",
    "yandex",
})


def _configured_ddgs_backend() -> str:
    """Return the validated owner-scoped DDGS text engine."""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
    except Exception as exc:  # pragma: no cover - defensive import/config guard
        raise ValueError(
            "web.ddgs_backend could not be loaded from Hermes configuration"
        ) from exc

    web_config = config.get("web", {})
    if not isinstance(web_config, dict):
        raise ValueError("web.ddgs_backend requires the web config to be a mapping")

    configured = web_config.get("ddgs_backend", "auto")
    if not isinstance(configured, str):
        raise ValueError("web.ddgs_backend must be one text engine name")

    backend = configured.strip().lower() or "auto"
    if "," in backend:
        raise ValueError(
            "web.ddgs_backend must select exactly one text engine, not a list"
        )
    if backend not in _DDGS_TEXT_BACKENDS:
        allowed = ", ".join(sorted(_DDGS_TEXT_BACKENDS))
        raise ValueError(
            f"unsupported web.ddgs_backend {backend!r}; choose one of: {allowed}"
        )
    return backend


def _run_ddgs_search(
    query: str,
    safe_limit: int,
    backend: str,
) -> list[dict[str, Any]]:
    """Run the blocking ddgs query and return normalized hits.

    Module-level (not a closure) so tests can patch it directly without
    spawning a real multi-second worker thread. ``DDGS(timeout=...)`` bounds
    each individual HTTP request; the overall wall-clock cap is enforced by
    the caller via a future timeout.
    """
    from ddgs import DDGS  # type: ignore

    results: list[dict[str, Any]] = []
    with DDGS(timeout=10) as client:
        for i, hit in enumerate(
            client.text(
                query,
                backend=backend,
                max_results=safe_limit,
            )
        ):
            if i >= safe_limit:
                break
            url = str(hit.get("href") or hit.get("url") or "")
            results.append(
                {
                    "title": str(hit.get("title", "")),
                    "url": url,
                    "description": str(hit.get("body", "")),
                    "position": i + 1,
                }
            )
    return results


class DDGSWebSearchProvider(WebSearchProvider):
    """DuckDuckGo HTML-scrape search provider.

    No API key needed. Rate limits are enforced server-side by DuckDuckGo;
    the provider surfaces ``DuckDuckGoSearchException`` and other ddgs errors
    as ``{"success": False, "error": ...}`` rather than raising.
    """

    @property
    def name(self) -> str:
        return "ddgs"

    @property
    def display_name(self) -> str:
        return "DuckDuckGo (ddgs)"

    def is_available(self) -> bool:
        """Return True when the ``ddgs`` package is importable.

        Probes the import once; cheap because Python caches the import. Must
        NOT perform network I/O — runs at tool-registration time and on every
        ``hermes tools`` paint.
        """
        try:
            import ddgs  # noqa: F401

            return True
        except ImportError:
            return False

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a DuckDuckGo search and return normalized results.

        The synchronous ``ddgs`` call is run in a worker thread with a hard
        wall-clock timeout (``_SEARCH_TIMEOUT_SECS``) so a hung search cannot
        block the shared agent loop indefinitely (#36776).
        """
        try:
            import ddgs  # type: ignore  # noqa: F401 — availability probe
        except ImportError:
            return {
                "success": False,
                "error": "ddgs package is not installed — run `pip install ddgs`",
            }

        # DDGS().text yields at most `max_results` items; we cap defensively
        # in case the package ignores the hint.
        safe_limit = max(1, int(limit))
        try:
            backend = _configured_ddgs_backend()
        except ValueError as exc:
            logger.warning(
                "DDGS search configuration rejected",
                extra={"error_type": type(exc).__name__},
            )
            return {
                "success": False,
                "error": f"DDGS search configuration error: {exc}",
            }

        # A fresh single-worker pool per call (rather than a module-level one)
        # is intentional: on timeout the blocking ddgs call cannot be cancelled
        # and keeps running, so a shared pool would serialise every later search
        # behind that hung worker. A per-call pool isolates each search from a
        # previously-hung one.
        pool = _cf.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(_run_ddgs_search, query, safe_limit, backend)
            try:
                web_results = future.result(timeout=_SEARCH_TIMEOUT_SECS)
            except _cf.TimeoutError:
                logger.warning(
                    "DDGS search timed out",
                    extra={
                        "backend": backend,
                        "timeout_seconds": _SEARCH_TIMEOUT_SECS,
                    },
                )
                return {
                    "success": False,
                    "error": (
                        f"DDGS search timed out after {_SEARCH_TIMEOUT_SECS}s "
                        f"using the {backend!r} engine. Try again later or set "
                        "web.ddgs_backend to a reachable single engine."
                    ),
                }
        except Exception as exc:  # noqa: BLE001 — ddgs raises its own exceptions
            logger.warning(
                "DDGS search failed",
                extra={
                    "backend": backend,
                    "error_type": type(exc).__name__,
                },
            )
            return {
                "success": False,
                "error": f"DDGS search failed ({type(exc).__name__})",
            }
        finally:
            # Return immediately without joining the worker. On timeout the
            # already-running ddgs call can't be cancelled (cancel_futures only
            # affects not-yet-started work), so the worker runs to completion
            # on its own; it writes nothing shared, so leaking it is safe.
            pool.shutdown(wait=False, cancel_futures=True)

        logger.info(
            "DDGS search completed",
            extra={
                "backend": backend,
                "results_count": len(web_results),
                "limit": limit,
            },
        )
        return {"success": True, "data": {"web": web_results}}

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "DuckDuckGo (ddgs)",
            "badge": "free · no key · search only",
            "tag": "Search via the ddgs Python package — no API key (pair with any extract provider)",
            "env_vars": [],
            # Trigger `_run_post_setup("ddgs")` after the user picks this row
            # so the ddgs Python package gets pip-installed on first selection.
            "post_setup": "ddgs",
        }
