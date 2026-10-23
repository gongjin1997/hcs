"""接口清单生成 + 安全回归：除白名单外，所有业务接口都必须登录（或带权限点）。"""
from app.main import app
from app.tools import apidoc

# 公开接口白名单：登录本身、登录页选项、邮件签名链接、SSE（一次性票据）
PUBLIC = {"/api/v1/auth/login-options", "/api/v1/auth/login", "/api/v1/mail-actions/{token}", "/api/v1/stream/alarms"}


def test_every_api_requires_login_except_whitelist():
    routes = [r for r in apidoc._routes(list(app.routes)) if r.include_in_schema]
    assert len(routes) >= 90
    missing = [r.path for r in routes if r.path not in PUBLIC and not apidoc._access(r.dependant)[1]]
    assert missing == []


def test_write_apis_have_permission_codes():
    """写操作（POST / PUT / PATCH / DELETE）除个人操作外都要声明权限点。"""
    self_service = {"/api/v1/auth/login", "/api/v1/auth/password", "/api/v1/mail-actions/{token}"}
    no_perm = []
    for r in apidoc._routes(list(app.routes)):
        if not r.include_in_schema or r.path in self_service:
            continue
        if set(r.methods) & {"POST", "PUT", "PATCH", "DELETE"} and not apidoc._access(r.dependant)[0]:
            no_perm.append(f"{sorted(r.methods)} {r.path}")
    assert no_perm == []


def test_build_markdown():
    md = apidoc.build()
    assert "共 **" in md and "| `alarm:handle` |" in md and "`/api/v1/alarms`" in md
