"""Server-rendered /login page.

No React bundle is loaded. Listed providers come from the registry;
OAuth providers send a GET to ``/auth/login?provider=<name>`` and password
providers submit through the inline script below.

The standalone page mirrors the dedicated chat GUI workspace: a fixed light
canvas, neutral borders, system sans-serif typography, rounded controls, and
subtle shadows. It intentionally does not depend on the authenticated SPA or
its user-selectable themes.

Test-stable class names: the existing test suite extracts the
``class="provider-btn"`` anchor href to walk the OAuth flow. That exact class
attribute MUST NOT change without updating
``tests/hermes_cli/test_dashboard_auth_401_reauth.py``.
"""
from __future__ import annotations

import html

from hermes_cli.dashboard_auth import list_session_providers

# Inline minimal CSS. The dashboard's full skin lives in the React bundle,
# which we deliberately do NOT load here — the login page must not depend on
# the SPA build being present or on the injected session token.
#
# Single curly braces are placeholders for ``str.format``; CSS curlies are
# doubled (``{{`` / ``}}``).
_LOGIN_HTML_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in — Hermes Agent</title>
<style>
  :root {{
    color-scheme: light;
    --canvas: #f5f6f7;
    --surface: #ffffff;
    --foreground: #202124;
    --text-secondary: #6f747c;
    --text-tertiary: #969aa1;
    --border: #dedfe2;
    --border-strong: #c8d2df;
    --focus-border: #aebdd0;
    --focus-ring: rgba(220, 229, 239, 0.7);
    --action: #2f3338;
    --action-hover: #191b1e;
    --error: #b42318;
  }}

  *, *::before, *::after {{ box-sizing: border-box; }}

  html, body {{
    margin: 0;
    min-height: 100%;
    background: var(--canvas);
    color: var(--foreground);
    font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont,
      "Segoe UI", sans-serif;
    font-size: 16px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }}

  body {{
    display: grid;
    min-height: 100vh;
    min-height: 100dvh;
    place-items: center;
    padding: clamp(1.25rem, 6vh, 5rem) 1rem;
  }}

  main {{
    width: 100%;
    max-width: 26rem;
  }}

  .brand {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.65rem;
    margin-bottom: 1.25rem;
    color: #3d4148;
    font-size: 0.9rem;
    font-weight: 600;
  }}

  .brand-mark {{
    display: grid;
    width: 2.5rem;
    height: 2.5rem;
    place-items: center;
    border-radius: 0.75rem;
    background: #f2f3f5;
    color: #4f555d;
    font-size: 1.125rem;
    font-weight: 600;
  }}

  .card {{
    padding: clamp(1.5rem, 5vw, 2rem);
    border: 1px solid var(--border);
    border-radius: 1.35rem;
    background: var(--surface);
    box-shadow: 0 8px 28px rgba(31, 41, 55, 0.08);
  }}

  h1 {{
    margin: 0 0 0.35rem;
    color: #25282d;
    font-size: 1.5rem;
    font-weight: 600;
    letter-spacing: -0.02em;
  }}

  .subtitle {{
    margin: 0 0 1.5rem;
    color: var(--text-secondary);
    font-size: 0.9rem;
  }}

  .provider-list {{
    display: grid;
    gap: 1rem;
  }}

  .provider-btn {{
    display: block;
    width: 100%;
    padding: 0.75rem 1rem;
    border: 0;
    border-radius: 0.75rem;
    background: var(--action);
    color: #ffffff;
    cursor: pointer;
    font: inherit;
    font-size: 0.875rem;
    font-weight: 600;
    text-align: center;
    text-decoration: none;
    transition: background-color 120ms ease, box-shadow 120ms ease,
      opacity 120ms ease;
  }}

  .provider-btn:hover {{ background: var(--action-hover); }}
  .provider-btn:active {{ background: #0f1012; }}
  .provider-btn:disabled {{ cursor: not-allowed; opacity: 0.45; }}
  .provider-btn:focus-visible {{
    outline: none;
    box-shadow: 0 0 0 3px var(--focus-ring);
  }}

  .provider-form {{
    display: grid;
    gap: 0.85rem;
    text-align: left;
  }}

  .form-title {{
    color: #3d4148;
    font-size: 0.8125rem;
    font-weight: 600;
  }}

  .field {{
    display: grid;
    gap: 0.35rem;
  }}

  .field-label {{
    color: #555b64;
    font-size: 0.75rem;
    font-weight: 500;
  }}

  .field-input {{
    width: 100%;
    min-height: 2.75rem;
    padding: 0.7rem 0.8rem;
    border: 1px solid var(--border-strong);
    border-radius: 0.7rem;
    outline: none;
    background: var(--surface);
    color: #26292e;
    font: inherit;
    font-size: 0.9rem;
    transition: border-color 120ms ease, box-shadow 120ms ease;
  }}

  .field-input::placeholder {{ color: var(--text-tertiary); }}
  .field-input:hover {{ border-color: var(--focus-border); }}
  .field-input:focus {{
    border-color: var(--focus-border);
    box-shadow: 0 0 0 2px var(--focus-ring);
  }}

  .password-input-wrap {{ position: relative; }}
  .password-input-wrap .field-input {{ padding-right: 3rem; }}
  .password-input-wrap input::-ms-reveal,
  .password-input-wrap input::-ms-clear {{ display: none; }}

  .password-toggle {{
    position: absolute;
    top: 50%;
    right: 0.4rem;
    display: inline-flex;
    width: 2rem;
    height: 2rem;
    padding: 0;
    transform: translateY(-50%);
    align-items: center;
    justify-content: center;
    border: 0;
    border-radius: 0.5rem;
    background: transparent;
    color: #737880;
    cursor: pointer;
    transition: background-color 120ms ease, color 120ms ease,
      box-shadow 120ms ease;
  }}

  .password-toggle:hover {{
    background: #f0f1f2;
    color: #25282d;
  }}

  .password-toggle:focus-visible {{
    outline: none;
    box-shadow: 0 0 0 2px var(--focus-ring);
  }}

  .password-toggle svg {{ width: 1rem; height: 1rem; }}
  .password-toggle svg[hidden] {{ display: none; }}

  .form-error {{
    padding: 0.55rem 0.7rem;
    border-radius: 0.55rem;
    background: #fff6f5;
    color: var(--error);
    font-size: 0.8rem;
  }}

  .provider-form .provider-btn {{ margin-top: 0.15rem; }}

  footer {{
    margin-top: 1rem;
    color: var(--text-tertiary);
    font-size: 0.75rem;
    text-align: center;
  }}

  ::selection {{ background: #dce5ef; color: var(--foreground); }}

  @media (max-height: 34rem) {{
    body {{ place-items: start center; }}
  }}
</style>
</head>
<body>
<main>
  <div class="brand"><span class="brand-mark" aria-hidden="true">H</span>Hermes</div>
  <div class="card">
    <h1>Welcome back</h1>
    <p class="subtitle">Sign in to continue to your Hermes workspace.</p>
    <div class="provider-list">
{provider_buttons}
    </div>
  </div>
  <footer>Hermes secure workspace</footer>
</main>
{password_script}
</body>
</html>
"""

_EMPTY_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign-in unavailable — Hermes Agent</title>
<style>
  :root {
    color-scheme: light;
    --canvas: #f5f6f7;
    --surface: #ffffff;
    --foreground: #202124;
    --text-secondary: #6f747c;
    --border: #dedfe2;
  }
  *, *::before, *::after { box-sizing: border-box; }
  html, body {
    margin: 0;
    min-height: 100%;
    background: var(--canvas);
    color: var(--foreground);
    font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont,
      "Segoe UI", sans-serif;
    font-size: 16px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }
  body {
    display: grid;
    min-height: 100vh;
    min-height: 100dvh;
    place-items: center;
    padding: clamp(1.25rem, 6vh, 5rem) 1rem;
  }
  main {
    width: 100%;
    max-width: 32rem;
    padding: clamp(1.5rem, 5vw, 2rem);
    border: 1px solid var(--border);
    border-radius: 1.35rem;
    background: var(--surface);
    box-shadow: 0 8px 28px rgba(31, 41, 55, 0.08);
  }
  .brand-mark {
    display: grid;
    width: 2.5rem;
    height: 2.5rem;
    margin-bottom: 1.25rem;
    place-items: center;
    border-radius: 0.75rem;
    background: #f2f3f5;
    color: #4f555d;
    font-size: 1.125rem;
    font-weight: 600;
  }
  h1 {
    margin: 0 0 0.75rem;
    color: #25282d;
    font-size: 1.5rem;
    font-weight: 600;
    letter-spacing: -0.02em;
  }
  p { margin: 0 0 1rem; color: var(--text-secondary); }
  p:last-child { margin-bottom: 0; }
  code {
    border-radius: 0.3rem;
    background: #f2f3f5;
    color: #3d4148;
    padding: 0.12em 0.35em;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.9em;
  }
</style>
</head>
<body>
<main>
<div class="brand-mark" aria-hidden="true">H</div>
<h1>Sign-in unavailable</h1>
<p>This dashboard is bound to a non-loopback host but no authentication
providers are installed.</p>
<p>Install <code>plugins/dashboard-auth-nous</code> (default) or another
auth provider, or restart with <code>--insecure</code> to bypass the
auth gate (not recommended on untrusted networks).</p>
</main>
</body>
</html>
"""


# Inline script that wires every password provider form to toggle password
# visibility, POST JSON to ``/auth/password-login``, and navigate on success.
# Emitted ONLY when at least one ``supports_password`` provider is listed
# (OAuth-only login pages stay script-free, preserving that no-JS contract).
#
# Plain string (NOT run through ``str.format``), so braces are literal — do not
# double them. Each handler scopes its controls and credentials to one form.
_PASSWORD_FORM_SCRIPT = """\
<script>
(function () {
  // The dashboard may be reverse-proxied under a path prefix such as
  // /hermes. Build form and landing paths from the rendered login URL so
  // password authentication never escapes that prefix to the proxy's default
  // upstream.
  var loginPrefix = window.location.pathname.replace(/\\/login$/, '') || '';

  function handle(form) {
    var passwordInput = form.querySelector('input[name=password]');
    var passwordToggle = form.querySelector('.password-toggle');
    if (passwordInput && passwordToggle) {
      passwordToggle.addEventListener('click', function () {
        var reveal = passwordInput.type === 'password';
        var label = reveal ? 'Hide password' : 'Show password';
        passwordInput.type = reveal ? 'text' : 'password';
        passwordToggle.setAttribute('aria-label', label);
        passwordToggle.setAttribute('title', label);
        passwordToggle.setAttribute('aria-pressed', reveal ? 'true' : 'false');
        var showIcon = passwordToggle.querySelector('.toggle-icon-show');
        var hideIcon = passwordToggle.querySelector('.toggle-icon-hide');
        if (showIcon) {
          if (reveal) { showIcon.setAttribute('hidden', ''); }
          else { showIcon.removeAttribute('hidden'); }
        }
        if (hideIcon) {
          if (reveal) { hideIcon.removeAttribute('hidden'); }
          else { hideIcon.setAttribute('hidden', ''); }
        }
      });
    }

    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var err = form.querySelector('.form-error');
      var btn = form.querySelector('button[type=submit]');
      if (err) { err.hidden = true; err.textContent = ''; }
      if (btn) { btn.disabled = true; }
      var body = {
        provider: form.getAttribute('data-provider') || '',
        username: (form.querySelector('input[name=username]') || {}).value || '',
        password: (form.querySelector('input[name=password]') || {}).value || '',
        next: (form.querySelector('input[name=next]') || {}).value || ''
      };
      fetch(loginPrefix + '/auth/password-login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        credentials: 'same-origin'
      }).then(function (resp) {
        if (resp.ok) {
          return resp.json().then(function (data) {
            var target = (data && data.next) || '/';
            window.location.assign(loginPrefix + target);
          });
        }
        var msg = resp.status === 429
          ? 'Too many attempts. Please wait and try again.'
          : (resp.status === 401 ? 'Invalid username or password.'
                                 : 'Sign-in failed. Please try again.');
        if (err) { err.textContent = msg; err.hidden = false; }
        if (btn) { btn.disabled = false; }
      }).catch(function () {
        if (err) { err.textContent = 'Network error. Please try again.'; err.hidden = false; }
        if (btn) { btn.disabled = false; }
      });
    });
  }
  var forms = document.querySelectorAll('form.provider-form');
  for (var i = 0; i < forms.length; i++) { handle(forms[i]); }
})();
</script>
"""


def render_login_html(*, next_path: str = "") -> str:
    """Return the full HTML for ``GET /login``.

    ``next_path`` — when set, the post-login landing path the user originally
    requested. Threaded into each provider button's ``href`` as a ``next=``
    query parameter so the OAuth round trip carries it end-to-end. The caller
    (``routes.login_page``) is responsible for validating ``next_path`` against
    the same-origin rules before we emit it; we still HTML-escape it as defence
    in depth.
    """
    providers = list_session_providers()
    if not providers:
        return _EMPTY_HTML

    if next_path:
        # URL-encode then HTML-escape. The URL-encode step matches the gate's
        # ``_safe_next_target`` output shape (also URL-encoded), so a value that
        # round-tripped from /login?next=... back into the button href is
        # byte-identical.
        from urllib.parse import quote

        next_qs = f"&next={html.escape(quote(next_path, safe=''), quote=True)}"
    else:
        next_qs = ""

    buttons = []
    needs_password_script = False
    for provider_index, provider in enumerate(providers):
        if getattr(provider, "supports_password", False):
            needs_password_script = True
            buttons.append(
                _render_password_form(provider, next_path, provider_index)
            )
        else:
            buttons.append(
                f'      <a class="provider-btn" '
                f'href="/auth/login?provider={html.escape(provider.name, quote=True)}{next_qs}">'
                f"Sign in with {html.escape(provider.display_name)}</a>"
            )
    script = _PASSWORD_FORM_SCRIPT if needs_password_script else ""
    return _LOGIN_HTML_TEMPLATE.format(
        provider_buttons="\n".join(buttons),
        password_script=script,
    )


def _render_password_form(provider, next_path: str, provider_index: int) -> str:
    """Render a username/password form for one password provider.

    Numeric input IDs avoid trusting provider names as DOM identifiers and
    remain unique when more than one password provider is installed. The
    provider name and validated landing path are still escaped before being
    emitted.
    """
    pname = html.escape(provider.name, quote=True)
    plabel = html.escape(provider.display_name)
    safe_next = html.escape(next_path, quote=True) if next_path else ""
    username_id = f"login-username-{provider_index}"
    password_id = f"login-password-{provider_index}"
    return (
        f'      <form class="provider-form" data-provider="{pname}" '
        f'autocomplete="on">\n'
        f'        <div class="form-title">Sign in with {plabel}</div>\n'
        f'        <input type="hidden" name="next" value="{safe_next}">\n'
        f'        <div class="field">\n'
        f'          <label class="field-label" for="{username_id}">Username</label>\n'
        f'          <input class="field-input" id="{username_id}" type="text" '
        f'name="username" autocomplete="username" autocapitalize="none" '
        f'autocorrect="off" spellcheck="false" required>\n'
        f'        </div>\n'
        f'        <div class="field">\n'
        f'          <label class="field-label" for="{password_id}">Password</label>\n'
        f'          <div class="password-input-wrap">\n'
        f'            <input class="field-input" id="{password_id}" type="password" '
        f'name="password" autocomplete="current-password" required>\n'
        f'            <button class="password-toggle" type="button" '
        f'aria-label="Show password" title="Show password" aria-pressed="false" '
        f'aria-controls="{password_id}">\n'
        f'              <svg class="toggle-icon-show" aria-hidden="true" focusable="false" '
        f'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round">'
        f'<path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"/>'
        f'<circle cx="12" cy="12" r="3"/></svg>\n'
        f'              <svg class="toggle-icon-hide" aria-hidden="true" focusable="false" '
        f'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round" hidden>'
        f'<path d="m2 2 20 20"/><path d="M6.71 6.71C4.89 8.03 3.5 9.76 2.66 11.67a1 1 0 0 0 0 .66C4.35 16.23 7.93 19 12 19c1.5 0 2.91-.38 4.15-1.05"/>'
        f'<path d="M10.73 5.08A9.8 9.8 0 0 1 12 5c4.07 0 7.65 2.77 9.34 6.67a1 1 0 0 1 0 .66 11 11 0 0 1-1.04 1.84"/>'
        f'<path d="M14.12 14.12A3 3 0 0 1 9.88 9.88"/></svg>\n'
        f'            </button>\n'
        f'          </div>\n'
        f'        </div>\n'
        f'        <div class="form-error" role="alert" hidden></div>\n'
        f'        <button class="provider-btn" type="submit">Sign in</button>\n'
        f'      </form>'
    )
