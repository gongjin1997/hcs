"""生产配置自检：默认密钥 / 缺主密钥 / SQLite 在 prod 下拒绝启动。"""
import base64
import os

import pytest

from app.core.config import Settings, ensure_prod_ready, prod_problems


def _prod(**kw) -> Settings:
    base = {"env": "prod", "jwt_secret": "x" * 40, "mail_link_secret": "y" * 24,
            "master_key": base64.b64encode(os.urandom(32)).decode(),
            "database_url": "postgresql+psycopg://u:p@db:5432/inspector"}
    base.update(kw)
    return Settings(**base)


def test_dev_never_blocks():
    assert prod_problems(Settings(env="dev")) == []


def test_prod_ok():
    assert prod_problems(_prod()) == []
    ensure_prod_ready(_prod())


@pytest.mark.parametrize("kw, word", [
    ({"jwt_secret": "change-me-in-prod-use-32+-random-bytes"}, "JWT_SECRET"),
    ({"jwt_secret": "short"}, "JWT_SECRET"),
    ({"mail_link_secret": "change-me-too"}, "MAIL_LINK_SECRET"),
    ({"master_key": None}, "MASTER_KEY"),
    ({"database_url": "sqlite:///./x.db"}, "PostgreSQL"),
])
def test_prod_problems(kw, word):
    problems = prod_problems(_prod(**kw))
    assert len(problems) == 1 and word in problems[0]
    with pytest.raises(SystemExit):
        ensure_prod_ready(_prod(**kw))
