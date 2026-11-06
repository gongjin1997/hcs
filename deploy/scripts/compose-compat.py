"""只给独立 Compose v1 生成兼容配置；保留变量引用，不输出实际 .env 密钥。"""
import os
import sys
import tempfile
from pathlib import Path

import yaml


def convert(source: Path, destination: Path, external_database: bool = False) -> None:
    data = yaml.safe_load(source.read_text())
    if not isinstance(data, dict):
        raise ValueError("invalid Compose document")
    data.pop("name", None)
    data["version"] = "2.4"
    for service in data.get("services", {}).values():
        service.pop("pull_policy", None)
        health = service.get("healthcheck", {})
        health.pop("start_interval", None)
        dependencies = service.get("depends_on", {})
        if isinstance(dependencies, dict):
            for name, condition in list(dependencies.items()):
                if isinstance(condition, dict) and condition.get("condition") == "service_completed_successfully":
                    del dependencies[name]  # 安装脚本已经串行完成迁移。
        environment = service.get("environment", {})
        if isinstance(environment, dict):
            value = environment.get("INSPECTOR_DATABASE_URL", "")
            if isinstance(value, str) and value.startswith("${INSPECTOR_DATABASE_URL:-postgresql+psycopg://"):
                environment["INSPECTOR_DATABASE_URL"] = (
                    "${INSPECTOR_DATABASE_URL}" if external_database else
                    "postgresql+psycopg://${POSTGRES_USER:-inspector}:${POSTGRES_PASSWORD:?缺少数据库口令}"
                    "@postgres:5432/${POSTGRES_DB:-inspector}"
                )
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".compose-")
    try:
        with os.fdopen(fd, "w") as output:
            yaml.safe_dump(data, output, allow_unicode=True, sort_keys=False)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    convert(Path(sys.argv[1]), Path(sys.argv[2]), len(sys.argv) > 3 and sys.argv[3] == "external")
