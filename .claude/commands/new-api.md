---
description: 按项目约定新增一个后端接口（权限、审计、范围、测试、接口清单、前端封装）
argument-hint: <接口描述，如 "GET /alarms/{id}/similar 相似告警">
---

新增接口：$ARGUMENTS

按 `docs/开发文档.md` §4.4 与 `backend/CLAUDE.md`：

1. 路由放到对应的 `backend/app/api/v1/<模块>.py`（新文件要在 `app/api/router.py` 注册）；`summary` 用中文写清用途（会进入接口清单）。
2. 写接口：`require_perm(...)` + `audit.log("对象.动作", ...)`；查询：`get_scope` 按租户过滤；声明 `response_model=ApiResponse[...]`。
3. 请求 / 响应模型放 `app/schemas/`，校验提示用中文；错误用 `BizError` / `not_found`。
4. 需要新权限点时：`core/permissions.py`（`PERMISSIONS` / `BUILTIN_ROLES` / `ADDED_IN`），并在本版本迁移里给内置角色补授（参考迁移 0003）。
5. 测试：正常路径、无权限（403 / 40301）、越租户（看不到其他租户数据）、参数错误（400 / 40001）。
6. `make apidoc` 更新 `docs/接口清单.md`；前端 `src/api/index.ts` 增加调用、`src/types/` 增加类型。
7. `make check`。先给出接口设计（路径、方法、权限点、请求 / 响应字段）等我确认，再实现。