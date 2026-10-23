---
description: 对照 make probe 的输出核对 HCS 字段映射（真实环境联调用）
argument-hint: <已脱敏的 probe 输出文件路径>
---

读取 `$ARGUMENTS`（`make probe args="--tenant <编码> --out <文件>"` 的输出，已由人工脱敏）。不要读取其他机密文件。

对照 `docs/联调手册.md`、`backend/app/tools/probe.py`（`PROBES`）、`backend/app/collectors/`、`backend/app/ledger/fetchers.py`、`backend/app/k8s/`、`backend/mock_hcs/`：

1. 逐个接口列出：调用是否成功、返回的关键字段、与代码期望的差异（缺失 / 改名 / 类型不同 / 枚举值不同 / 分页方式不同）。
2. 每个差异给出修改建议：改哪个文件的哪个函数、Mock 如何同步、要补的测试。
3. 先输出差异表等我确认，**不要直接改代码**。
4. 输出中不要出现 Token、AK/SK、密码、手机号、邮箱、内网 IP 等敏感值；需要举例时用占位符。