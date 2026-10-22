"""生产初始化：python -m app.workers.bootstrap

- 幂等：可重复执行，只补齐缺失的内置规则和巡检任务（含台账同步），不覆盖已有配置
- 首次部署时设置 INSPECTOR_INIT_ADMIN_PASSWORD 创建 admin；创建完成后请从 Secret 中删除该变量并尽快修改口令
- K8s 中与迁移串行执行：alembic upgrade head && python -m app.workers.bootstrap
"""
import logging

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.logging import setup_logging
from app.services.bootstrap import bootstrap

log = logging.getLogger("bootstrap")


def main() -> None:
    setup_logging()
    with SessionLocal() as db:
        result = bootstrap(db, admin_password=get_settings().init_admin_password)
    log.info("bootstrap finished: %s", result)
    if not result["admin_created"] and get_settings().init_admin_password:
        log.warning("users already exist, INSPECTOR_INIT_ADMIN_PASSWORD ignored; remove it from the secret")


if __name__ == "__main__":
    main()
