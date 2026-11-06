"""在已有 Python 运行时容器内校验、展开代码包及安装离线 wheels；宿主机不需要 Python。"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

try:
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
except ImportError:
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.utils import canonicalize_name

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")
MAX_TOTAL = 2 * 1024**3
MAX_FILE = 512 * 1024**2
MAX_MEMBERS = 20000


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def allowed(name: str) -> bool:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or path.as_posix() != name.rstrip("/"):
        return False
    if any(part.startswith(".env") or part in {".git", "__pycache__"} for part in path.parts):
        return False
    if path.name.endswith((".db", ".pem", ".key", ".crt", ".pyc")):
        return False
    return name in {"manifest.json", "requirements.txt", "constraints.txt", "backend", "backend/app", "backend/migrations",
                    "backend/alembic.ini", "web", "wheels"} or name.startswith(
                        ("backend/app/", "backend/migrations/", "web/", "wheels/"))


def read_manifest(raw: bytes) -> dict:
    if len(raw) > 8 * 1024**2:
        raise ValueError("代码清单过大")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("format") != 1 or not isinstance(value.get("release"), str):
        raise ValueError("代码包格式或版本标识无效")
    if not ID.fullmatch(value["release"]):
        raise ValueError("代码包格式或版本标识无效")
    if value.get("platform") != "linux/arm64" or value.get("python") != "3.12":
        raise ValueError("代码包必须面向 ARM64 / Python 3.12 运行时")
    files = value.get("files")
    if not isinstance(files, dict) or not files or len(files) > MAX_MEMBERS:
        raise ValueError("代码文件清单无效")
    for name, description in files.items():
        if not isinstance(name, str) or not allowed(name) or name == "manifest.json" or not isinstance(description, dict):
            raise ValueError("代码清单包含不允许的文件")
        if not isinstance(description.get("sha256"), str) or not SHA.fullmatch(description["sha256"]):
            raise ValueError("代码文件校验信息无效")
        if type(description.get("size")) is not int:
            raise ValueError("代码文件校验信息无效")
        if not 0 <= description["size"] <= MAX_FILE:
            raise ValueError("代码文件大小不合法")
    required = {"backend/app/__init__.py", "backend/migrations/env.py", "backend/alembic.ini", "web/index.html",
                "requirements.txt", "constraints.txt"}
    if not required.issubset(files) or sum(item["size"] for item in files.values()) > MAX_TOTAL:
        raise ValueError("代码包不完整或超过大小限制")
    return value


def verify_files(root: Path, manifest: dict) -> None:
    for name, description in manifest["files"].items():
        file = root / name
        for item in (file, *file.parents):
            if item == root.parent:
                break
            if item.is_symlink():
                raise ValueError("代码目录中不允许符号链接")
        if not file.is_file() or file.stat().st_size != description["size"] or digest(file) != description["sha256"]:
            raise ValueError(f"代码文件校验失败：{name}")
    dependency_hash = hashlib.sha256((root / "requirements.txt").read_bytes() + b"\0" +
                                     (root / "constraints.txt").read_bytes()).hexdigest()
    if dependency_hash != manifest.get("dependencies_sha256"):
        raise ValueError("依赖清单校验失败")


def dependency_problems(root: Path) -> list[str]:
    pins = {}
    for line in (root / "constraints.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" in line or " @ " in line or line.startswith("-"):
            raise ValueError("仅支持离线 wheel 形式的依赖")
        constraint = Requirement(line)
        if constraint.marker is None or constraint.marker.evaluate():
            pins[canonicalize_name(constraint.name)] = constraint
    problems = []
    required = []
    for line in (root / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" in line or " @ " in line or line.startswith("-"):
            raise ValueError("仅支持离线 wheel 形式的依赖")
        requirement = Requirement(line)
        if requirement.marker is None or requirement.marker.evaluate():
            required.append(requirement)
    # 既检查直接依赖，也核对已安装的生产传递依赖约束；不要求安装开发测试工具。
    installed = {}
    for item in metadata.distributions():
        if item.metadata["Name"]:
            installed.setdefault(canonicalize_name(item.metadata["Name"]), item.version)
    for requirement in required:
        name = canonicalize_name(requirement.name)
        if name not in installed or installed[name] not in requirement.specifier:
            problems.append(name)
    for name, requirement in pins.items():
        if name in installed and installed[name] not in requirement.specifier:
            problems.append(name)
    return sorted(set(problems))


def prepare_python(root: Path) -> None:
    python = root / "python"
    python.mkdir(mode=0o755, exist_ok=True)
    if str(python) not in sys.path:
        sys.path.insert(0, str(python))
    problems = dependency_problems(root)
    if not problems:
        return
    wheels = root / "wheels"
    if not wheels.is_dir() or not any(wheels.glob("*.whl")):
        raise ValueError("运行时依赖不同，请在构建机重新生成带 --with-wheels 的代码包；目标机不会联网安装依赖")
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-cache-dir", "--disable-pip-version-check",
                    "--only-binary=:all:", "--find-links", str(wheels), "--target", str(python),
                    "-r", str(root / "requirements.txt"), "-c", str(root / "constraints.txt")],
                   check=True, stdout=sys.stderr)
    # 新进程重新读取依赖，避免 importlib.metadata 的路径缓存遮蔽新安装的 wheel。
    environment = dict(os.environ, PYTHONPATH=str(python))
    subprocess.run([sys.executable, __file__, "check", str(root)], env=environment, check=True, stdout=sys.stderr)


def prepare(package: Path, releases: Path) -> str:
    if sys.version_info[:2] != (3, 12):
        raise ValueError("运行时不是 Python 3.12")
    if releases.is_symlink():
        raise ValueError("版本目录不能是符号链接")
    releases.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=releases))
    try:
        with tarfile.open(package, "r:gz") as archive:
            members = archive.getmembers()
            if not members or len(members) > MAX_MEMBERS or sum(item.size for item in members) > MAX_TOTAL:
                raise ValueError("代码包内容超过限制")
            names = set()
            for member in members:
                name = member.name.rstrip("/")
                if name in names or not allowed(name) or not (member.isfile() or member.isdir()):
                    raise ValueError("代码包包含重复文件、链接或非法路径")
                if member.size < 0 or member.size > MAX_FILE:
                    raise ValueError("代码文件大小超过限制")
                names.add(name)
            source = archive.extractfile("manifest.json")
            if source is None:
                raise ValueError("缺少代码清单")
            raw = source.read(8 * 1024**2 + 1)
            manifest = read_manifest(raw)
            actual = {member.name for member in members if member.isfile() and member.name != "manifest.json"}
            if actual != set(manifest["files"]):
                raise ValueError("归档与代码清单不一致")
            for member in members:
                name = member.name.rstrip("/")
                path = staging / name
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True, mode=0o755)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    data = archive.extractfile(member)
                    if data is None:
                        raise ValueError("代码文件无法读取")
                    with path.open("wb") as output:
                        shutil.copyfileobj(data, output)
                    path.chmod(0o644)
        verify_files(staging, manifest)
        release = releases / manifest["release"]
        if release.exists() or release.is_symlink():
            if release.is_symlink() or (release / "manifest.json").read_bytes() != raw:
                raise ValueError("该版本标识已被其他内容使用")
            verify_files(release, manifest)
            # 已发布目录保持不可变；重复应用只校验，不向在用代码目录重新写入依赖。
            environment = dict(os.environ, PYTHONPATH=str(release / "python"))
            subprocess.run([sys.executable, __file__, "check", str(release)], env=environment,
                           check=True, stdout=sys.stderr)
        else:
            prepare_python(staging)
            for directory, _, _ in os.walk(staging):
                Path(directory).chmod(0o755)
            staging.rename(release)
        return manifest["release"]
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["prepare", "check", "verify"])
    parser.add_argument("source", type=Path)
    parser.add_argument("releases", nargs="?", type=Path)
    args = parser.parse_args()
    try:
        if args.operation in {"check", "verify"}:
            if args.operation == "verify":
                verify_files(args.source, read_manifest((args.source / "manifest.json").read_bytes()))
            problems = dependency_problems(args.source)
            if problems:
                raise ValueError("离线依赖校验失败：" + ", ".join(problems))
        else:
            if args.releases is None:
                raise ValueError("缺少版本目录")
            print(prepare(args.source, args.releases))
    except (OSError, ValueError, KeyError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        print(f"代码包未应用：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
