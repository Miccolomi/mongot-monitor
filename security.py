"""
Security utilities.
- K8s name validation (prevents path traversal / injection in URL params)
- HTTP security headers (CSP, X-Frame-Options, X-Content-Type-Options, …)
- Optional HTTP Basic Auth
"""

from __future__ import annotations

import base64
import functools
import re

from flask import Flask, request, Response

# Kubernetes RFC 1123 label: lowercase alphanumeric and hyphens, max 253 chars.
# Dots are allowed in namespace names (e.g. cert-manager.io sub-namespaces).
_K8S_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9\-\.]{0,251}[a-z0-9]$')


def is_valid_k8s_name(name: str) -> bool:
    """Return True if *name* is a valid Kubernetes object name."""
    if not name or len(name) > 253:
        return False
    # Single-character names are valid (e.g. namespace "d")
    if len(name) == 1:
        return bool(re.match(r'^[a-z0-9]$', name))
    return bool(_K8S_NAME_RE.match(name))


# ── Security headers ──────────────────────────────────────────────────────────

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self';"
    ),
}


def register_security_headers(app: Flask) -> None:
    """Attach an after_request hook that injects security headers on every response."""

    @app.after_request
    def _add_headers(response: Response) -> Response:
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


# ── Basic Auth ────────────────────────────────────────────────────────────────

class BasicAuth:
    """
    Minimal HTTP Basic Auth guard.
    Usage:
        auth = BasicAuth("user", "secret")
        auth.register(app)          # protects all routes
        auth.protect(view_func)     # protects a single view
    """

    def __init__(self, username: str, password: str):
        self._username = username
        self._password = password

    def _check(self) -> bool:
        auth = request.authorization
        return bool(
            auth
            and auth.username == self._username
            and auth.password == self._password
        )

    def _unauthorized(self) -> Response:
        return Response(
            "Unauthorized", 401,
            {"WWW-Authenticate": 'Basic realm="Mongot Monitor"'},
        )

    def protect(self, fn):
        """Decorator: protect a single view function."""
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not self._check():
                return self._unauthorized()
            return fn(*args, **kwargs)
        return wrapper

    def register(self, app: Flask) -> None:
        """Protect ALL routes on *app* via a before_request hook."""

        @app.before_request
        def _require_auth():
            if not self._check():
                return self._unauthorized()
