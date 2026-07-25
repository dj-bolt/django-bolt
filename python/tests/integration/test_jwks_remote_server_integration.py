"""Fetch and use a remote JWKS over real HTTPS during server startup."""

from __future__ import annotations

import ipaddress
import json
import ssl
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from jwt.algorithms import RSAAlgorithm

from .apps import app_module
from .apps.jwks_remote import AUDIENCE, ISSUER
from .helpers import get_free_port

pytestmark = pytest.mark.server_integration

KID = "integration-signing-key"


def generate_server_certificate(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path = tmp_path / "jwks-server-cert.pem"
    private_key_path = tmp_path / "jwks-server-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private_key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate_path, private_key_path


@contextmanager
def https_jwks_server(tmp_path, document: dict):
    body = json.dumps(document).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/.well-known/jwks.json":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    certificate_path, private_key_path = generate_server_certificate(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate_path, private_key_path)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"https://127.0.0.1:{server.server_port}/.well-known/jwks.json",
            certificate_path,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def jwks_for(key, *, kid: str = KID) -> dict:
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return {"keys": [jwk]}


def make_token(key, *, audience: str = AUDIENCE) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": "remote-jwks-user",
            "iat": now,
            "exp": now + 3600,
            "iss": ISSUER,
            "aud": audience,
        },
        key,
        algorithm="RS256",
        headers={"kid": KID},
    )


def test_remote_jwks_over_real_https(make_server_project, tmp_path):
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    with https_jwks_server(tmp_path, jwks_for(signing_key)) as (jwks_url, certificate_path):
        project = make_server_project(api_module=app_module("jwks_remote"))
        env = {
            "DJANGO_BOLT_TEST_JWKS_URL": jwks_url,
            "SSL_CERT_FILE": str(certificate_path),
        }
        with project.start(env=env) as server:
            valid = server.get(
                "/whoami",
                headers={"Authorization": f"Bearer {make_token(signing_key)}"},
            )
            assert valid.status_code == 200, valid.text
            assert valid.json()["user_id"] == "remote-jwks-user"

            wrong_signature = server.get(
                "/whoami",
                headers={"Authorization": f"Bearer {make_token(wrong_key)}"},
            )
            assert wrong_signature.status_code == 401, wrong_signature.text

            wrong_audience = server.get(
                "/whoami",
                headers={"Authorization": f"Bearer {make_token(signing_key, audience='wrong-audience')}"},
            )
            assert wrong_audience.status_code == 401, wrong_audience.text


def test_unreachable_remote_jwks_aborts_startup(make_server_project, tmp_path):
    certificate_path, _ = generate_server_certificate(tmp_path)
    unreachable_url = f"https://127.0.0.1:{get_free_port()}/.well-known/jwks.json"
    project = make_server_project(api_module=app_module("jwks_remote"))

    with (
        pytest.raises(AssertionError) as excinfo,
        project.start(
            timeout=15,
            env={
                "DJANGO_BOLT_TEST_JWKS_URL": unreachable_url,
                "SSL_CERT_FILE": str(certificate_path),
            },
        ),
    ):
        pass
    assert "runbolt exited before the server became ready" in str(excinfo.value)
