"""Release-manager regressions using tiny archives and installed dependencies only."""

from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/scripts/release-manager.py"
SPEC = importlib.util.spec_from_file_location("release_manager_under_test", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise ImportError(f"Cannot load {SCRIPT.name}")
manager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manager)
REAL_RUN = subprocess.run
MISSING_DEPENDENCY = "hcs-inspector-fixture-never-installed"


class ReleaseManagerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hcs-release-manager-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.releases = self.root / "releases"
        original_path = sys.path[:]
        self.addCleanup(sys.path.__setitem__, slice(None), original_path)
        self.run = self.enterContext(patch.object(manager.subprocess, "run", side_effect=self.run_dependency_check))
        self.pin = f"pip=={metadata.version('pip')}\n".encode()
        self.files = {
            "backend/app/__init__.py": b"# fixture application\n",
            "backend/migrations/env.py": b"# fixture migration; never executed\n",
            "backend/alembic.ini": b"[alembic]\nscript_location = migrations\n",
            "web/index.html": b"<html>fixture release</html>\n",
            "requirements.txt": self.pin,
            "constraints.txt": self.pin,
        }
        self.package_count = 0

    def run_dependency_check(self, command, **kwargs):
        """Allow only the real, read-only manager check; never pip or Docker."""
        self.assertEqual(command[:3], [sys.executable, str(SCRIPT), "check"])
        self.assertEqual(len(command), 4)
        release = Path(command[3]).resolve()
        self.assertTrue(release.is_relative_to(self.root))
        self.assertTrue(release.is_dir())
        self.assertTrue(kwargs.get("check"))
        self.assertEqual(kwargs["env"]["PYTHONPATH"], str(release / "python"))
        kwargs.update(
            cwd=self.root,
            timeout=15,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                "PATH": os.defpath,
                "HOME": str(self.root),
                "PYTHONPATH": str(release / "python"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "PIP_NO_INDEX": "1",
                "PIP_CONFIG_FILE": os.devnull,
            },
        )
        return REAL_RUN(command, **kwargs)

    def manifest(self, files=None, *, release="fixture-1"):
        files = self.files if files is None else files
        return {
            "format": 1,
            "release": release,
            "platform": "linux/arm64",
            "python": "3.12",
            "dependencies_sha256": hashlib.sha256(files["requirements.txt"] + b"\0" + files["constraints.txt"]).hexdigest(),
            "files": {
                name: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
                for name, content in files.items()
            },
        }

    def package(self, *, files=None, manifest=None, omit=(), include_manifest=True, extra=()):
        files = self.files if files is None else files
        manifest = self.manifest(files) if manifest is None else manifest
        self.package_count += 1
        destination = self.root / f"fixture-{self.package_count}.tar.gz"
        with tarfile.open(destination, "w:gz") as archive:
            for name in ("backend", "backend/app", "backend/migrations", "web"):
                directory = tarfile.TarInfo(name)
                directory.type = tarfile.DIRTYPE
                directory.mode = 0o755
                archive.addfile(directory)
            entries = dict(files)
            if include_manifest:
                entries["manifest.json"] = json.dumps(manifest, sort_keys=True).encode()
            for name, content in entries.items():
                if name not in omit:
                    member = tarfile.TarInfo(name)
                    member.size = len(content)
                    member.mode = 0o644
                    archive.addfile(member, io.BytesIO(content))
            for member, content in extra:
                if member.isfile():
                    member.size = len(content)
                archive.addfile(member, io.BytesIO(content) if member.isfile() else None)
        return destination

    def invoke(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(manager.sys, "argv", [str(SCRIPT), *map(str, arguments)]):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = manager.main()
        return result, stdout.getvalue(), stderr.getvalue()

    def assert_rejected(self, package, message):
        before = {path.name for path in self.releases.iterdir()} if self.releases.exists() else set()
        with self.assertRaisesRegex(ValueError, message):
            manager.prepare(package, self.releases)
        self.assertEqual({path.name for path in self.releases.iterdir()}, before)
        self.assertFalse(list(self.releases.glob(".pending-*")))
        self.run.assert_not_called()

    def snapshot(self, root):
        result = {}
        for path in [root, *sorted(root.rglob("*"))]:
            info = path.lstat()
            content = path.read_bytes() if path.is_file() else None
            result[str(path.relative_to(root))] = (
                content, info.st_mode, info.st_ino, info.st_mtime_ns, info.st_ctime_ns
            )
        return result

    def dependency_root(self, requirements, constraints=""):
        root = self.root / "dependency-check"
        root.mkdir(exist_ok=True)
        (root / "requirements.txt").write_text(requirements)
        (root / "constraints.txt").write_text(constraints)
        return root

    def dist_info(self, root, name, version):
        directory = root / f"{manager.canonicalize_name(name).replace('-', '_')}-{version}.dist-info"
        directory.mkdir(parents=True)
        (directory / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")

    def test_prepare_real_small_archive_and_check_installed_dependencies(self):
        package = self.package()
        self.assertLess(package.stat().st_size, 4096)
        self.assertEqual(manager.prepare(package, self.releases), "fixture-1")
        release = self.releases / "fixture-1"
        self.assertEqual({path.name for path in self.releases.iterdir()}, {"fixture-1"})
        for name, content in self.files.items():
            with self.subTest(file=name):
                self.assertEqual((release / name).read_bytes(), content)
                self.assertEqual(stat.S_IMODE((release / name).stat().st_mode), 0o644)
        self.assertTrue((release / "python").is_dir())
        self.assertEqual(stat.S_IMODE((release / "python").stat().st_mode), 0o755)
        manifest = manager.read_manifest((release / "manifest.json").read_bytes())
        manager.verify_files(release, manifest)
        self.assertEqual(manager.dependency_problems(release), [])
        self.assertEqual(self.invoke("check", release), (0, "", ""))
        self.run.assert_not_called()

    def test_prepare_cli_prints_only_release_identifier(self):
        result, stdout, stderr = self.invoke("prepare", self.package(), self.releases)
        self.assertEqual((result, stdout, stderr), (0, "fixture-1\n", ""))
        self.assertTrue((self.releases / "fixture-1/web/index.html").is_file())
        self.run.assert_not_called()

    def test_file_hash_mismatch_rejected(self):
        manifest = self.manifest()
        files = dict(self.files)
        files["web/index.html"] = files["web/index.html"].replace(b"fixture", b"altered")
        self.assertEqual(len(files["web/index.html"]), manifest["files"]["web/index.html"]["size"])
        self.assert_rejected(self.package(files=files, manifest=manifest), "代码文件校验失败：web/index.html")

    def test_declared_file_size_mismatch_rejected(self):
        manifest = self.manifest()
        manifest["files"]["web/index.html"]["size"] += 1
        self.assert_rejected(self.package(manifest=manifest), "代码文件校验失败：web/index.html")

    def test_dependency_manifest_hash_mismatch_rejected(self):
        manifest = self.manifest()
        manifest["dependencies_sha256"] = "0" * 64
        self.assert_rejected(self.package(manifest=manifest), "依赖清单校验失败")

    def test_duplicate_file_manifest_and_directory_entries_rejected(self):
        for name in ("web/index.html", "manifest.json", "backend/app/"):
            with self.subTest(name=name):
                member = tarfile.TarInfo(name)
                if name.endswith("/"):
                    member.type = tarfile.DIRTYPE
                self.assert_rejected(self.package(extra=[(member, b"duplicate")]), "重复文件、链接或非法路径")

    def test_parent_traversal_and_noncanonical_paths_rejected(self):
        for name in ("../escaped.py", "../../escaped.py", "backend/app/../../../escaped.py",
                     "backend/app//escaped.py", "backend/app/./escaped.py"):
            with self.subTest(name=name):
                self.assert_rejected(
                    self.package(extra=[(tarfile.TarInfo(name), b"untrusted")]), "重复文件、链接或非法路径"
                )
        self.assertFalse((self.root / "escaped.py").exists())

    def test_absolute_archive_path_rejected_without_touching_target(self):
        target = self.root / "untouched.py"
        target.write_bytes(b"keep fixture")
        member = tarfile.TarInfo(str(target))
        self.assert_rejected(self.package(extra=[(member, b"overwrite")]), "重复文件、链接或非法路径")
        self.assertEqual(target.read_bytes(), b"keep fixture")

    def test_symbolic_and_hard_links_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            with self.subTest(kind=kind):
                member = tarfile.TarInfo("backend/app/link.py")
                member.type = kind
                member.linkname = "../../requirements.txt"
                self.assert_rejected(self.package(extra=[(member, b"")]), "重复文件、链接或非法路径")

    def test_missing_manifest_is_clean_cli_failure(self):
        result, stdout, stderr = self.invoke("prepare", self.package(include_manifest=False), self.releases)
        self.assertEqual((result, stdout), (1, ""))
        self.assertIn("代码包未应用：", stderr)
        self.assertIn("manifest.json", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(list(self.releases.iterdir()), [])
        self.run.assert_not_called()

    def test_each_required_file_must_be_listed(self):
        for name in self.files:
            with self.subTest(name=name):
                files = {key: value for key, value in self.files.items() if key != name}
                manifest = self.manifest()
                del manifest["files"][name]
                self.assert_rejected(self.package(files=files, manifest=manifest), "代码包不完整")

    def test_each_listed_required_file_must_be_present_in_archive(self):
        for name in self.files:
            with self.subTest(name=name):
                self.assert_rejected(self.package(omit=[name]), "归档与代码清单不一致")

    def test_unlisted_archive_file_rejected(self):
        member = tarfile.TarInfo("backend/app/unlisted.py")
        self.assert_rejected(self.package(extra=[(member, b"# unlisted\n")]), "归档与代码清单不一致")

    def test_wrong_manifest_platform_rejected(self):
        for platform in ("linux/amd64", "linux/arm/v7", None):
            with self.subTest(platform=platform):
                manifest = self.manifest()
                manifest["platform"] = platform
                self.assert_rejected(self.package(manifest=manifest), "ARM64 / Python 3.12")

    def test_wrong_manifest_python_rejected(self):
        for version in ("3.11", "3.13", None):
            with self.subTest(version=version):
                manifest = self.manifest()
                manifest["python"] = version
                self.assert_rejected(self.package(manifest=manifest), "ARM64 / Python 3.12")

    def test_wrong_runtime_python_rejected_before_creating_release_directory(self):
        package = self.package()
        for version in ((3, 11), (3, 13)):
            with self.subTest(version=version), patch.object(manager.sys, "version_info", version):
                with self.assertRaisesRegex(ValueError, "运行时不是 Python 3.12"):
                    manager.prepare(package, self.releases)
        self.assertFalse(self.releases.exists())
        self.run.assert_not_called()

    def test_release_identifier_cannot_be_overwritten_with_different_content(self):
        manager.prepare(self.package(), self.releases)
        release = self.releases / "fixture-1"
        before = self.snapshot(release)
        files = {**self.files, "web/index.html": b"<html>replacement release</html>\n"}
        self.assert_rejected(self.package(files=files), "该版本标识已被其他内容使用")
        self.assertEqual(self.snapshot(release), before)

    def test_same_release_is_checked_in_real_child_without_rewriting_files(self):
        package = self.package()
        manager.prepare(package, self.releases)
        release = self.releases / "fixture-1"
        marker = release / "python/deployment-marker.txt"
        marker.write_bytes(b"installed overlay fixture")
        marker.chmod(0o444)
        os.utime(marker, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
        before = self.snapshot(release)
        with patch.object(manager, "prepare_python", side_effect=AssertionError("published dependencies must stay immutable")):
            self.assertEqual(manager.prepare(package, self.releases), "fixture-1")
        self.assertEqual(self.snapshot(release), before)
        self.assertEqual({path.name for path in self.releases.iterdir()}, {"fixture-1"})
        self.run.assert_called_once()
        self.assertEqual(self.run.call_args.args[0], [sys.executable, str(SCRIPT), "check", str(release)])

    def test_same_release_rechecks_overlay_dependencies_and_preserves_failed_release(self):
        package = self.package()
        manager.prepare(package, self.releases)
        release = self.releases / "fixture-1"
        self.dist_info(release / "python", "pip", "0")
        before = self.snapshot(release)
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            manager.prepare(package, self.releases)
        self.assertIn("离线依赖校验失败：pip", caught.exception.stderr)
        self.assertEqual(self.snapshot(release), before)
        self.assertEqual({path.name for path in self.releases.iterdir()}, {"fixture-1"})
        self.run.assert_called_once()

    def test_same_release_rejects_tampered_published_file_without_repairing_it(self):
        package = self.package()
        manager.prepare(package, self.releases)
        release = self.releases / "fixture-1"
        (release / "web/index.html").write_bytes(b"<html>tampered release</html>\n")
        before = self.snapshot(release)
        self.assert_rejected(package, "代码文件校验失败：web/index.html")
        self.assertEqual(self.snapshot(release), before)

    def test_bad_new_packages_leave_deployed_old_release_and_pointer_unchanged(self):
        manager.prepare(self.package(), self.releases)
        old = self.releases / "fixture-1"
        current = self.root / "current"
        current.symlink_to(old, target_is_directory=True)
        before = self.snapshot(old)
        bad_hash = self.manifest(release="fixture-2")
        bad_hash["files"]["web/index.html"]["sha256"] = "0" * 64
        missing_dependency = {**self.files, "requirements.txt": f"{MISSING_DEPENDENCY}==1\n".encode()}
        cases = [
            (self.package(manifest=bad_hash), "代码文件校验失败"),
            (self.package(extra=[(tarfile.TarInfo("../../escaped.py"), b"bad")]), "非法路径"),
            (self.package(files=missing_dependency, manifest=self.manifest(missing_dependency, release="fixture-2")),
             "--with-wheels"),
        ]
        for package, message in cases:
            with self.subTest(package=package.name):
                self.assert_rejected(package, message)
                self.assertEqual(self.snapshot(old), before)
                self.assertEqual(current.readlink(), old)
                self.assertEqual(current.resolve(), old)
        self.assertFalse((self.root / "escaped.py").exists())

    def test_unknown_dependency_without_wheels_fails_explicitly_without_installing(self):
        self.assertEqual(list(metadata.distributions(name=MISSING_DEPENDENCY)), [])
        files = {**self.files, "requirements.txt": f"{MISSING_DEPENDENCY}==1\n".encode()}
        wheels = tarfile.TarInfo("wheels")
        wheels.type = tarfile.DIRTYPE
        for extra in ([], [(wheels, b"")]):
            with self.subTest(empty_wheels=bool(extra)):
                self.assert_rejected(
                    self.package(files=files, extra=extra), "--with-wheels.*目标机不会联网安装依赖"
                )

    def test_check_cli_names_unknown_dependency_without_installing(self):
        root = self.dependency_root(f"{MISSING_DEPENDENCY}==1\n", self.pin.decode())
        result, stdout, stderr = self.invoke("check", root)
        self.assertEqual((result, stdout), (1, ""))
        self.assertIn(f"离线依赖校验失败：{MISSING_DEPENDENCY}", stderr)
        self.assertNotIn("Traceback", stderr)
        self.run.assert_not_called()

    def test_installed_dependency_constraint_mismatch_is_reported(self):
        root = self.dependency_root(self.pin.decode(), "pip==0\n")
        self.assertEqual(manager.dependency_problems(root), ["pip"])
        self.run.assert_not_called()

    def test_absent_constraint_only_package_is_not_required(self):
        root = self.dependency_root(self.pin.decode(), f"{MISSING_DEPENDENCY}==1\n")
        self.assertEqual(manager.dependency_problems(root), [])
        self.run.assert_not_called()

    def test_overlay_metadata_version_wins_over_duplicate_base_distribution(self):
        name = "Hcs-Inspector_Fixture.Overlay"
        root = self.dependency_root("hcs.inspector-fixture_overlay==2\n", "hcs-inspector-fixture-overlay==2\n")
        overlay, base = root / "python", self.root / "base-site-packages"
        self.dist_info(overlay, name, "2")
        self.dist_info(base, name, "1")
        versions = [distribution.version for distribution in metadata.distributions(path=[str(overlay), str(base)])]
        self.assertEqual(versions, ["2", "1"])
        sys.path.insert(0, str(base))
        manager.prepare_python(root)
        self.assertLess(sys.path.index(str(overlay)), sys.path.index(str(base)))
        self.assertEqual(manager.dependency_problems(root), [])
        self.run.assert_not_called()

    def test_incompatible_overlay_is_not_hidden_by_matching_base_distribution(self):
        name = "Hcs-Inspector_Fixture.Overlay"
        root = self.dependency_root("hcs-inspector-fixture-overlay==2\n")
        overlay, base = root / "python", self.root / "base-site-packages"
        self.dist_info(overlay, name, "1")
        self.dist_info(base, name, "2")
        sys.path[:0] = [str(overlay), str(base)]
        self.assertEqual(manager.dependency_problems(root), ["hcs-inspector-fixture-overlay"])
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
