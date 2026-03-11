"""
Security tests — input validation, HTTP headers, Basic Auth.
"""

import base64
import pytest

from security import is_valid_k8s_name, BasicAuth
from mongot_monitor import create_app


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def auth_client():
    """Flask test client with Basic Auth enabled (user=admin, password=secret)."""
    app = create_app(basic_auth=BasicAuth("admin", "secret"))
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _basic(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# ── is_valid_k8s_name ─────────────────────────────────────────────────────────

class TestK8sNameValidation:
    def test_valid_simple(self):
        assert is_valid_k8s_name("mypod") is True

    def test_valid_with_hyphens(self):
        assert is_valid_k8s_name("my-pod-0") is True

    def test_valid_single_char(self):
        assert is_valid_k8s_name("a") is True

    def test_valid_with_dots(self):
        assert is_valid_k8s_name("my.namespace") is True

    def test_valid_max_length(self):
        assert is_valid_k8s_name("a" * 253) is True

    def test_invalid_empty(self):
        assert is_valid_k8s_name("") is False

    def test_invalid_too_long(self):
        assert is_valid_k8s_name("a" * 254) is False

    def test_invalid_uppercase(self):
        assert is_valid_k8s_name("MyPod") is False

    def test_invalid_starts_with_hyphen(self):
        assert is_valid_k8s_name("-mypod") is False

    def test_invalid_ends_with_hyphen(self):
        assert is_valid_k8s_name("mypod-") is False

    def test_invalid_path_traversal(self):
        assert is_valid_k8s_name("../etc/passwd") is False

    def test_invalid_slash(self):
        assert is_valid_k8s_name("ns/pod") is False

    def test_invalid_null_byte(self):
        assert is_valid_k8s_name("pod\x00name") is False

    def test_invalid_special_chars(self):
        assert is_valid_k8s_name("pod;rm -rf /") is False

    def test_invalid_none(self):
        assert is_valid_k8s_name(None) is False  # type: ignore


# ── Security headers ──────────────────────────────────────────────────────────

class TestSecurityHeaders:
    def test_x_content_type_options(self, client):
        resp = client.get("/")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"

    def test_x_frame_options(self, client):
        resp = client.get("/")
        assert resp.headers.get("X-Frame-Options") == "DENY"

    def test_x_xss_protection(self, client):
        resp = client.get("/")
        assert resp.headers.get("X-XSS-Protection") == "1; mode=block"

    def test_referrer_policy(self, client):
        resp = client.get("/")
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    def test_content_security_policy_present(self, client):
        resp = client.get("/")
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "default-src" in csp
        assert "'self'" in csp

    def test_headers_on_api_endpoints(self, client):
        resp = client.get("/healthcheck")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"

    def test_headers_on_static_assets(self, client):
        resp = client.get("/static/css/main.css")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"


# ── Input validation on log endpoints ────────────────────────────────────────

class TestInputValidation:
    def test_valid_pod_name_passes_to_k8s_check(self, client):
        # No K8s configured → 500, but input validation passed
        resp = client.get("/api/logs/default/my-pod-0")
        assert resp.status_code == 500  # K8s not available, not 400

    def test_invalid_pod_name_returns_400(self, client):
        # Flask normalises path traversal sequences before routing → 404
        # Our validation still blocks them when they reach the route
        resp = client.get("/api/logs/default/../etc/passwd")
        assert resp.status_code in (400, 404)

    def test_invalid_namespace_returns_400(self, client):
        resp = client.get("/api/logs/INVALID_NS/my-pod")
        assert resp.status_code == 400

    def test_path_traversal_in_namespace_returns_400(self, client):
        resp = client.get("/api/logs/../../etc/my-pod")
        assert resp.status_code in (400, 404)  # Flask may 404 before our check

    def test_semicolon_in_pod_name_returns_400(self, client):
        resp = client.get("/api/logs/default/pod;malicious")
        assert resp.status_code in (400, 404)

    def test_download_logs_invalid_name_returns_400(self, client):
        resp = client.get("/api/download_logs/default/POD_NAME_INVALID")
        assert resp.status_code == 400

    def test_download_logs_valid_name_passes_to_k8s(self, client):
        resp = client.get("/api/download_logs/default/valid-pod-0")
        assert resp.status_code == 500  # K8s not available, not 400


# ── Basic Auth ────────────────────────────────────────────────────────────────

class TestBasicAuth:
    def test_no_credentials_returns_401(self, auth_client):
        resp = auth_client.get("/")
        assert resp.status_code == 401

    def test_wrong_password_returns_401(self, auth_client):
        resp = auth_client.get("/", headers=_basic("admin", "wrong"))
        assert resp.status_code == 401

    def test_wrong_username_returns_401(self, auth_client):
        resp = auth_client.get("/", headers=_basic("hacker", "secret"))
        assert resp.status_code == 401

    def test_correct_credentials_returns_200(self, auth_client):
        resp = auth_client.get("/", headers=_basic("admin", "secret"))
        assert resp.status_code == 200

    def test_auth_required_on_api_endpoints(self, auth_client):
        resp = auth_client.get("/healthcheck")
        assert resp.status_code == 401

    def test_auth_passes_on_api_with_correct_credentials(self, auth_client):
        resp = auth_client.get("/healthcheck", headers=_basic("admin", "secret"))
        assert resp.status_code == 200

    def test_www_authenticate_header_present(self, auth_client):
        resp = auth_client.get("/")
        assert "WWW-Authenticate" in resp.headers

    def test_no_auth_configured_does_not_require_credentials(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
