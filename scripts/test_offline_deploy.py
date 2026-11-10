"""离线部署回归：只使用临时配置和模拟 Docker，不连接正式数据库或云服务。"""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
IMAGES = [
    "hcs-inspector-arm64-backend:0.4.0",
    "hcs-inspector-arm64-web:0.4.0",
    "hcs-inspector-arm64-postgres:16-alpine",
    "hcs-inspector-arm64-redis:7-alpine",
]
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ["FAKE_DOCKER_ROOT"])
if pathlib.Path(sys.argv[0]).name == "docker-compose":
    args = ["compose", *args]
    (root / "standalone-used").touch()
elif args[:1] == ["compose"] and os.environ.get("FAKE_NO_PLUGIN"):
    sys.exit(1)
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\n")
if args[:2] == ["compose", "version"]:
    print(os.environ.get("FAKE_COMPOSE_VERSION", "2.40.3"))
elif args[:1] == ["info"]:
    if "--format" in args:
        print(os.environ.get("FAKE_ARCH", "linux/aarch64"))
elif args[:2] == ["image", "inspect"]:
    name = args[-1]
    if name not in json.loads(os.environ["FAKE_IMAGES"]):
        sys.exit(1)
    loaded = (root / "loaded").exists()
    if name == os.environ.get("FAKE_MISSING") and not loaded:
        sys.exit(1)
    if "--format" in args:
        identity = "sha256:test" if loaded or name != os.environ.get("FAKE_STALE") else "sha256:old"
        arch = "linux/amd64" if os.environ.get("FAKE_WRONG_IMAGE_ARCH") else "linux/arm64"
        print(identity + " " + arch)
elif args[:1] == ["load"]:
    sys.stdin.buffer.read()
    (root / "loaded").touch()
elif args[:1] == ["run"] and "/manager.py" in args:
    mounts = {}
    for i, arg in enumerate(args):
        if arg == "-v":
            parts = args[i+1].split(":")
            mounts[parts[1]] = parts[0]
    if pathlib.Path(mounts["/manager.py"]).name != "compose-compat.py":
        sys.exit("unexpected helper")
    import subprocess
    result = subprocess.run([os.environ["FAKE_HELPER_PYTHON"], mounts["/manager.py"], mounts["/input.yml"],
                             str(pathlib.Path(mounts["/out"]) / pathlib.Path(args[-2]).name), args[-1]], check=False)
    sys.exit(result.returncode)
elif args[:1] == ["compose"] and args[-2:] == ["config", "--images"]:
    if os.environ.get("FAKE_COMPOSE_VERSION", "2").startswith("1."):
        sys.exit("Compose v1 has no config --images")
    print("\n".join(json.loads(os.environ["FAKE_IMAGES"])))
elif args[:1] == ["compose"] and args[-1:] == ["config"]:
    if os.environ.get("FAKE_CONFIG_TEXT"):
        print(os.environ["FAKE_CONFIG_TEXT"])
        sys.exit(0)
    print("services:")
    for index, image in enumerate(json.loads(os.environ["FAKE_IMAGES"])):
        print(f"  service{index}:\n    environment:\n      PRIVATE: synthetic-private-value\n    image: '{image}'")
    print("version: '2.4'")
else:
    sys.exit("unexpected Docker command")
