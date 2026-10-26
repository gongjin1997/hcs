import os
import sqlite3
import tempfile

# 必须在导入 app 之前设置：测试使用独立的临时 SQLite 库
_TMP = tempfile.mkdtemp(prefix="inspector-test-")
os.environ["INSPECTOR_DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["INSPECTOR_DEMO_SEED"] = "true"
os.environ["INSPECTOR_ENV"] = "test"
os.environ["INSPECTOR_JWT_SECRET"] = "test-jwt-secret-0123456789abcdef0123456789"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def demo_template() -> dict:
    """会话内只写一次演示数据（bootstrap → seed_demo）到模板库；会话库与 test_bootstrap 共用，省掉一次约 4 秒的写入。"""
    from sqlalchemy import func, select

    from app.models import User
    from app.services.bootstrap import bootstrap
    from app.services.seed import seed_demo

    path = f"{_TMP}/demo-template.db"
    tpl = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(tpl)
    with sessionmaker(bind=tpl, expire_on_commit=False, autoflush=False)() as db:
        bootstrap(db)
        users_after_bootstrap = db.scalar(select(func.count()).select_from(User))
        seeded = seed_demo(db)
    tpl.dispose()
    return {"path": path, "users_after_bootstrap": users_after_bootstrap, "seeded": seeded}


@pytest.fixture()
def demo_db(demo_template):
    """模板库的独立内存副本（含演示数据），用例可随意读写。"""
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
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)()
    yield session
    session.close()
    engine.dispose()


def demo_login(client: TestClient, username: str, tenant_id: int | None = None) -> dict:
    """用演示账号登录（密码取自演示数据定义，测试代码里不再重复写明文）；tenant_id 对应顶部“租户切换”（X-Tenant-Id）。"""
    from app.services.seed import DEMO_USERS

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
    from app.services.bootstrap import bootstrap
    from app.services.seed import _tenants

    bootstrap(mem_db)
    tenants = _tenants(mem_db)
    mem_db.commit()
    return tenants
