from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app import models  # noqa: F401  注册全部模型
from app.core.config import get_settings
from app.core.db import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
# 数据库地址：默认取 INSPECTOR_DATABASE_URL；测试 / 脚本可通过 Config.attributes["database_url"] 指定（% 需转义）
config.set_main_option("sqlalchemy.url",
                       (config.attributes.get("database_url") or get_settings().database_url).replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
                      literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.",
                                     poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True,
                          render_as_batch=connection.dialect.name == "sqlite")
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
