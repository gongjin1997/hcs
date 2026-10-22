import os
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

from app.core.db import Base  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
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
