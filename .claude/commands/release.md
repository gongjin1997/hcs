---
description: 准备发版（改版本号、整理 CHANGELOG、检查），不打 tag、不推送
argument-hint: <版本号，如 0.5.0>
---

准备发布版本 $ARGUMENTS（流程见 `docs/开发文档.md` §7）：

1. 版本号改 5 处：`backend/app/__init__.py`、`backend/pyproject.toml`、`frontend/package.json`（以及 `package-lock.json` 顶部的版本）、
   `deploy/.env.example` 的 `HCS_INSPECTOR_VERSION`、`deploy/docker-compose.yml` 中的默认值。用 `rg` 确认没有遗漏的旧版本号。
2. `CHANGELOG.md`：把「Unreleased」整理为「[$ARGUMENTS] - 今天的日期」，按 新增 / 变更 / 修复 / 部署 / 质量 / 已知限制 分组，写清升级注意事项。
3. `make apidoc`；依赖有变化时提醒我执行 `make lock`（不要自动执行）。
4. `make check`；本版本有迁移时，`make test-pg`（T02 完成后可用）并确认 upgrade / downgrade 都通过。
5. 汇总给我：变更摘要、迁移列表、部署运维手册「升级」一节需要补充的内容草稿。不要打 tag，不要 push。