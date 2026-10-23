"""生成接口清单（Markdown）：python -m app.tools.apidoc > ../docs/接口清单.md（仓库根目录 make apidoc）

按模块（OpenAPI tag）列出 方法 / 路径 / 说明 / 访问控制；访问控制从路由依赖解析：
- require_perm("a", "b") → 需要权限点 a + b；require_perm(..., any_of=True) → 满足其一
- 只依赖登录（get_current_user）→ “登录即可”；没有登录依赖 → “公开”
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app import __version__
from app.core.permissions import PERMISSION_NAMES

# 不走登录令牌、由接口自身校验凭证的公开接口
_PUBLIC_NOTES = {
    "/api/v1/mail-actions/{token}": "公开（邮件签名令牌，24 小时有效，仅可认领）",
    "/api/v1/stream/alarms": "公开（SSE 一次性票据 `?ticket=`）",
}


def _closure(call: Callable[..., Any]) -> dict[str, Any]:
    code = getattr(call, "__code__", None)
    cells = getattr(call, "__closure__", None) or ()
    if code is None:
        return {}
    return {name: cell.cell_contents for name, cell in zip(code.co_freevars, cells, strict=False)}


def _access(dep: Dependant) -> tuple[list[str], bool]:
    """返回（权限点表达式列表, 是否需要登录）。"""
    perms: list[str] = []
    login = False
    for d in dep.dependencies:
        call = d.call
        qual = getattr(call, "__qualname__", "") if call is not None else ""
        if qual.startswith("require_perm."):
            c = _closure(call)  # type: ignore[arg-type]
            codes = [str(x) for x in c.get("codes", ())]
            perms.append((" 或 " if c.get("any_of") else " + ").join(f"`{x}`" for x in codes))
            login = True
        elif qual in ("get_current_user", "get_scope"):
            login = True
        sub_perms, sub_login = _access(d)
        perms.extend(p for p in sub_perms if p not in perms)
        login = login or sub_login
    return perms, login


def _routes(routes: list[Any]) -> list[Any]:
    """展开路由：APIRoute 直接返回；新版 FastAPI 的 include_router 为延迟展开对象（effective_route_contexts）。"""
    out: list[Any] = []
    for r in routes:
        if isinstance(r, APIRoute):
            out.append(r)
        elif hasattr(r, "effective_route_contexts"):
            out.extend(r.effective_route_contexts())
    return out


def build() -> str:
    from app.main import app

    groups: dict[str, list[tuple[str, str, str, str]]] = {}
    for route in _routes(list(app.routes)):
        if not route.include_in_schema:
            continue
        tag = str(route.tags[0]) if route.tags else "其它"
        perms, login = _access(route.dependant)
        access = "；".join(perms) if perms else ("登录即可" if login else _PUBLIC_NOTES.get(route.path, "公开"))
        summary = (route.summary or route.name or "").replace("|", "\\|")
        for method in sorted(m for m in route.methods if m not in ("HEAD", "OPTIONS")):
            groups.setdefault(tag, []).append((method, route.path, summary, access))

    total = sum(len(v) for v in groups.values())
    out = [
        f"# 接口清单（v{__version__}，自动生成）",
        "",
        "> 由 `make apidoc`（`python -m app.tools.apidoc`）根据代码生成，请勿手改。统一前缀 `/api/v1`，"
        "返回 `{\"code\": 0, \"message\": \"ok\", \"data\": ...}`；登录后请求头带 `Authorization: Bearer <token>`，"
        "可选 `X-Tenant-Id` 限定租户。",
        "> 在线文档：`/api/docs`（Swagger UI 静态资源来自公网 CDN，离线局点以本文件为准）。",
        "",
        f"共 **{total}** 个业务接口，另有运维接口：`GET /healthz`（存活）、`GET /readyz`（数据库 / Redis 就绪）、"
        "`GET /metrics`（Prometheus，令牌或 nginx 白名单）。",
        "",
        "| 模块 | 接口数 |",
        "| --- | --- |",
    ]
    out += [f"| {tag} | {len(rows)} |" for tag, rows in groups.items()]
    for tag, rows in groups.items():
        out += ["", f"## {tag}（{len(rows)}）", "", "| 方法 | 路径 | 说明 | 访问控制 |", "| --- | --- | --- | --- |"]
        out += [f"| {m} | `{p}` | {s} | {a} |" for m, p, s, a in rows]
    out += ["", "## 权限点", "", "| 编码 | 名称 |", "| --- | --- |"]
    out += [f"| `{code}` | {name} |" for code, name in PERMISSION_NAMES.items()]
    return "\n".join(out) + "\n"


def main() -> int:
    sys.stdout.write(build())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
