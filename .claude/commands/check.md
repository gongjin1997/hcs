---
description: 运行 make check，失败则定位并修复
---

运行 `make check`（ruff + mypy + vue-tsc + pytest + 前端构建，与 CI 一致）。

- 失败时：定位原因，只修与失败直接相关的代码（不顺手重构、不全量格式化），然后重新运行，直到通过。
- 如果失败与当前改动无关（例如环境问题），说明原因并停下来问我。
- 最后报告：ruff / mypy / vue-tsc / pytest（通过用例数、耗时）/ 前端构建各自的结果。