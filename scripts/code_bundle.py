#!/usr/bin/env python3
"""Build an offline source update for the existing ARM64 runtime (Python 3.12).

Run on the build machine only. No image is built, saved, or exported. The target
machine extracts the bundle and uses its existing Docker runtime image. The
archive puts frontend/dist contents directly under web/ (web/index.html) and
includes the versioned Compose overlay for runtime settings shipped with code.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

DEFAULT_RUNTIME_IMAGE = "hcs-inspector-arm64-backend:0.4.0"
RELEASE_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}\Z")
SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "tests",
        "test",
        "__tests__",
        "coverage",
        "htmlcov",
        ".cache",
        "logs",
        "log",
        "certs",
        "certificates",
        "secrets",
        "keys",
        ".ssh",
        "credentials",
        "database",
        "databases",
    }
)
SKIP_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".map",
    ".db",
    ".db-wal",
    ".db-shm",
    ".sqlite",
    ".sqlite3",
    ".sqlite-wal",
    ".sqlite-shm",
    ".sqlite3-wal",
    ".sqlite3-shm",
    ".log",
    ".pem",
    ".key",
    ".crt",
    ".cer",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".der",
    ".csr",
    ".p7b",
    ".p7c",
    ".pub",
    ".sqlite-journal",
    ".sqlite3-journal",
    ".db-journal",
)


class BundleError(RuntimeError):
    """A source or build input is unsafe or incomplete."""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_path(path: Path) -> None:
    """Check every existing component without following source symlinks."""
    absolute = Path(os.path.abspath(path))
    for item in [*reversed(absolute.parents), absolute]:
        try:
            mode = item.lstat().st_mode
        except FileNotFoundError:
            raise BundleError(f"Missing input: {item}") from None
        if stat.S_ISLNK(mode):
            raise BundleError(f"Symbolic links are not allowed: {item}")
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise BundleError(f"Only regular files and directories are allowed: {item}")


def _read(path: Path) -> bytes:
    _check_path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BundleError(f"Expected a regular file: {path}")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(fd)


def _excluded(relative: PurePosixPath) -> bool:
    for part in relative.parts:
        low = part.lower()
        if (
            low in SKIP_DIRS
            or low.startswith(".env")
            or low.startswith("id_rsa")
            or low.startswith(("id_ed25519", "id_ecdsa", "id_dsa"))
        ):
            return True
    name = relative.name.lower()
    return (
        name.endswith(SKIP_SUFFIXES)
        or name.endswith((".log.gz", ".sql.gz", ".dump", ".backup", ".bak"))
        or name.startswith(("test_", "test."))
        or name in {"conftest.py", "pytest.ini"}
        or name.endswith(
            ("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts", ".test.tsx", ".spec.tsx", ".test.jsx", ".spec.jsx")
        )
    )


def _source_entries(source: Path) -> list[tuple[Path, PurePosixPath, bool]]:
    _check_path(source)
    if not source.is_dir():
        raise BundleError(f"Expected a source directory: {source}")
    # Validate *all* entries, including otherwise excluded entries. Do not silently
    # dereference or hide a symlink inside a cache/test/secret directory.
    entries: list[tuple[Path, PurePosixPath, bool]] = []

    def walk(directory: Path) -> None:
        for path in sorted(directory.iterdir()):
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise BundleError(f"Only regular files and directories are allowed: {path}")
            relative = PurePosixPath(path.relative_to(source).as_posix())
            entries.append((path, relative, stat.S_ISDIR(mode)))
            if stat.S_ISDIR(mode):
                walk(path)

    walk(source)
    return entries


def _copy_tree(source: Path, dest: Path) -> None:
    entries = _source_entries(source)
    dest.mkdir(parents=True, exist_ok=True)
    for path, relative, is_dir in entries:
        if _excluded(relative):
            continue
        target = dest.joinpath(*relative.parts)
        if is_dir:
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_read(path))


def _validate_frontend_source(frontend: Path) -> None:
    """Reject links before npm can copy their targets into dist.

    Installed dependencies are outside the shipped source; npm normally creates
    symlinks in node_modules/.bin, so do not descend into that directory.
    """
    _check_path(frontend / "package.json")
    for path in sorted(frontend.iterdir()):
        if path.name in {"node_modules", ".git"}:
            continue
        _check_path(path)
        if path.is_dir():
            _source_entries(path)


def _metadata(root: Path) -> tuple[str, bytes, bytes]:
    pyproject = root / "backend/pyproject.toml"
    if not pyproject.exists() and not pyproject.is_symlink():
        pyproject = root / "pyproject.toml"
    try:
        project = tomllib.loads(_read(pyproject).decode("utf-8"))["project"]
        version = project["version"]
        dependencies = project["dependencies"]
        extras = project.get("optional-dependencies", {})
        if not isinstance(extras, dict):
            raise BundleError("project.optional-dependencies must be a table")
        postgres = extras.get("postgres")
    except (KeyError, UnicodeError, tomllib.TOMLDecodeError, TypeError) as exc:
        raise BundleError(f"Invalid project metadata: {exc}") from exc
    if not isinstance(version, str) or not version.strip() or "\n" in version or "\r" in version:
        raise BundleError("project.version must be a nonempty single-line string")
    if not isinstance(dependencies, list) or not isinstance(postgres, list):
        raise BundleError("project.dependencies and optional-dependencies.postgres must be arrays")
    items = []
    for dep in dependencies + postgres:
        if not isinstance(dep, str) or not dep.strip() or "\n" in dep or "\r" in dep or dep.lstrip().startswith(("-", "#")):
            raise BundleError("Dependencies must be single-line requirement strings")
        if dep not in items:
            items.append(dep)
    requirements = ("\n".join(items) + "\n").encode("utf-8")
    candidates = (root / "backend/constraints.txt", root / "constraints.txt", root / "deploy/constraints.txt")
    constraint_path = next((path for path in candidates if path.exists() or path.is_symlink()), None)
    if constraint_path is None:
        raise BundleError("Missing constraints.txt (expected backend/, project root, or deploy/)")
    constraints = _read(constraint_path)
    return version, requirements, constraints


def _output_directory(root: Path) -> Path:
    _check_path(root)
    for path in (root / "deploy", root / "deploy/dist"):
        if path.exists() or path.is_symlink():
            _check_path(path)
            if not path.is_dir():
                raise BundleError(f"Expected output directory: {path}")
        else:
            path.mkdir()
    return root / "deploy/dist"


def _run(command: list[str], cwd: Path | None = None) -> None:
    try:
        subprocess.run(command, cwd=cwd, check=True)
    except FileNotFoundError as exc:
        raise BundleError(f"Build tool not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise BundleError(f"Build command failed (exit {exc.returncode}): {command[0]}") from exc


def _inventory(stage: Path) -> dict[str, dict[str, str | int]]:
    result = {}
    for path in sorted(stage.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise BundleError(f"Unsafe staged entry: {path}")
        if stat.S_ISREG(mode):
            content = _read(path)
            result[path.relative_to(stage).as_posix()] = {"sha256": _digest(content), "size": len(content)}
    return result


def _archive(stage: Path, output: Path) -> None:
    with output.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for path in sorted(stage.rglob("*")):
                relative = path.relative_to(stage).as_posix()
                info = tarfile.TarInfo(relative)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                if path.is_dir():
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    archive.addfile(info)
                else:
                    data = _read(path)
                    info.mode = 0o644
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))


def build_bundle(
    root: Path,
    *,
    release: str | None = None,
    skip_web_build: bool = False,
    with_wheels: bool = False,
    runtime_image: str = DEFAULT_RUNTIME_IMAGE,
) -> Path:
    root = Path(os.path.abspath(root))
    if release is not None and not RELEASE_PATTERN.fullmatch(release):
        raise BundleError(
            "release must be 1-128 safe ASCII letters, digits, dots, underscores or hyphens, starting with a letter or digit"
        )
    if not runtime_image or runtime_image.startswith("-") or any(char.isspace() for char in runtime_image):
        raise BundleError("Invalid runtime image name")
    _check_path(root)
    version, requirements, constraints = _metadata(root)
    out_dir = _output_directory(root)
    with tempfile.TemporaryDirectory(prefix=".code-bundle-", dir=out_dir) as temporary:
        temporary = Path(temporary)
        stage = temporary / "payload"
        stage.mkdir()
        _copy_tree(root / "backend/app", stage / "backend/app")
        _copy_tree(root / "backend/migrations", stage / "backend/migrations")
        (stage / "backend/alembic.ini").write_bytes(_read(root / "backend/alembic.ini"))
        (stage / "docker-compose.code.yml").write_bytes(_read(root / "deploy/docker-compose.code.yml"))
        frontend = root / "frontend"
        _check_path(frontend)
        dist = frontend / "dist"
        if not skip_web_build:
            _validate_frontend_source(frontend)
            _run(["npm", "run", "build"], cwd=frontend)
        _copy_tree(dist, stage / "web")
        if not (stage / "web/index.html").is_file():
            raise BundleError("frontend/dist/index.html is missing; run npm run build on the build machine")
        (stage / "requirements.txt").write_bytes(requirements)
        (stage / "constraints.txt").write_bytes(constraints)
        code_files = _inventory(stage)
        code_hash = _digest(json.dumps(code_files, sort_keys=True, separators=(",", ":")).encode())
        release = release or f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{code_hash[:12]}"
        if with_wheels:
            wheels = stage / "wheels"
            wheels.mkdir()
            # The image must already be local. Never pull, build, save, export,
            # prune, stop or modify an existing container. This disposable run
            # only downloads wheels into the build machine's staging directory.
            _run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--pull=never",
                    "--platform",
                    "linux/arm64",
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "--cap-drop=ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--mount",
                    f"type=bind,src={stage},dst=/bundle",
                    "--workdir",
                    "/bundle",
                    "--entrypoint",
                    "python",
                    runtime_image,
                    "-m",
                    "pip",
                    "download",
                    "--only-binary=:all:",
                    "--no-cache-dir",
                    "-r",
                    "requirements.txt",
                    "-c",
                    "constraints.txt",
                    "--dest",
                    "wheels",
                ]
            )
            if not any(wheels.iterdir()):
                raise BundleError("pip download produced no wheels")
            for wheel in wheels.iterdir():
                if wheel.suffix != ".whl" or not stat.S_ISREG(wheel.lstat().st_mode):
                    raise BundleError(f"Unexpected wheel download: {wheel.name}")
        files = _inventory(stage)
        manifest = {
            "format": 1,
            "release": release,
            "version": version,
            "platform": "linux/arm64",
            "python": "3.12",
            "dependencies_sha256": _digest(requirements + b"\0" + constraints),
            "files": files,
        }
        (stage / "manifest.json").write_bytes((json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
        output = out_dir / f"hcs-inspector-code-{release}.tar.gz"
        sidecar = output.with_name(output.name + ".sha256")
        if output.exists() or output.is_symlink() or sidecar.exists() or sidecar.is_symlink():
            raise BundleError(f"Release output already exists: {output.name}; choose a new --release")
        partial = temporary / "bundle.tar.gz"
        _archive(stage, partial)
        checksum = _digest(_read(partial))
        sidecar_temp = temporary / "checksum.sha256"
        sidecar_temp.write_text(f"{checksum}  {output.name}\n", encoding="ascii")
        # Exclusive hard links avoid overwriting concurrent releases or links.
        os.link(partial, output)
        try:
            os.link(sidecar_temp, sidecar)
        except OSError:
            output.unlink()
            raise
        return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1], help="source checkout (default: repository root)"
    )
    parser.add_argument("--release", help="safe release ID; default: UTC timestamp plus code digest")
    parser.add_argument(
        "--skip-web-build", action="store_true", help="reuse frontend/dist (repeat tests / already built frontend)"
    )
    parser.add_argument(
        "--with-wheels", action="store_true", help="download ARM64 binary wheels using an existing local runtime image"
    )
    parser.add_argument("--runtime-image", default=DEFAULT_RUNTIME_IMAGE)
    args = parser.parse_args(argv)
    try:
        output = build_bundle(
            args.root,
            release=args.release,
            skip_web_build=args.skip_web_build,
            with_wheels=args.with_wheels,
            runtime_image=args.runtime_image,
        )
    except (BundleError, OSError) as exc:
        print(f"code-bundle: {exc}", file=sys.stderr)
        return 1
    print(output)
    print(output.with_name(output.name + ".sha256"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
