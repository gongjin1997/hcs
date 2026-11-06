"""给已安装环境补充代码发布入口；不包含镜像、真实配置或数据。仅构建机执行。"""
import argparse
import hashlib
import io
import os
import shlex
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ["deploy.sh", "docker-compose.yml", "docker-compose.offline.yml", "docker-compose.code.yml",
         "docker-compose.https.yml", ".env.example", "Makefile"]
SCRIPTS = ["common.sh", "gen-env.sh", "install.sh", "status.sh", "backup.sh", "restore.sh", "upgrade.sh", "rollback.sh",
           "update-code.sh", "release-manager.py", "compose-compat.py"]


def build(code: Path) -> Path:
    if not code.is_file() or code.is_symlink() or not code.name.startswith("hcs-inspector-code-"):
        raise ValueError("请先生成正式代码包")
    prefix = "hcs-inspector-code-deploy-upgrade"
    destination = ROOT / "deploy/dist" / (prefix + ".tar.gz")
    payload = {name: (ROOT / "deploy" / name).read_bytes() for name in FILES}
    payload.update({"scripts/" + name: (ROOT / "deploy/scripts" / name).read_bytes() for name in SCRIPTS})
    payload["code/" + code.name] = code.read_bytes()
    payload["code/" + code.name + ".sha256"] = (
        hashlib.sha256(payload["code/" + code.name]).hexdigest() + "  " + code.name + "\n").encode()
    command = shlex.quote("code/" + code.name)
    payload["apply-code.sh"] = ("#!/usr/bin/env bash\nset -euo pipefail\ncd \"$(dirname \"$0\")\"\n"
                                f"exec bash deploy.sh update {command}\n").encode()
    payload["CODE-UPGRADE-README.txt"] = (
        "这是已部署环境的代码发布脚本补丁，不含镜像、.env、数据库或证书。\n"
        "在原部署目录解压（去掉最外层目录），执行 bash apply-code.sh。\n"
        "会复用本地镜像、备份内置数据库并更新代码，原开关和凭据保留。\n"
        "以后只上传 hcs-inspector-code-版本.tar.gz，执行 bash deploy.sh update 代码包。\n").encode()
    payload["CODE-UPGRADE-SHA256SUMS"] = "".join(
        f"{hashlib.sha256(content).hexdigest()}  {name}\n" for name, content in sorted(payload.items())
    ).encode()
    destination.parent.mkdir(exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".deploy-tools-", suffix=".tar.gz")
    os.close(fd)
    try:
        with tarfile.open(temporary, "w:gz") as archive:
            for name, content in sorted(payload.items()):
                info = tarfile.TarInfo(prefix + "/" + name)
                info.size = len(content)
                info.mode = 0o755 if name.endswith(".sh") else 0o644
                info.uid = info.gid = 0
                archive.addfile(info, io.BytesIO(content))
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    checksum = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_name(destination.name + ".sha256").write_text(checksum + "  " + destination.name + "\n")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("code", type=Path)
    print(build(parser.parse_args().code))
