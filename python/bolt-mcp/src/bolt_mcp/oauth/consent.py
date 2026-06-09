"""Minimal login + consent pages for the ``/authorize`` step.

Plain server-rendered HTML (no template engine dependency). The OAuth request
parameters are carried forward as hidden fields so the POST can reconstruct them
without server-side state. All interpolated values are HTML-escaped.
"""

from __future__ import annotations

from html import escape
from typing import Any

from .config import AuthorizationServer

# The OAuth request parameters threaded through login → consent → code issuance.
OAUTH_PARAM_KEYS = (
    "response_type",
    "client_id",
    "redirect_uri",
    "scope",
    "state",
    "code_challenge",
    "code_challenge_method",
)

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
 body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0b0c0f;color:#e7e9ee;
   display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}}
 .card{{background:#15171c;border:1px solid #262a33;border-radius:14px;padding:28px 30px;width:340px;
   box-shadow:0 10px 40px rgba(0,0,0,.4)}}
 h1{{font-size:18px;margin:0 0 4px}} p{{color:#9aa1ad;font-size:13px;margin:0 0 18px;line-height:1.5}}
 label{{display:block;font-size:12px;color:#9aa1ad;margin:12px 0 6px}}
 input[type=text],input[type=password]{{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:9px;
   border:1px solid #2c313c;background:#0f1115;color:#e7e9ee;font-size:14px}}
 .row{{display:flex;gap:10px;margin-top:20px}}
 button{{flex:1;padding:10px 14px;border-radius:9px;border:0;font-size:14px;font-weight:600;cursor:pointer}}
 .primary{{background:#4f7cff;color:#fff}} .ghost{{background:#21262f;color:#cfd4dd}}
 .err{{background:#2a1416;border:1px solid #5b2a2f;color:#f3b7bd;padding:8px 11px;border-radius:8px;
   font-size:12px;margin-bottom:14px}}
 .scope{{display:inline-block;background:#1d2128;border:1px solid #2c313c;border-radius:6px;
   padding:2px 8px;margin:3px 4px 0 0;font-size:12px;color:#cfd4dd}}
</style></head><body><div class="card">{body}</div></body></html>"""


def _hidden_fields(params: dict[str, Any]) -> str:
    out = []
    for key in OAUTH_PARAM_KEYS:
        value = params.get(key)
        if value is None:
            continue
        out.append(f'<input type="hidden" name="{escape(key)}" value="{escape(str(value))}">')
    return "\n".join(out)


def login_page(server: AuthorizationServer, params: dict[str, Any], *, error: str | None = None) -> str:
    action = escape(server.path("authorize"))
    err_html = f'<div class="err">{escape(error)}</div>' if error else ""
    body = f"""<h1>Sign in</h1>
<p>Authorize access to <strong>{escape(server.effective_issuer())}</strong>.</p>
{err_html}
<form method="post" action="{action}">
  {_hidden_fields(params)}
  <label for="username">Username</label>
  <input id="username" name="username" type="text" autocomplete="username" autofocus>
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password">
  <div class="row"><button class="primary" type="submit">Sign in</button></div>
</form>"""
    return _PAGE.format(title="Sign in", body=body)


def consent_page(
    server: AuthorizationServer,
    params: dict[str, Any],
    *,
    client_name: str,
    username: str,
) -> str:
    action = escape(server.path("authorize"))
    scopes = (params.get("scope") or "").split()
    scope_html = "".join(f'<span class="scope">{escape(s)}</span>' for s in scopes) or "<em>(no scopes)</em>"
    body = f"""<h1>Authorize {escape(client_name or "application")}</h1>
<p>Signed in as <strong>{escape(username)}</strong>. This application is requesting access:</p>
<div>{scope_html}</div>
<form method="post" action="{action}">
  {_hidden_fields(params)}
  <div class="row">
    <button class="primary" name="decision" value="approve" type="submit">Allow</button>
    <button class="ghost" name="decision" value="deny" type="submit">Deny</button>
  </div>
</form>"""
    return _PAGE.format(title="Authorize", body=body)