'''


class OfflineDeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hcs-offline-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.deploy = self.root / "deploy"
        self.deploy.mkdir()
        shutil.copytree(ROOT / "deploy/scripts", self.deploy / "scripts")
        for name in ("deploy.sh", "install.sh", ".env.example", "docker-compose.yml", "docker-compose.offline.yml",
                     "docker-compose.code.yml", "docker-compose.https.yml"):
            shutil.copy2(ROOT / "deploy" / name, self.deploy / name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(FAKE_DOCKER)
        (self.bin / "docker").chmod(0o755)
        (self.bin / "docker-compose").write_text(FAKE_DOCKER)
        (self.bin / "docker-compose").chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("INSPECTOR_", "COMPOSE_", "FAKE_"))}
        self.env.update(PATH=f"{self.bin}:{os.environ['PATH']}", WEB_PORT="18081", INSPECTOR_BASE_URL="http://127.0.0.1:18081",
                        FAKE_DOCKER_ROOT=str(self.root), FAKE_IMAGES=json.dumps(IMAGES), FAKE_HELPER_PYTHON=sys.executable)
        self.env.pop("HCS_ALLOW_EMULATION", None)

    def run_bash(self, code, *, env=None):
        return subprocess.run(["bash", "-c", code], cwd=self.deploy, env=env or self.env, capture_output=True,
                              text=True, timeout=15, check=False)

    def generate(self):
        result = self.run_bash("bash scripts/gen-env.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        return (self.deploy / ".env").read_bytes()

    def bundle(self):
        (self.deploy / "bundle.env").write_text(
            "HCS_INSPECTOR_VERSION=0.4.0\nIMAGE_PREFIX=hcs-inspector-arm64\n"
            "POSTGRES_IMAGE=hcs-inspector-arm64-postgres:16-alpine\n"
            "REDIS_IMAGE=hcs-inspector-arm64-redis:7-alpine\nOFFLINE_PLATFORM=linux/arm64\n")
        images = self.deploy / "images"
        images.mkdir()
        (images / "PLATFORM").write_text("linux/arm64\n")
        (images / "manifest.tsv").write_text("".join(f"{name}\tsha256:test\tlinux/arm64\n" for name in IMAGES))
        import gzip
        (images / "images.tar.gz").write_bytes(gzip.compress(b"synthetic Docker archive"))
        (images / "SHA256SUMS").write_text("".join(
            f"{hashlib.sha256((images / name).read_bytes()).hexdigest()}  {name}\n"
            for name in ("PLATFORM", "manifest.tsv", "images.tar.gz")))

    def calls(self):
        file = self.root / "calls.jsonl"
        return [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []

    def test_scripts_have_valid_bash_syntax(self):
        for file in [self.deploy / "deploy.sh", self.deploy / "install.sh", *(self.deploy / "scripts").glob("*.sh")]:
            with self.subTest(file=file.name):
                self.assertEqual(subprocess.run(["bash", "-n", str(file)], capture_output=True, check=False).returncode, 0)

    def test_generation_production_secrets_permissions_and_idempotent_refusal(self):
        before = self.generate()
        values = dict(line.split("=", 1) for line in before.decode().splitlines() if line and not line.startswith("#") and "=" in line)
        self.assertEqual(values["INSPECTOR_ENV"], "prod")
        self.assertEqual(values["INSPECTOR_DEMO_SEED"], "false")
        self.assertEqual(values["WEB_PORT"], "18081")
        self.assertEqual(values["INSPECTOR_BASE_URL"], "http://127.0.0.1:18081")
        self.assertEqual(len(base64.b64decode(values["INSPECTOR_MASTER_KEY"])), 32)
        self.assertGreaterEqual(len(values["INSPECTOR_JWT_SECRET"]), 64)
        self.assertNotIn(b"__GEN_", before)
        self.assertEqual((self.deploy / ".env").stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(self.run_bash("bash scripts/gen-env.sh").returncode, 0)
        self.assertEqual((self.deploy / ".env").read_bytes(), before)

    def test_bad_port_does_not_leave_partial_configuration(self):
        for port in ("0", "65536", "abc", "80&bad"):
            with self.subTest(port=port):
                result = self.run_bash("bash scripts/gen-env.sh", env={**self.env, "WEB_PORT": port})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.deploy / ".env").exists())
                self.assertFalse(list(self.deploy.glob(".env.new.*")))

    def test_bad_url_does_not_leave_partial_configuration(self):
        for url in ("javascript:bad", "http://host|bad", "http://host\nBAD=1", "http://host&bad"):
            with self.subTest(url=url):
                result = self.run_bash("bash scripts/gen-env.sh", env={**self.env, "INSPECTOR_BASE_URL": url})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.deploy / ".env").exists())

    def test_read_configuration_never_executes_it(self):
        (self.deploy / ".env").write_text("CUSTOM='$(touch compromised)'\n")
        result = self.run_bash("source scripts/common.sh; env_get CUSTOM")
        self.assertEqual(result.stdout, "$(touch compromised)")
        self.assertFalse((self.deploy / "compromised").exists())

    def test_active_code_release_supplies_its_compose_overlay_with_legacy_fallback(self):
        release = self.deploy / "releases/code-release"
        release.mkdir(parents=True)
        overlay = release / "docker-compose.code.yml"
        overlay.write_text("services:\n  scheduler:\n    stop_grace_period: 37s\n")
        (self.deploy / ".code.env").write_text(f"HCS_CODE_DIR={release}\n")
        (self.deploy / ".env").write_text("IMAGE_PREFIX=hcs-inspector-arm64\nHCS_INSPECTOR_VERSION=0.4.0\n")

        result = self.run_bash("source scripts/common.sh; dc config",
                               env={**self.env, "FAKE_COMPOSE_VERSION": "1.29.2"})

        self.assertEqual(result.returncode, 0, result.stderr)
        compose_calls = [call for call in self.calls() if call[:1] == ["compose"] and call[-1:] == ["config"]]
        latest_call = compose_calls[-1]
        files = [latest_call[index + 1] for index, arg in enumerate(latest_call[:-1]) if arg == "-f"]
        self.assertTrue(files[-1].startswith(".compose-legacy/"))
        converted = [yaml.safe_load(path.read_text()) for path in (self.deploy / ".compose-legacy").glob("*.yml")]
        self.assertTrue(any(document["services"]["scheduler"].get("stop_grace_period") == "37s"
                            for document in converted))

        overlay.unlink()
        result = self.run_bash("source scripts/common.sh; dc config",
                               env={**self.env, "FAKE_COMPOSE_VERSION": "1.29.2"})
        self.assertEqual(result.returncode, 0, result.stderr)
        compose_calls = [call for call in self.calls() if call[:1] == ["compose"] and call[-1:] == ["config"]]
        latest_call = compose_calls[-1]
        files = [latest_call[index + 1] for index, arg in enumerate(latest_call[:-1]) if arg == "-f"]
        self.assertTrue(files[-1].startswith(".compose-legacy/"))
        converted = [yaml.safe_load(path.read_text()) for path in (self.deploy / ".compose-legacy").glob("*.yml")]
        self.assertTrue(any(document["services"].get("scheduler", {}).get("stop_grace_period") == "45s"
                            for document in converted))

    def test_default_configuration_only_fills_missing_files(self):
        defaults = self.deploy / "defaults/nginx/snippets"
        defaults.mkdir(parents=True)
        (defaults / "allowlist.conf").write_text("default\n")
        (defaults / "hcs-app.conf").write_text("new file\n")
        existing = self.deploy / "nginx/snippets"
        existing.mkdir(parents=True)
        (existing / "allowlist.conf").write_text("synthetic existing access policy\n")
        for _ in range(2):
            result = self.run_bash("source scripts/common.sh; prepare_configs")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((existing / "allowlist.conf").read_text(), "synthetic existing access policy\n")
            self.assertEqual((existing / "hcs-app.conf").read_text(), "new file\n")

    def test_shell_demo_compose_override_is_rejected(self):
        before = self.generate()
        result = self.run_bash("bash deploy.sh", env={**self.env, "COMPOSE_FILE": "docker-compose.yml:docker-compose.demo.yml"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.deploy / ".env").read_bytes(), before)

    def test_complete_matching_images_skip_import_but_check_all_four(self):
        self.generate()
        self.bundle()
        result = self.run_bash("source scripts/common.sh; ensure_images")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call[:1] == ["load"] for call in self.calls()))
        self.assertEqual({call[-1] for call in self.calls() if call[:2] == ["image", "inspect"]}, set(IMAGES))

    def test_standalone_compose_works_without_plugin(self):
        self.generate()
        self.bundle()
        (self.bin / "docker-compose").write_text(FAKE_DOCKER)
        (self.bin / "docker-compose").chmod(0o755)
        result = self.run_bash("source scripts/common.sh; need_docker; ensure_images",
                               env={**self.env, "FAKE_NO_PLUGIN": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "standalone-used").exists())
        self.assertFalse(any("--env-file" in call for call in self.calls()))

    def test_compose_plugin_alone_is_not_accepted(self):
        (self.bin / "docker-compose").unlink()
        result = self.run_bash("source scripts/common.sh; select_compose", env={**self.env, "FAKE_NO_PLUGIN": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("独立 docker-compose", result.stderr)
        self.assertFalse((self.root / "standalone-used").exists())

    def test_standalone_v1_uses_compatible_config_without_plugin(self):
        self.generate()
        self.bundle()
        (self.bin / "docker-compose").write_text(FAKE_DOCKER)
        (self.bin / "docker-compose").chmod(0o755)
        result = self.run_bash("source scripts/common.sh; need_docker; ensure_images",
                               env={**self.env, "FAKE_NO_PLUGIN": "1", "FAKE_COMPOSE_VERSION": "1.29.2"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any("--images" in call for call in self.calls()))
        self.assertFalse(any(call[:1] == ["load"] for call in self.calls()))
        self.assertNotIn("synthetic-private-value", result.stdout + result.stderr)
        cached = list((self.deploy / ".compose-legacy").glob("*.yml"))
        self.assertEqual(len(cached), 2)
        for file in cached:
            import yaml
            value = yaml.safe_load(file.read_text())
            self.assertEqual(value["version"], "2.4")
            self.assertNotIn("name", value)
            self.assertTrue(all("pull_policy" not in service for service in value["services"].values()))

    def test_legacy_image_extraction_handles_quotes_and_keeps_configuration_private(self):
        (self.bin / "docker-compose").write_text(FAKE_DOCKER)
        (self.bin / "docker-compose").chmod(0o755)
        text = ('services:\n  api:\n    environment:\n      PRIVATE: synthetic-private-value\n'
                '      image: nested-value-is-not-an-image\n'
                '    image: "localhost:5000/inspector/backend:0.4.0"\n'
                "  web:\n    image: 'inspector/web:0.4.0'\n"
                "volumes:\n  local:\n    image: unrelated-extension\nversion: '2.4'\n")
        result = self.run_bash("source scripts/common.sh; select_compose; compose_config_images",
            env={**self.env, "FAKE_NO_PLUGIN": "1", "FAKE_COMPOSE_VERSION": "1.29.2", "FAKE_CONFIG_TEXT": text})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["localhost:5000/inspector/backend:0.4.0", "inspector/web:0.4.0"])
        self.assertNotIn("synthetic-private-value", result.stdout + result.stderr)

    def test_legacy_image_extraction_refuses_empty_or_invalid_reference(self):
        (self.bin / "docker-compose").write_text(FAKE_DOCKER)
        (self.bin / "docker-compose").chmod(0o755)
        for text in ("services:\n  api:\n    image: 'bad image name'\n", "services: {}\n"):
            with self.subTest(text=text):
                result = self.run_bash("source scripts/common.sh; select_compose; compose_config_images",
                    env={**self.env, "FAKE_NO_PLUGIN": "1", "FAKE_COMPOSE_VERSION": "1.29.2", "FAKE_CONFIG_TEXT": text})
                self.assertNotEqual(result.returncode, 0)

    def test_unsupported_old_compose_fails_before_import(self):
        self.generate()
        self.bundle()
        (self.bin / "docker-compose").write_text(FAKE_DOCKER)
        (self.bin / "docker-compose").chmod(0o755)
        result = self.run_bash("source scripts/common.sh; need_docker; ensure_images",
                               env={**self.env, "FAKE_NO_PLUGIN": "1", "FAKE_COMPOSE_VERSION": "1.28.0"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call[:1] == ["load"] for call in self.calls()))

    def test_missing_database_or_redis_image_triggers_offline_import(self):
        for image in IMAGES[2:]:
            with self.subTest(image=image):
                self.generate() if not (self.deploy / ".env").exists() else None
                self.bundle() if not (self.deploy / "bundle.env").exists() else None
                (self.root / "loaded").unlink(missing_ok=True)
                result = self.run_bash("source scripts/common.sh; ensure_images", env={**self.env, "FAKE_MISSING": image})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((self.root / "loaded").exists())
        self.assertFalse(any("pull" in call or "build" in call for call in self.calls()))

    def test_existing_same_tag_with_old_id_is_reloaded(self):
        self.generate()
        self.bundle()
        result = self.run_bash("source scripts/common.sh; ensure_images", env={**self.env, "FAKE_STALE": IMAGES[0]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "loaded").exists())

    def test_manifest_accepts_verified_legacy_config_id(self):
        self.generate()
        self.bundle()
        manifest = self.deploy / "images/manifest.tsv"
        manifest.write_text(manifest.read_text().replace("sha256:test", "sha256:manifest,sha256:test"))
        images = self.deploy / "images"
        (images / "SHA256SUMS").write_text("".join(
            f"{hashlib.sha256((images / name).read_bytes()).hexdigest()}  {name}\n"
            for name in ("PLATFORM", "manifest.tsv", "images.tar.gz")))
        result = self.run_bash("source scripts/common.sh; ensure_images")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call[:1] == ["load"] for call in self.calls()))

    def test_docker_save_manifest_records_verified_config_digest(self):
        import io
        import tarfile
        self.bundle()
        raw = json.dumps({"os": "linux", "architecture": "arm64", "config": {"Cmd": ["synthetic"]}}).encode()
        digest = hashlib.sha256(raw).hexdigest()
        saved = [{"Config": digest + ".json", "RepoTags": IMAGES, "Layers": []}]
        archive = self.deploy / "images/images.tar.gz"
        with tarfile.open(archive, "w:gz") as out:
            for name, content in ((digest + ".json", raw), ("manifest.json", json.dumps(saved).encode())):
                info = tarfile.TarInfo(name)
                info.size = len(content)
                out.addfile(info, io.BytesIO(content))
        result = subprocess.run([sys.executable, str(ROOT / "deploy/scripts/image-manifest.py"), str(archive.parent)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [line.split("\t") for line in (archive.parent / "manifest.tsv").read_text().splitlines()]
        self.assertEqual({row[0] for row in rows}, set(IMAGES))
        self.assertTrue(all(row[1] == f"sha256:test,sha256:{digest}" for row in rows))

    def test_wrong_image_architecture_is_rejected_even_after_load(self):
        self.generate()
        self.bundle()
        result = self.run_bash("source scripts/common.sh; ensure_images", env={**self.env, "FAKE_WRONG_IMAGE_ARCH": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("架构", result.stderr)

    def test_corrupt_archive_fails_before_import(self):
        self.generate()
        self.bundle()
        with (self.deploy / "images/images.tar.gz").open("ab") as file:
            file.write(b"corrupt")
        result = self.run_bash("source scripts/common.sh; ensure_images")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call[:1] == ["load"] for call in self.calls()))

    def test_x86_host_rejected_before_creating_configuration_or_import(self):
        self.bundle()
        result = self.run_bash("bash deploy.sh", env={**self.env, "FAKE_ARCH": "linux/x86_64"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.deploy / ".env").exists())
        self.assertFalse(any(call[:1] == ["load"] for call in self.calls()))

    def test_production_guard_preserves_existing_configuration(self):
        original = self.generate()
        for line in ("INSPECTOR_ENV=dev", "INSPECTOR_DEMO_SEED=true", "INSPECTOR_HCS_MOCK_URL=http://mock:9000",
                     "COMPOSE_FILE=docker-compose.yml:docker-compose.demo.yml"):
            with self.subTest(line=line):
                content = original + f"\n{line}\n".encode()
                (self.deploy / ".env").write_bytes(content)
                result = self.run_bash("bash deploy.sh")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.deploy / ".env").read_bytes(), content)
                self.assertFalse(any(call[:1] == ["load"] for call in self.calls()))

    def test_existing_production_configuration_untouched_by_entrypoint(self):
        before = self.generate()
        # 只隔离测试入口的分发和幂等保留；完整服务启动另由 ARM64 容器验收。
        (self.deploy / "scripts/install.sh").write_text("#!/usr/bin/env bash\ntouch installed\n")
        for _ in range(2):
            result = self.run_bash("bash deploy.sh")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((self.deploy / ".env").read_bytes(), before)
        self.assertTrue((self.deploy / "installed").exists())

    def test_existing_database_without_environment_refuses_key_regeneration(self):
        (self.deploy / "data/postgres").mkdir(parents=True)
        (self.deploy / "data/postgres/PG_VERSION").write_text("16\n")
        result = self.run_bash("bash deploy.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.deploy / ".env").exists())

    def test_cancel_restore_with_environment_preserves_original_secrets(self):
        import tarfile
        before = self.generate()
        backup = self.root / "backup"
        backup.mkdir()
        (backup / "db.dump").write_bytes(b"synthetic dump")
        (backup / "env").write_text("INSPECTOR_MASTER_KEY=synthetic-different-key\n")
        archive = self.deploy / "synthetic-backup.tar.gz"
        with tarfile.open(archive, "w:gz") as out:
            out.add(backup / "db.dump", arcname="db.dump")
            out.add(backup / "env", arcname="env")
        result = self.run_bash("printf 'no\\n' | bash scripts/restore.sh synthetic-backup.tar.gz --with-env")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("已取消", result.stderr)
        self.assertEqual((self.deploy / ".env").read_bytes(), before)
        self.assertFalse(list(self.deploy.glob(".env.before-restore.*")))

    def test_external_database_backup_and_restore_refuse_wrong_local_database(self):
        self.generate()
        with (self.deploy / ".env").open("a") as file:
            file.write("\nINSPECTOR_DATABASE_URL=postgresql+psycopg://test:test@database.example.invalid/prod\n")
        for action in ("backup", "restore"):
            with self.subTest(action=action):
                result = self.run_bash(f"bash scripts/{action}.sh")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("显式数据库", result.stderr)
                self.assertFalse(any(call[:1] == ["compose"] and "version" not in call for call in self.calls()))

    def test_build_refuses_unsupported_arch_and_demo_bundle(self):
        for args in ("--platform linux/arm/v7", "--with-demo", "--platform"):
            with self.subTest(args=args):
                result = self.run_bash(f"bash scripts/offline-bundle.sh {args}")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls(), [])

    @unittest.skipUnless(shutil.which("docker-compose"), "需要独立 docker-compose 做配置解析")
    def test_rendered_offline_compose_preserves_production_settings(self):
        self.generate()
        self.bundle()
        with (self.deploy / ".env").open("a") as file:
            file.write("\nINSPECTOR_DATABASE_URL=postgresql+psycopg://test:test@database.example.invalid/prod\n"
                       "INSPECTOR_REDIS_URL=redis://cache.example.invalid/4\nDATA_DIR=/tmp/synthetic-existing-data\n")
        real_env = {**self.env, "PATH": os.environ["PATH"]}
        result = self.run_bash("source scripts/common.sh; dc config --format json", env=real_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)["services"]
        self.assertEqual(set(services), {"api", "web", "postgres", "redis", "migrate", "scheduler", "notifier"})
        for name, service in services.items():
            with self.subTest(service=name):
                self.assertEqual(service["pull_policy"], "never")
                self.assertEqual(service["platform"], "linux/arm64")
                self.assertIn(service["image"], IMAGES)
                if name in {"api", "migrate", "scheduler", "notifier"}:
                    self.assertEqual(service["environment"]["INSPECTOR_ENV"], "prod")
                    self.assertEqual(service["environment"]["INSPECTOR_DEMO_SEED"], "false")
                    self.assertEqual(service["environment"]["INSPECTOR_DATABASE_URL"],
                                     "postgresql+psycopg://test:test@database.example.invalid/prod")
                    self.assertEqual(service["environment"]["INSPECTOR_REDIS_URL"], "redis://cache.example.invalid/4")
        self.assertEqual(services["postgres"]["volumes"][0]["source"], "/tmp/synthetic-existing-data/postgres")


class FirstInstallTests(unittest.TestCase):
    """真实执行首次安装入口；只替换服务启动，验证复制、路径和防覆盖行为。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hcs-first-install-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "transfer files"
        self.source.mkdir()
        shutil.copy2(ROOT / "deploy/install.sh", self.source / "install.sh")
        for name in (".env.example", "bundle.env", "Makefile", "README.md", "docker-compose.yml",
                     "docker-compose.offline.yml", "docker-compose.code.yml", "docker-compose.https.yml"):
            (self.source / name).write_text("synthetic package\n")
        for name in ("scripts", "defaults", "code", "images"):
            (self.source / name).mkdir()
        (self.source / "scripts/common.sh").write_text(
            'need_docker() { [ "${FIRST_INSTALL_NO_DOCKER:-0}" = 0 ]; }\n'
            'check_offline_platform() { [ "${FIRST_INSTALL_WRONG_ARCH:-0}" = 0 ]; }\n')
        for name in ("release-manager.py", "compose-compat.py", "update-code.sh"):
            (self.source / "scripts" / name).write_text("synthetic helper\n")
        (self.source / "deploy.sh").write_text(
            '#!/usr/bin/env bash\nset -eu\nprintf "%s\\n%s\\n" "$PWD" "${WEB_PORT:-80}" > started\n')
        (self.source / "code/release.tar.gz").write_bytes(b"synthetic code")
        (self.source / "images/images.tar.gz").write_bytes(b"synthetic runtime images")
        (self.source / "images/PLATFORM").write_text("linux/arm64\n")
        (self.source / "images/manifest.tsv").write_text("synthetic image manifest\n")
        self.checksums(self.source / "images")
        self.checksums(self.source)
        self.target = self.root / "installation directory"
        self.env = {**os.environ, "WEB_PORT": "18083", "FIRST_INSTALL_NO_DOCKER": "0",
                    "FIRST_INSTALL_WRONG_ARCH": "0"}

    @staticmethod
    def checksums(directory):
        files = sorted(p for p in directory.rglob("*") if p.is_file() and p != directory / "SHA256SUMS")
        (directory / "SHA256SUMS").write_text("".join(
            f"{hashlib.sha256(p.read_bytes()).hexdigest()}  ./{p.relative_to(directory)}\n" for p in files))

    def run_install(self, *args, env=None):
        return subprocess.run(["bash", str(self.source / "install.sh"), *map(str, args)],
                              cwd=self.root, env=env or self.env, capture_output=True,
                              text=True, timeout=15, check=False)

    def test_check_only_reports_real_default_without_docker_or_installing(self):
        result = self.run_install("--check", env={**self.env, "FIRST_INSTALL_NO_DOCKER": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/data/hcs-inspector", result.stdout)
        self.assertFalse((self.source / "started").exists())
        self.assertFalse(self.target.exists())

    def test_first_install_copies_complete_payload_and_starts_in_target(self):
        (self.source / ".env").write_text("synthetic extra configuration; must not be copied")
        (self.source / "unrelated.txt").write_text("must not be copied")
        result = self.run_install("--dir", self.target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.target / "started").read_text().splitlines(), [str(self.target), "18083"])
        self.assertEqual((self.target / "images/images.tar.gz").read_bytes(), b"synthetic runtime images")
        self.assertTrue((self.target / ".env.example").is_file())
        self.assertFalse((self.target / ".env").exists())
        self.assertFalse((self.target / "unrelated.txt").exists())
        self.assertFalse((self.source / "started").exists())
        self.assertFalse(list(self.root.glob(".hcs-inspector-install.*")))

    def test_empty_existing_target_is_supported(self):
        self.target.mkdir()
        result = self.run_install("--dir", self.target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.target / "started").is_file())

    def test_existing_configuration_is_never_overwritten(self):
        self.target.mkdir()
        existing = self.target / ".env"
        existing.write_bytes(b"synthetic existing configuration\n")
        result = self.run_install("--dir", self.target)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不覆盖", result.stderr)
        self.assertEqual(existing.read_bytes(), b"synthetic existing configuration\n")
        self.assertEqual(list(self.target.iterdir()), [existing])

    def test_in_place_retry_uses_original_directory(self):
        result = self.run_install("--dir", self.source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.source / "started").read_text().splitlines()[0], str(self.source))

    def test_corrupt_package_refused_before_target_creation(self):
        (self.source / "code/release.tar.gz").write_bytes(b"corrupt")
        result = self.run_install("--dir", self.target)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.target.exists())

    def test_missing_runtime_is_refused_before_target_creation(self):
        (self.source / "images/images.tar.gz").unlink()
        result = self.run_install("--dir", self.target)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("缺失", result.stderr)
        self.assertFalse(self.target.exists())

    def test_no_docker_or_wrong_arch_does_not_create_target(self):
        for setting in ("FIRST_INSTALL_NO_DOCKER", "FIRST_INSTALL_WRONG_ARCH"):
            with self.subTest(setting=setting):
                result = self.run_install("--dir", self.target, env={**self.env, setting: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.target.exists())

    def test_unsafe_target_and_unknown_arguments_are_refused(self):
        for args in (("--dir", "relative"), ("--dir", "/"), ("--dir", "/tmp/bad:path"),
                     ("--dir",), ("--unknown",)):
            with self.subTest(args=args):
                result = self.run_install(*args)
                self.assertNotEqual(result.returncode, 0)

    def test_symlink_target_is_refused(self):
        real = self.root / "real"
        real.mkdir()
        self.target.symlink_to(real, target_is_directory=True)
        result = self.run_install("--dir", self.target)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(real.iterdir()), [])

    def test_symlink_payload_is_refused(self):
        (self.source / "defaults/linked").symlink_to(self.source / "README.md")
        result = self.run_install("--dir", self.target)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("符号链接", result.stderr)
        self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()
