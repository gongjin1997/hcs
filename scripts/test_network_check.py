"""现场网络脚本回归：仅本机临时 HTTP/TLS 服务，不使用真实 HCS、配置或凭据。"""

import contextlib
import datetime
import errno
import importlib.util
import io
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "network_check", ROOT / "deploy/scripts/network-check.py"
)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class NetworkCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hcs-network-check-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        blocked = {
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
            "ssl_cert_file",
            "ssl_cert_dir",
        }
        env = {
            key: value
            for key, value in os.environ.items()
            if key.lower() not in blocked
        }
        self.environment = patch.dict(os.environ, env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    @contextlib.contextmanager
    def server(self, *, tls=False):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self):
                received.append(dict(self.headers))
                self.send_response(401)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        if tls:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name(
                [x509.NameAttribute(NameOID.COMMON_NAME, "Local diagnostic fixture")]
            )
            now = datetime.datetime.now(datetime.UTC)
            certificate = (
                x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(
                    x509.BasicConstraints(ca=True, path_length=None), critical=True
                )
                .add_extension(
                    x509.KeyUsage(
                        True, False, True, False, False, True, True, False, False
                    ),
                    critical=True,
                )
                .add_extension(
                    x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                    critical=False,
                )
                .add_extension(
                    x509.AuthorityKeyIdentifier.from_issuer_public_key(
                        key.public_key()
                    ),
                    critical=False,
                )
                .add_extension(
                    x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                    critical=False,
                )
                .add_extension(
                    x509.SubjectAlternativeName(
                        [
                            x509.DNSName("localhost"),
                            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                        ]
                    ),
                    critical=False,
                )
                .sign(key, hashes.SHA256())
            )
            (self.root / "ca.pem").write_bytes(
                certificate.public_bytes(serialization.Encoding.PEM)
            )
            private = self.root / "key.pem"
            private.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            private.chmod(0o600)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(self.root / "ca.pem", private)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield (
                f"{'https' if tls else 'http'}://127.0.0.1:{server.server_port}",
                received,
            )
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_http_reachable_401_is_transport_success_without_credentials(self):
        with self.server() as (url, received):
            result = probe.check(url)
        self.assertTrue(result["ok"])
        self.assertEqual(result["http"]["status"], 401)
        self.assertFalse(result["tenant_credentials_sent"])
        self.assertFalse(result["http"]["authentication_tested"])
        self.assertEqual(len(received), 1)
        self.assertFalse(
            any(
                key.lower() in ("authorization", "x-auth-token", "x-sdk-date")
                for key in received[0]
            )
        )

    def test_untrusted_certificate_identifies_tls_layer(self):
        with self.server(tls=True) as (url, received):
            result = probe.check(url)
        self.assertFalse(result["ok"])
        self.assertTrue(result["direct_connections"][0]["tcp"]["ok"])
        self.assertEqual(
            result["direct_connections"][0]["tls"]["reason"], "TLS_CERTIFICATE"
        )
        self.assertEqual(result["http"]["reason"], "TLS_CERTIFICATE")
        self.assertEqual(received, [])

    def test_supplied_ca_verifies_same_tls_endpoint(self):
        with self.server(tls=True) as (url, received):
            result = probe.check(url, ca_file=str(self.root / "ca.pem"))
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["direct_connections"][0]["tls"]["certificate_verified"])
        self.assertTrue(result["ca_file_exists"])
        self.assertTrue(result["ca_file_readable"])
        self.assertEqual(len(received), 1)

    def test_existing_disabled_verification_is_reported_without_changing_it(self):
        with self.server(tls=True) as (url, _):
            result = probe.check(url, verify=False)
        self.assertTrue(result["ok"])
        self.assertIn("warning", result)
        self.assertFalse(result["configuration_modified"])
        self.assertFalse(result["direct_connections"][0]["tls"]["certificate_verified"])

    def test_missing_ca_is_configuration_error_without_http_request(self):
        with self.server(tls=True) as (url, received):
            result = probe.check(url, ca_file=str(self.root / "missing.pem"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["tls_configuration"]["reason"], "CA_CONFIGURATION")
        self.assertEqual(result["http"]["reason"], "CA_CONFIGURATION")
        self.assertEqual(received, [])

    def test_connection_refused_classification(self):
        with socket.socket() as local:
            local.bind(("127.0.0.1", 0))
            result = probe.check(f"http://127.0.0.1:{local.getsockname()[1]}")
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["direct_connections"][0]["tcp"]["reason"], "TCP_REFUSED"
        )

    def test_dns_failure_is_bounded_and_reported(self):
        error = socket.gaierror(socket.EAI_NONAME, "private-host")
        with (
            patch.object(probe, "resolve", side_effect=error),
            patch.object(
                probe.httpx, "Client", side_effect=httpx.ConnectError("private-host")
            ),
        ):
            result = probe.check("https://example.invalid")
        self.assertFalse(result["ok"])
        self.assertEqual(result["dns"]["reason"], "DNS")
        self.assertNotIn("private-host", json.dumps(result))

    def test_nested_causes_and_cycles_never_echo_exception_or_proxy_secrets(self):
        inner = ssl.SSLCertVerificationError(
            1, "https://user:private-password@private-host/?token=private-token"
        )
        error = httpx.ConnectError("private-password")
        error.__cause__ = inner
        inner.__context__ = error
        with (
            patch.dict(
                os.environ,
                {"HTTPS_PROXY": "http://user:private-proxy-password@localhost:1"},
            ),
            patch.object(probe, "resolve", return_value=[]),
            patch.object(probe.httpx, "Client", side_effect=error),
        ):
            result = probe.check("https://example.invalid")
        self.assertEqual(result["http"]["reason"], "TLS_CERTIFICATE")
        self.assertIn("HTTPS_PROXY", result["proxy_environment_present"])
        self.assertNotIn("private-", json.dumps(result))
        self.assertLessEqual(len(result["http"]["exception_types"]), 16)

    def test_invalid_urls_never_resolve_or_echo_embedded_secrets(self):
        for url in (
            "https://user:private-password@example.invalid",
            "https://example.invalid/?token=private-token",
            "ftp://example.invalid",
            "https://example.invalid:bad",
        ):
            with self.subTest(url=url), patch.object(probe, "resolve") as lookup:
                result = probe.check(url)
                self.assertFalse(result["ok"])
                self.assertNotIn("private-", json.dumps(result))
                lookup.assert_not_called()

    def test_network_errno_and_timeouts(self):
        for error, reason in [
            (OSError(errno.ENETUNREACH, "private"), "NO_ROUTE"),
            (OSError(errno.EHOSTUNREACH, "private"), "NO_ROUTE"),
            (httpx.ConnectTimeout("private"), "TIMEOUT"),
            (ssl.SSLError("private"), "TLS_HANDSHAKE"),
        ]:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(probe.failure(error)["reason"], reason)
                self.assertNotIn("private", json.dumps(probe.failure(error)))

    def test_configuration_warning_does_not_rewrite_region_or_domain(self):
        self.assertEqual(
            len(
                probe.configuration_warnings(
                    "region-1_team", "console.example.invalid", "team"
                )
            ),
            2,
        )
        self.assertEqual(
            probe.configuration_warnings("region-1", "example.invalid", "team"), []
        )

    def test_unexpected_settings_error_is_sanitized(self):
        with (
            patch.object(probe, "_main", side_effect=RuntimeError("private-password")),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(probe.main(["--tenant", "1"]), 2)
        self.assertFalse(json.loads(out.getvalue())["ok"])
        self.assertNotIn("private-password", out.getvalue())

    def test_multi_service_checks_deduplicate_gateways_and_preserve_sources(self):
        targets = [
            {"service": "ecs", "endpoint_source": "tenant", "url": "https://shared.example.invalid"},
            {"service": "dcs", "endpoint_source": "global", "url": "https://shared.example.invalid"},
        ]
        with patch.object(probe, "check", return_value={"ok": True, "http": {"status": 401}}) as check:
            result = probe.check_services(targets, verify=False, ca_file=None)
        self.assertTrue(result["ok"])
        self.assertEqual(check.call_count, 1)
        self.assertEqual([item["service"] for item in result["services"]], ["ecs", "dcs"])
        self.assertEqual([item["endpoint_source"] for item in result["services"]], ["tenant", "global"])
        self.assertFalse(result["authentication_tested"])
        self.assertFalse(result["tenant_credentials_sent"])
        self.assertFalse(result["configuration_modified"])

    def test_multi_service_missing_endpoint_is_not_silently_successful(self):
        target = {"service": "cce", "endpoint_source": "global", "configuration_error": "端点缺失"}
        with patch.object(probe, "check") as check:
            result = probe.check_services([target], verify=True, ca_file=None)
        self.assertFalse(result["ok"])
        check.assert_not_called()

    def test_service_resolution_failure_never_echoes_template_or_exception(self):
        class Resolver:
            def base(self, _service):
                raise ValueError("private-password")

        targets = probe.service_targets(Resolver(), ["cce"], overrides={})
        self.assertNotIn("private-", json.dumps(targets))
        self.assertEqual(targets[0]["service"], "cce")

    def test_service_flags_require_tenant_and_known_services(self):
        for args in (["--url", "https://example.invalid", "--services", "all"],
                     ["--tenant", "1", "--services", "private-unknown"],
                     ["--tenant", "1", "--project-ref", "-1"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                probe._main(args)
            self.assertEqual(error.exception.code, 2)


class NetworkWrapperTests(unittest.TestCase):
    def test_wrapper_uses_existing_container_stdin_without_host_python(self):
        with tempfile.TemporaryDirectory(prefix="hcs-network-wrapper-") as temp:
            root = Path(temp)
            deployment = root / "installed"
            (deployment / "scripts").mkdir(parents=True)
            capture = root / "invocation.json"
            (deployment / "scripts/common.sh").write_text(
                "need_docker() { :; }\n"
                'dc() { "'
                + sys.executable
                + '" "'
                + str(root / "fake-compose.py")
                + '" "$@"; }\n'
            )
            (root / "fake-compose.py").write_text(
                "import json,sys\nfrom pathlib import Path\n"
                "Path("
                + repr(str(capture))
                + ').write_text(json.dumps({"args":sys.argv[1:],"script":sys.stdin.read()}))\n'
            )
            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / "deploy/scripts/network-check.sh"),
                    "--tenant",
                    "1",
                    "--deploy-dir",
                    str(deployment),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            observed = json.loads(capture.read_text())
            self.assertEqual(
                observed["args"], ["exec", "-T", "api", "python", "-", "--tenant", "1"]
            )
            self.assertEqual(
                observed["script"],
                (ROOT / "deploy/scripts/network-check.py").read_text(),
            )

    def test_wrapper_help_does_not_require_installed_docker(self):
        result = subprocess.run(
            ["bash", str(ROOT / "deploy/scripts/network-check.sh"), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("/data/hcs-inspector", result.stdout)


if __name__ == "__main__":
    unittest.main()
