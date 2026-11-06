import os
import sqlite3
import tempfile
from uuid import uuid4

# 必须在导入 app 之前设置：测试使用独立的临时 SQLite 库
_TMP = tempfile.mkdtemp(prefix="inspector-test-")
os.environ["INSPECTOR_DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["INSPECTOR_DEMO_SEED"] = "true"
os.environ["INSPECTOR_ENV"] = "test"
os.environ["INSPECTOR_JWT_SECRET"] = "test-jwt-secret-0123456789abcdef0123456789"
os.environ["INSPECTOR_REDIS_URL"] = ""
os.environ["INSPECTOR_INIT_ADMIN_PASSWORD"] = ""
_PG_URL = os.environ.get("INSPECTOR_TEST_DATABASE_URL")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlalchemy.schema import CreateSchema, DropSchema  # noqa: E402

_pg_root = create_engine(_PG_URL) if _PG_URL else None
_schemas: set[str] = set()


def _pg_url() -> tuple[str, str]:
    assert _pg_root is not None and _PG_URL
    schema = f"hcs_test_{uuid4().hex}"
    with _pg_root.begin() as conn:
        conn.execute(CreateSchema(schema))
    _schemas.add(schema)
    url = make_url(_PG_URL).update_query_dict({"options": f"-csearch_path={schema}"})
    return url.render_as_string(hide_password=False), schema


def _drop_schema(schema: str) -> None:
    assert _pg_root is not None
    with _pg_root.begin() as conn:
        conn.execute(DropSchema(schema, cascade=True))
    _schemas.discard(schema)


if _PG_URL:
    os.environ["INSPECTOR_DATABASE_URL"], _app_schema = _pg_url()

from app.core.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def database_lifecycle():
    if _PG_URL:
        from alembic import command
        from alembic.config import Config

        cfg = Config("alembic.ini")
        cfg.attributes["database_url"] = os.environ["INSPECTOR_DATABASE_URL"]
        command.upgrade(cfg, "head")
    yield
    engine.dispose()
    if _pg_root is not None:
        for schema in list(_schemas):
            _drop_schema(schema)
        _pg_root.dispose()


def pytest_collection_modifyitems(items):
    if not _PG_URL:
        for item in items:
            if "pg" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="需要 make test-pg 的 PostgreSQL 测试库"))


@pytest.fixture()
def migration_url(tmp_path):
    if not _PG_URL:
        yield f"sqlite:///{tmp_path / 'mig.db'}"
        return
    url, schema = _pg_url()
    try:
        yield url
    finally:
        _drop_schema(schema)


@pytest.fixture(scope="session")
def demo_template() -> dict:
    """会话内只写一次演示数据（bootstrap → seed_demo）到模板库；会话库与 test_bootstrap 共用，省掉一次约 4 秒的写入。"""
    from sqlalchemy import func, select

    from app.demo.seed import seed_demo
    from app.models import User
    from app.services.bootstrap import bootstrap

    path = f"{_TMP}/demo-template.db"
    tpl = engine if _PG_URL else create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(tpl)
    with sessionmaker(bind=tpl, expire_on_commit=False, autoflush=False)() as db:
        bootstrap(db)
        users_after_bootstrap = db.scalar(select(func.count()).select_from(User))
        seeded = seed_demo(db)
    if not _PG_URL:
        tpl.dispose()
    return {"path": path, "users_after_bootstrap": users_after_bootstrap, "seeded": seeded}


@pytest.fixture()
def demo_db(demo_template, mem_db):
    """模板库的独立内存副本（含演示数据），用例可随意读写。"""
    if _PG_URL:
        from app.demo.seed import seed_demo

        seed_demo(mem_db)
        yield mem_db
        return
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with eng.connect() as conn, sqlite3.connect(demo_template["path"]) as src:
        src.backup(conn.connection.driver_connection)
    session = sessionmaker(bind=eng, expire_on_commit=False, autoflush=False)()
    yield session
    session.close()
    eng.dispose()


@pytest.fixture(scope="session")
def client(demo_template):
    # 把模板库整库复制进应用库，启动时 seed_demo 发现已有用户直接跳过
    if not _PG_URL:
        with engine.connect() as conn, sqlite3.connect(demo_template["path"]) as src:
            src.backup(conn.connection.driver_connection)
    with TestClient(app) as c:
        yield c


def login(client: TestClient, username: str, password: str) -> dict:
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['data']['access_token']}"}


@pytest.fixture(scope="session")
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture()
def mem_db():
    """独立的内存库（不含演示数据），用于流水线测试。"""
    schema = None
    if _PG_URL:
        url, schema = _pg_url()
        eng = create_engine(url)
    else:
        eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng, expire_on_commit=False, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        eng.dispose()
        if schema:
            _drop_schema(schema)


def demo_login(client: TestClient, username: str, tenant_id: int | None = None) -> dict:
    """用演示账号登录（密码取自演示数据定义，测试代码里不再重复写明文）；tenant_id 对应顶部“租户切换”（X-Tenant-Id）。"""
    from app.demo.seed import DEMO_USERS

    password = next(u[1] for u in DEMO_USERS if u[0] == username)
    headers = login(client, username, password)
    if tenant_id is not None:
        headers["X-Tenant-Id"] = str(tenant_id)
    return headers


@pytest.fixture(scope="session")
def tenant_ids(client, admin) -> dict[str, int]:
    """演示租户 code → id。"""
    return {t["code"]: t["id"] for t in client.get("/api/v1/system/tenants", headers=admin).json()["data"]}


MOCK_URL = "http://mock"          # 进程内 Mock HCS（mock_hcs.app），不依赖单独启动的 Mock 服务


@pytest.fixture()
def mock_http():
    from mock_hcs.app import app as mock_app

    return TestClient(mock_app, base_url=MOCK_URL)


@pytest.fixture()
def world(mem_db):
    """空库 + 两个演示租户（租户A：用户名密码 / 有 CES；租户B：AK/SK / 无 CES、有 Prometheus）。"""
    from app.demo.seed import _tenants
    from app.services.bootstrap import bootstrap

    bootstrap(mem_db)
    tenants = _tenants(mem_db)
    mem_db.commit()
    return tenants
