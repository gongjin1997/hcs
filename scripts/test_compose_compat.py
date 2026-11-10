"""Compose v1 conversion regressions without Docker or real deployment secrets."""

from __future__ import annotations

import copy
import importlib.util
import io
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/scripts/compose-compat.py"
SPEC = importlib.util.spec_from_file_location("compose_compat_under_test", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise ImportError(f"Cannot load {SCRIPT.name}")
compat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compat)

NESTED_DATABASE_URL = (
    "${INSPECTOR_DATABASE_URL:-postgresql+psycopg://${POSTGRES_USER:-inspector}:"
    "${POSTGRES_PASSWORD:?fixture password required}@postgres:5432/${POSTGRES_DB:-inspector}}"
)
FLAT_DATABASE_URL = (
    "postgresql+psycopg://${POSTGRES_USER:-inspector}:${POSTGRES_PASSWORD:?缺少数据库口令}"
    "@postgres:5432/${POSTGRES_DB:-inspector}"
)


class ComposeCompatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hcs-compose-compat-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "compose.yml"
        self.destination = self.root / "generated/compose-v1.yml"
        self.document = {
            "name": "${COMPOSE_PROJECT_NAME:-fixture}",
            "version": "3.9",
            "services": {
                "postgres": {
                    "image": "fixture/postgres:local",
                    "pull_policy": "never",
                    "environment": {"POSTGRES_PASSWORD": "${POSTGRES_PASSWORD:?fixture password required}"},
                    "healthcheck": {"test": ["CMD", "fixture-health"], "interval": "5s", "start_interval": "1s", "retries": 3},
                    "volumes": ["fixture-data:/var/lib/postgresql/data"],
                },
                "redis": {"image": "fixture/redis:local", "pull_policy": "never"},
                "migrate": {
                    "image": "fixture/backend:local",
                    "command": ["fixture-migration-never-run"],
                    "depends_on": {"postgres": {"condition": "service_healthy"}},
                    "environment": {"INSPECTOR_DATABASE_URL": NESTED_DATABASE_URL},
                },
                "api": {
                    "image": "fixture/backend:local",
                    "pull_policy": "never",
                    "env_file": [".env"],
                    "depends_on": {
                        "migrate": {"condition": "service_completed_successfully"},
                        "postgres": {"condition": "service_healthy"},
                        "redis": {"condition": "service_healthy"},
                    },
                    "environment": {
                        "INSPECTOR_DATABASE_URL": NESTED_DATABASE_URL,
                        "INSPECTOR_JWT_SECRET": "${INSPECTOR_JWT_SECRET:?fixture secret required}",
                        "INSPECTOR_REDIS_URL": "${INSPECTOR_REDIS_URL:-redis://redis:6379/0}",
                    },
                    "healthcheck": {
                        "test": ["CMD", "fixture-health"], "interval": "30s", "timeout": "3s",
                        "start_period": "90s", "start_interval": "2s", "retries": 4,
                    },
                },
                "scheduler": {
                    "image": "fixture/backend:local",
                    "stop_grace_period": "45s",
                    "depends_on": {"api": {"condition": "service_healthy"}},
                },
                "web": {"image": "fixture/web:local", "depends_on": ["api", "redis"], "ports": ["18080:80"]},
            },
            "volumes": {"fixture-data": {}},
            "networks": {"default": {"driver": "bridge"}},
            "x-fixture": {"note": "preserve unrelated extension"},
        }

    def convert(self, *, external_database=False):
        self.source.write_text(yaml.safe_dump(self.document, sort_keys=False))
        before = self.source.read_bytes()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            compat.convert(self.source, self.destination, external_database=external_database)
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(list(self.destination.parent.glob(".compose-*")))
        return yaml.safe_load(self.destination.read_text())

    def test_top_level_name_removed_and_version_is_string_2_4(self):
        result = self.convert()
        self.assertNotIn("name", result)
        self.assertEqual(result["version"], "2.4")
        self.assertIsInstance(result["version"], str)

    def test_version_added_when_absent(self):
        del self.document["version"]
        self.assertEqual(self.convert()["version"], "2.4")

    def test_pull_policy_removed_from_every_service(self):
        result = self.convert()
        for name, service in result["services"].items():
            with self.subTest(service=name):
                self.assertNotIn("pull_policy", service)
                self.assertEqual(service["image"], self.document["services"][name]["image"])

    def test_start_interval_removed_without_changing_other_health_settings(self):
        result = self.convert()
        for name in ("postgres", "api"):
            with self.subTest(service=name):
                expected = dict(self.document["services"][name]["healthcheck"])
                del expected["start_interval"]
                self.assertEqual(result["services"][name]["healthcheck"], expected)

    def test_only_completed_migration_dependency_removed_and_healthy_dependencies_kept(self):
        result = self.convert()["services"]
        self.assertEqual(result["api"]["depends_on"], {
            "postgres": {"condition": "service_healthy"}, "redis": {"condition": "service_healthy"}
        })
        self.assertEqual(result["migrate"]["depends_on"], {"postgres": {"condition": "service_healthy"}})
        self.assertEqual(result["scheduler"]["depends_on"], {"api": {"condition": "service_healthy"}})
        self.assertEqual(result["migrate"]["command"], ["fixture-migration-never-run"])

    def test_short_form_and_started_dependencies_preserved(self):
        self.document["services"]["scheduler"]["depends_on"]["redis"] = {"condition": "service_started"}
        result = self.convert()["services"]
        self.assertEqual(result["scheduler"]["stop_grace_period"], "45s")
        for name in ("scheduler", "web"):
            self.assertEqual(result[name]["depends_on"], self.document["services"][name]["depends_on"])

    def test_nested_database_default_becomes_flat_compose_v1_interpolation(self):
        result = self.convert()["services"]
        for name in ("api", "migrate"):
            with self.subTest(service=name):
                value = result[name]["environment"]["INSPECTOR_DATABASE_URL"]
                self.assertEqual(value, FLAT_DATABASE_URL)
                self.assertEqual(re.findall(r"\$\{([^{}]+)\}", value), [
                    "POSTGRES_USER:-inspector", "POSTGRES_PASSWORD:?缺少数据库口令", "POSTGRES_DB:-inspector"
                ])
                literal = re.sub(r"\$\{[^{}]+\}", "placeholder", value)
                self.assertNotIn("${", literal)
                self.assertNotIn("}", literal)

    def test_external_database_mode_retains_explicit_variable_reference(self):
        result = self.convert(external_database=True)["services"]
        for name in ("api", "migrate"):
            self.assertEqual(result[name]["environment"]["INSPECTOR_DATABASE_URL"], "${INSPECTOR_DATABASE_URL}")
        self.assertNotIn("postgresql+psycopg://", self.destination.read_text())

    def test_existing_explicit_or_literal_external_database_reference_unchanged(self):
        for value in ("${INSPECTOR_DATABASE_URL}", "postgresql+psycopg://fixture-db.invalid/inspector"):
            for external in (False, True):
                with self.subTest(value=value, external_database=external):
                    self.document["services"]["api"]["environment"]["INSPECTOR_DATABASE_URL"] = value
                    result = self.convert(external_database=external)
                    self.assertEqual(result["services"]["api"]["environment"]["INSPECTOR_DATABASE_URL"], value)

    def test_other_compose_settings_preserved(self):
        expected = copy.deepcopy(self.document)
        del expected["name"]
        expected["version"] = "2.4"
        for service in expected["services"].values():
            service.pop("pull_policy", None)
            service.get("healthcheck", {}).pop("start_interval", None)
        del expected["services"]["api"]["depends_on"]["migrate"]
        for name in ("migrate", "api"):
            expected["services"][name]["environment"]["INSPECTOR_DATABASE_URL"] = FLAT_DATABASE_URL
        self.assertEqual(self.convert(), expected)

    def test_list_form_environment_is_preserved(self):
        environment = ["TZ=UTC", "INSPECTOR_JWT_SECRET=${INSPECTOR_JWT_SECRET}"]
        self.document["services"]["web"]["environment"] = environment
        result = self.convert()
        self.assertEqual(result["services"]["web"]["environment"], environment)

    def test_shared_environment_aliases_convert_consistently(self):
        environment = {"INSPECTOR_DATABASE_URL": NESTED_DATABASE_URL}
        self.document["x-backend"] = {"environment": environment}
        for name in ("api", "migrate", "scheduler"):
            self.document["services"][name]["environment"] = environment
        result = self.convert()
        self.assertEqual(result["x-backend"]["environment"]["INSPECTOR_DATABASE_URL"], FLAT_DATABASE_URL)
        for name in ("api", "migrate", "scheduler"):
            self.assertEqual(result["services"][name]["environment"]["INSPECTOR_DATABASE_URL"], FLAT_DATABASE_URL)

    def test_no_environment_or_dotenv_secret_values_are_emitted(self):
        secrets = {
            "POSTGRES_PASSWORD": "fixture-only-password-not-a-real-secret",
            "INSPECTOR_JWT_SECRET": "fixture-only-jwt-not-a-real-secret",
            "INSPECTOR_DATABASE_URL": "postgresql+psycopg://fixture-only-secret.invalid/fixture",
        }
        dotenv = self.root / ".env"
        dotenv.write_text("".join(f"{name}={value}\n" for name, value in secrets.items()))
        before = dotenv.read_bytes()
        for external in (False, True):
            with self.subTest(external_database=external), patch.dict(os.environ, secrets, clear=True):
                result = self.convert(external_database=external)
                rendered = self.destination.read_text()
                for value in secrets.values():
                    self.assertNotIn(value, rendered)
                self.assertEqual(result["services"]["api"]["env_file"], [".env"])
                self.assertEqual(
                    result["services"]["api"]["environment"]["INSPECTOR_JWT_SECRET"],
                    "${INSPECTOR_JWT_SECRET:?fixture secret required}",
                )
                self.assertEqual(dotenv.read_bytes(), before)

    def test_invalid_top_level_document_rejected_without_output(self):
        for content in ("null\n", "[]\n", "fixture scalar\n"):
            with self.subTest(content=content):
                self.source.write_text(content)
                with self.assertRaisesRegex(ValueError, "invalid Compose document"):
                    compat.convert(self.source, self.destination)
                self.assertFalse(self.destination.exists())

    def test_repeated_conversion_is_stable_and_leaves_no_temporary_files(self):
        first = self.convert()
        before = self.destination.read_bytes()
        self.assertEqual(self.convert(), first)
        self.assertEqual(self.destination.read_bytes(), before)
        self.assertEqual({path.name for path in self.destination.parent.iterdir()}, {self.destination.name})


if __name__ == "__main__":
    unittest.main()
