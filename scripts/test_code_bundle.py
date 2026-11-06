"""Isolated code-bundle tests: no real npm, Docker, service, or HCS access."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("code_bundle", Path(__file__).with_name("code_bundle.py"))
cb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cb)


class CodeBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "checkout"
        self.root.mkdir()
        files = {
            "backend/pyproject.toml": (
                '[project]\nname = "fixture"\nversion = "9.8.7"\n'
                'dependencies = ["fastapi==0.115.0", "httpx>=0.27; python_version >= \'3.12\'"]\n'
                '[project.optional-dependencies]\npostgres = ["psycopg[binary]==3.2.3"]\n'
                'dev = ["pytest==8.0"]\n'
            ),
            "backend/constraints.txt": "fastapi==0.115.0\npsycopg==3.2.3\n",
            "backend/app/main.py": "print('fixture')\n",
            "backend/app/nested/helper.py": "VALUE = 1\n",
            "backend/migrations/env.py": "# migration runner\n",
            "backend/migrations/versions/001_first.py": "# fixture migration\n",
            "backend/alembic.ini": "[alembic]\nscript_location = migrations\n",
            "frontend/dist/index.html": "<html>fixture</html>\n",
            "frontend/dist/assets/index.js": "console.log('fixture');\n",
            "frontend/package.json": '{"scripts":{"build":"fixture-only"}}\n',
            "customer-data/actual.db": "do not inspect or include",
            "frontend/src/main.ts": "source must not ship",
            "backend/app/__pycache__/main.pyc": "excluded",
            "backend/app/tests/test_private.py": "excluded",
            "backend/app/test_main.py": "excluded",
            "backend/app/.env": "excluded",
            "backend/app/.env.production": "excluded",
            "backend/app/secrets/token.txt": "excluded",
            "backend/app/certs/cert.pem": "excluded",
            "backend/app/private.key": "excluded",
            "backend/app/conftest.py": "excluded",
            "backend/app/data/db.sqlite3": "excluded",
            "backend/app/logs/run.log": "excluded",
            "frontend/dist/assets/index.js.map": "excluded",
            "frontend/dist/node_modules/some/index.js": "excluded",
        }
        for name, value in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        (self.root / "backend/app/main.py").chmod(0o700)

    def build(self, **kwargs):
        return cb.build_bundle(
            self.root, release=kwargs.pop("release", "test-1"), skip_web_build=kwargs.pop("skip_web_build", True), **kwargs
        )

    def payload(self, archive):
        with tarfile.open(archive) as handle:
            return {m.name: handle.extractfile(m).read() for m in handle.getmembers() if m.isfile()}

    def test_payload_manifest_dependencies_modes_checksum_and_source_permissions(self):
        with patch.object(cb.subprocess, "run") as run:
            archive = self.build()
        run.assert_not_called()
        self.assertEqual(archive.parent, self.root / "deploy/dist")
        self.assertEqual(archive.name, "hcs-inspector-code-test-1.tar.gz")
        files = self.payload(archive)
        self.assertEqual(
            set(files),
            {
                "backend/app/main.py",
                "backend/app/nested/helper.py",
                "backend/migrations/env.py",
                "backend/migrations/versions/001_first.py",
                "backend/alembic.ini",
                "web/index.html",
                "web/assets/index.js",
                "requirements.txt",
                "constraints.txt",
                "manifest.json",
            },
        )
        manifest = json.loads(files["manifest.json"])
        self.assertEqual(set(manifest), {"format", "release", "version", "platform", "python", "dependencies_sha256", "files"})
        self.assertEqual(
            [manifest[k] for k in ("format", "release", "version", "platform", "python")],
            [1, "test-1", "9.8.7", "linux/arm64", "3.12"],
        )
        self.assertIn(b"psycopg[binary]==3.2.3\n", files["requirements.txt"])
        self.assertNotIn(b"pytest", files["requirements.txt"])
        self.assertIn(b"python_version >= '3.12'", files["requirements.txt"])
        self.assertEqual(files["constraints.txt"], (self.root / "backend/constraints.txt").read_bytes())
        self.assertEqual(
            manifest["dependencies_sha256"],
            hashlib.sha256(files["requirements.txt"] + b"\0" + files["constraints.txt"]).hexdigest(),
        )
        self.assertEqual(set(manifest["files"]), set(files) - {"manifest.json"})
        for name, meta in manifest["files"].items():
            self.assertEqual(meta, {"sha256": hashlib.sha256(files[name]).hexdigest(), "size": len(files[name])})
        with tarfile.open(archive) as handle:
            for member in handle:
                self.assertTrue(member.isdir() or member.isfile())
                self.assertEqual(member.mode, 0o755 if member.isdir() else 0o644)
                self.assertEqual((member.uid, member.gid, member.mtime), (0, 0, 0))
                self.assertFalse(member.name.startswith("/"))
                self.assertNotIn("..", Path(member.name).parts)
        checksum = archive.with_name(archive.name + ".sha256").read_text()
        self.assertEqual(checksum, f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n")
        self.assertEqual(stat.S_IMODE((self.root / "backend/app/main.py").stat().st_mode), 0o700)
        self.assertFalse(any(p.name.startswith(".code-bundle-") for p in archive.parent.iterdir()))

    def test_default_runs_npm_build(self):
        with patch.object(cb.subprocess, "run") as run:
            self.build(skip_web_build=False)
        run.assert_called_once_with(["npm", "run", "build"], cwd=self.root / "frontend", check=True)

    def test_wheels_are_downloaded_by_existing_arm64_image_and_manifested(self):
        def fake_run(command, **kwargs):
            self.assertEqual(command[:3], ["docker", "run", "--rm"])
            self.assertIn("--pull=never", command)
            self.assertEqual(command[command.index("--platform") + 1], "linux/arm64")
            self.assertEqual(command[command.index("--entrypoint") + 1], "python")
            self.assertIn("local/runtime:fixture", command)
            self.assertIn("--only-binary=:all:", command)
            self.assertEqual(command[-6:], ["-r", "requirements.txt", "-c", "constraints.txt", "--dest", "wheels"])
            mount = command[command.index("--mount") + 1]
            stage = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
            (stage / "wheels/fixture-1.0-py3-none-any.whl").write_bytes(b"fixture wheel")

        with patch.object(cb.subprocess, "run", side_effect=fake_run) as run:
            archive = self.build(with_wheels=True, runtime_image="local/runtime:fixture")
        self.assertEqual(run.call_count, 1)
        files = self.payload(archive)
        self.assertIn("wheels/fixture-1.0-py3-none-any.whl", files)
        self.assertIn("wheels/fixture-1.0-py3-none-any.whl", json.loads(files["manifest.json"])["files"])

    def test_default_runtime_image(self):
        with patch.object(cb, "_run", side_effect=cb.BundleError("stop")) as run:
            with self.assertRaises(cb.BundleError):
                self.build(with_wheels=True)
        self.assertIn("hcs-inspector-arm64-backend:0.4.0", run.call_args.args[0])

    def test_unsafe_releases_rejected_before_build(self):
        for release in ("../escape", "x/y", "", ".", "..", "x y", "x\ny", "你好", "-bad", "a" * 129):
            with self.subTest(release=release), patch.object(cb.subprocess, "run") as run:
                with self.assertRaises(cb.BundleError):
                    self.build(release=release)
                run.assert_not_called()

    def test_safe_release_characters(self):
        self.assertEqual(self.build(release="V0.4.0_rc-1").name, "hcs-inspector-code-V0.4.0_rc-1.tar.gz")

    def test_automatic_release_contains_timestamp_and_content_digest(self):
        archive = self.build(release=None)
        self.assertRegex(archive.name, r"^hcs-inspector-code-\d{8}T\d{6}Z-[0-9a-f]{12}\.tar\.gz$")
        manifest = json.loads(self.payload(archive)["manifest.json"])
        self.assertIn(manifest["release"], archive.name)

    def test_symlinks_rejected_even_under_excluded_directories(self):
        for name in ("backend/app/link.py", "backend/app/__pycache__/link.py", "frontend/dist/assets/link.js"):
            with self.subTest(name=name):
                path = self.root / name
                path.symlink_to(self.root / "backend/app/main.py")
                try:
                    with self.assertRaises(cb.BundleError):
                        self.build()
                finally:
                    path.unlink()
        self.assertEqual(list((self.root / "deploy/dist").iterdir()), [])

    def test_source_directory_symlink_rejected(self):
        source = self.root / "backend/migrations"
        source.rename(source.with_name("real_migrations"))
        source.symlink_to(source.with_name("real_migrations"), target_is_directory=True)
        with self.assertRaises(cb.BundleError):
            self.build()

    def test_source_parent_symlink_rejected(self):
        link = Path(self.temp.name) / "link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(cb.BundleError):
            cb.build_bundle(link, release="fixture", skip_web_build=True)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires FIFO support")
    def test_nonregular_source_rejected(self):
        os.mkfifo(self.root / "backend/app/pipe")
        with self.assertRaises(cb.BundleError):
            self.build()

    def test_output_directory_symlink_rejected(self):
        (self.root / "deploy").mkdir()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.root / "deploy/dist").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(cb.BundleError):
            self.build()
        self.assertEqual(list(outside.iterdir()), [])

    def test_duplicate_release_does_not_overwrite(self):
        output = self.build()
        original = output.read_bytes()
        with self.assertRaises(cb.BundleError):
            self.build()
        self.assertEqual(output.read_bytes(), original)

    def test_missing_constraints_is_actionable(self):
        (self.root / "backend/constraints.txt").unlink()
        with self.assertRaisesRegex(cb.BundleError, "Missing constraints"):
            self.build()

    def test_root_constraints_fallback(self):
        (self.root / "backend/constraints.txt").rename(self.root / "constraints.txt")
        self.assertEqual(self.payload(self.build())["constraints.txt"], b"fastapi==0.115.0\npsycopg==3.2.3\n")

    def test_missing_dist_fails_without_publishing(self):
        shutil.rmtree(self.root / "frontend/dist")
        with self.assertRaises(cb.BundleError):
            self.build()
        self.assertEqual(list((self.root / "deploy/dist").iterdir()), [])

    def test_failed_npm_does_not_publish(self):
        with patch.object(cb.subprocess, "run", side_effect=subprocess.CalledProcessError(2, "npm")):
            with self.assertRaisesRegex(cb.BundleError, "Build command failed"):
                self.build(skip_web_build=False)
        self.assertEqual(list((self.root / "deploy/dist").iterdir()), [])

    def test_empty_wheel_download_rejected(self):
        with patch.object(cb.subprocess, "run"):
            with self.assertRaisesRegex(cb.BundleError, "no wheels"):
                self.build(with_wheels=True)

    def test_same_release_and_source_produce_reproducible_archive(self):
        output = self.build()
        original = output.read_bytes()
        output.with_name(output.name + ".sha256").unlink()
        output.unlink()
        self.assertEqual(self.build().read_bytes(), original)

    def test_cli_help_does_not_touch_sources(self):
        result = subprocess.run([sys.executable, str(Path(cb.__file__)), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--with-wheels", result.stdout)
        self.assertIn("--skip-web-build", result.stdout)

    def test_cli_builds_fixture_from_frontend_and_emits_only_archive_and_sidecar(self):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(Path(cb.__file__)),
                "--root",
                str(self.root),
                "--release",
                "cli-fixture",
                "--skip-web-build",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.root / "deploy/dist/hcs-inspector-code-cli-fixture.tar.gz"
        self.assertEqual(result.stdout.splitlines(), [str(output), str(output) + ".sha256"])
        self.assertEqual({p.name for p in output.parent.iterdir()}, {output.name, output.name + ".sha256"})
        self.assertIn("web/index.html", self.payload(output))
        self.assertFalse(any(name.startswith("frontend/") for name in self.payload(output)))

    def test_npm_result_is_used_when_dist_does_not_exist(self):
        shutil.rmtree(self.root / "frontend/dist")

        def fake_build(command, **kwargs):
            dist = kwargs["cwd"] / "dist"
            dist.mkdir()
            (dist / "index.html").write_bytes(b"freshly built frontend")

        with patch.object(cb.subprocess, "run", side_effect=fake_build):
            output = self.build(skip_web_build=False)
        self.assertEqual(self.payload(output)["web/index.html"], b"freshly built frontend")

    def test_dist_symlink_is_rejected_before_npm_can_write_through_it(self):
        dist = self.root / "frontend/dist"
        dist.rename(dist.with_name("saved_dist"))
        dist.symlink_to(dist.with_name("saved_dist"), target_is_directory=True)
        with patch.object(cb.subprocess, "run") as run:
            with self.assertRaises(cb.BundleError):
                self.build(skip_web_build=False)
        run.assert_not_called()

    def test_symlinked_package_json_is_rejected_before_npm(self):
        package = self.root / "frontend/package.json"
        package.rename(package.with_suffix(".saved"))
        package.symlink_to(package.with_suffix(".saved"))
        with patch.object(cb.subprocess, "run") as run:
            with self.assertRaises(cb.BundleError):
                self.build(skip_web_build=False)
        run.assert_not_called()

    def test_metadata_symlinks_rejected(self):
        for name in ("backend/pyproject.toml", "backend/constraints.txt", "backend/alembic.ini"):
            with self.subTest(name=name):
                path = self.root / name
                saved = path.with_suffix(path.suffix + ".saved")
                path.rename(saved)
                path.symlink_to(saved)
                try:
                    with self.assertRaises(cb.BundleError):
                        self.build()
                finally:
                    path.unlink()
                    saved.rename(path)

    def test_missing_postgres_extra_rejected(self):
        pyproject = self.root / "backend/pyproject.toml"
        pyproject.write_text(pyproject.read_text().replace('postgres = ["psycopg[binary]==3.2.3"]', "other = []"))
        with self.assertRaisesRegex(cb.BundleError, "optional-dependencies.postgres"):
            self.build()

    def test_malformed_optional_dependency_table_rejected_cleanly(self):
        pyproject = self.root / "backend/pyproject.toml"
        pyproject.write_text('[project]\nversion="1"\ndependencies=[]\noptional-dependencies=[]\n')
        with self.assertRaisesRegex(cb.BundleError, "must be a table"):
            self.build()

    def test_multiline_dependency_rejected(self):
        pyproject = self.root / "backend/pyproject.toml"
        pyproject.write_text(
            '[project]\nversion="1"\ndependencies=["fastapi\\n-r secret"]\n[project.optional-dependencies]\npostgres=[]\n'
        )
        with self.assertRaisesRegex(cb.BundleError, "single-line requirement"):
            self.build()

    def test_dependency_digest_changes_with_constraints(self):
        first = json.loads(self.payload(self.build(release="before"))["manifest.json"])
        path = self.root / "backend/constraints.txt"
        path.write_bytes(path.read_bytes() + b"# revised constraints\n")
        second = json.loads(self.payload(self.build(release="after"))["manifest.json"])
        self.assertNotEqual(first["dependencies_sha256"], second["dependencies_sha256"])

    def test_empty_or_asset_only_frontend_rejected(self):
        (self.root / "frontend/dist/index.html").unlink()
        with self.assertRaisesRegex(cb.BundleError, "index.html is missing"):
            self.build()
        self.assertEqual(list((self.root / "deploy/dist").iterdir()), [])

    def test_rejects_output_symlinks_without_touching_target(self):
        out = self.root / "deploy/dist"
        out.mkdir(parents=True)
        sentinel = self.root / "sentinel"
        sentinel.write_bytes(b"untouched")
        for name in ("hcs-inspector-code-test-1.tar.gz", "hcs-inspector-code-test-1.tar.gz.sha256"):
            with self.subTest(name=name):
                link = out / name
                link.symlink_to(sentinel)
                try:
                    with self.assertRaises(cb.BundleError):
                        self.build()
                    self.assertEqual(sentinel.read_bytes(), b"untouched")
                finally:
                    link.unlink()

    def test_unexpected_wheel_entries_rejected_and_temporary_files_removed(self):
        for entry in ("package.tar.gz", "nested.whl", "symlink.whl"):
            with self.subTest(entry=entry):

                def fake_download(command, *, entry=entry, **kwargs):
                    mount = command[command.index("--mount") + 1]
                    stage = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
                    target = stage / "wheels" / entry
                    if entry == "nested.whl":
                        target.mkdir()
                    elif entry == "symlink.whl":
                        target.symlink_to(self.root / "backend/app/main.py")
                    else:
                        target.write_bytes(b"not a wheel")

                with patch.object(cb.subprocess, "run", side_effect=fake_download):
                    with self.assertRaises(cb.BundleError):
                        self.build(with_wheels=True)
                self.assertEqual(list((self.root / "deploy/dist").iterdir()), [])

    def test_frontend_source_link_is_rejected_before_npm(self):
        (self.root / "frontend/src/link.ts").symlink_to(self.root / "customer-data/actual.db")
        with patch.object(cb.subprocess, "run") as run:
            with self.assertRaises(cb.BundleError):
                self.build(skip_web_build=False)
        run.assert_not_called()

    def test_installed_npm_binary_links_are_not_source_or_payload(self):
        binary = self.root / "frontend/node_modules/.bin/vite"
        binary.parent.mkdir(parents=True)
        binary.symlink_to(self.root / "frontend/src/main.ts")
        with patch.object(cb.subprocess, "run") as run:
            output = self.build(skip_web_build=False)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(any("node_modules" in name for name in self.payload(output)))

    def test_wrapper_resolves_root_from_its_own_location(self):
        script = self.root / "scripts/code_bundle.py"
        script.parent.mkdir()
        shutil.copyfile(cb.__file__, script)
        wrapper = self.root / "deploy/scripts/code-bundle.sh"
        wrapper.parent.mkdir(parents=True)
        source = Path(__file__).resolve().parents[1] / "deploy/scripts/code-bundle.sh"
        shutil.copyfile(source, wrapper)
        result = subprocess.run(
            ["sh", str(wrapper), "--release", "wrapper-fixture", "--skip-web-build"],
            cwd=self.temp.name,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.root / "deploy/dist/hcs-inspector-code-wrapper-fixture.tar.gz"
        self.assertTrue(output.is_file())
        self.assertIn("web/index.html", self.payload(output))

    def test_failed_docker_leaves_no_output(self):
        with patch.object(cb.subprocess, "run", side_effect=subprocess.CalledProcessError(125, "docker")):
            with self.assertRaisesRegex(cb.BundleError, "Build command failed"):
                self.build(with_wheels=True)
        self.assertEqual(list((self.root / "deploy/dist").iterdir()), [])

    def test_cli_errors_have_no_traceback(self):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(Path(cb.__file__)),
                "--root",
                str(self.root),
                "--release",
                "../escape",
                "--skip-web-build",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("code-bundle:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
