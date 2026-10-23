"""生产初始化：python -m app.workers.bootstrap

- 幂等：可重复执行，只补齐缺失的内置规则和巡检任务（含台账同步），不覆盖已有配置
- 首次部署时设置 INSPECTOR_INIT_ADMIN_PASSWORD 创建 admin；创建完成后请从 deploy/.env 中清空该变量并尽快修改口令
- docker compose 部署中由一次性的 migrate 服务与迁移串行执行：alembic upgrade head && python -m app.workers.bootstrap
  （make install / make upgrade 自动执行；升级时先于新版本 api / scheduler / notifier 运行）
"""
import logging

from app.core.config import ensure_prod_ready, get_settings
from app.core.db import SessionLocal
from app.core.logging import setup_logging
from app.services.bootstrap import bootstrap

log = logging.getLogger("bootstrap")


def main() -> None:
    setup_logging()
    ensure_prod_ready()
    with SessionLocal() as db:
        result = bootstrap(db, admin_password=get_settings().init_admin_password)
    log.info("bootstrap finished: %s", result)
    if not result["admin_created"] and get_settings().init_admin_password:
        log.warning("users already exist, INSPECTOR_INIT_ADMIN_PASSWORD ignored; clear it in deploy/.env")


if __name__ == "__main__":
    main()
