# 版本记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [0.4.0] - 2026-10-23

v0.4 的目标是“**可交付试运行**”：补齐运维日常需要的配置能力（静默、采集开关、任务编辑、系统设置、通知维护、台账字典与导入），
把部署收敛为**单机 docker compose**（去掉 K8s），并为局域网对接真实 HCS 准备好探测工具和手册。

### 新增

- **维护窗口与静默**（新页面「维护与静默」）：按租户 / 服务 / 资源类型 / 规则 / 级别 / 具体资源 / 指纹 / 名称关键字匹配；
  一次性或每周循环（最长 31 天，循环最长 366 天）；创建前预览命中的活动告警；告警列表「快速静默」；
  命中的告警变为「已抑制」（不通知、不升级），窗口结束或提前解除后恢复为「告警中」并补发通知；已认领的告警不受影响。
- **采集开关**（系统管理 → 采集开关）：按「资源类型 × 租户」启停采集，全局开关 + 租户覆盖；保存前显示影响（哪些「租户 · 资源类型」停止 / 恢复采集、将关闭的活动告警数）；
  关闭后跳过台账同步与巡检（已有台账保留、不标记为释放），并关闭该类资源的活动告警。
- **巡检任务编辑**（系统管理 → 巡检任务）：修改执行周期（秒，有最小值保护）或 crontab、启停，全部审计；调度进程最迟 30 秒自动生效，无需重启；查看每个租户的最近运行。
- **平台自身告警**：R-PLAT-001 任务连续失败（默认 3 次）、R-PLAT-002 租户认证失败（401/403）、R-PLAT-003 通知重试 5 次仍失败、
  R-PLAT-004 调度 / 通知进程心跳超时（默认 3 分钟）；没有通知路由命中时发给系统设置里的收件人或平台管理员；页面「平台状态」页签集中查看。
- **系统设置**（系统管理 → 系统设置，存 `sys_setting`，保存即生效）：数据保留天数（8 类数据）、定时报表发送时间与收件人、平台自身告警参数、台账字典。
- **数据清理任务** `data_cleanup`（默认每天 03:30）：按保留天数分批删除过期数据，活动告警 / 在用资源不删除，删除行数记入运行记录。
- **通知维护**：通知路由、联系人组、联系人页面增删改；路由「模拟匹配」（给定告警条件看会发给谁）；SMTP 测试发信。
- **台账字典与 Excel 批量导入**：业务系统 / 负责人字典（可设为只能从字典选择）；下载模板 → 上传 → 预览（逐行校验、显示变更）→ 确认导入，全程审计。
- **HCS 接口探测工具** `python -m app.tools.probe`（`make probe`）：22 个接口 + IAM 认证 + CES 指标逐个调用，输出必需 / 可选字段缺失、状态取值分布、脱敏样例；
  退出码 0 / 1 / 2，供局域网联调核对字段映射。
- **平台自检工具** `python -m app.tools.status`（`make status`）：进程心跳、通知积压、任务健康、平台告警、迁移版本；`--json`；`--check scheduler|notifier` 用作容器健康检查。
- **Prometheus 指标** `GET /metrics`：活动告警、通知积压 / 最老待发时长、任务最近状态 / 耗时 / 连续失败、进程心跳、台账资源数、静默数、SSE 连接数、HTTP 请求数与耗时；
  支持 `INSPECTOR_METRICS_TOKEN`（Bearer）。
- **接口清单自动生成** `python -m app.tools.apidoc`（`make apidoc`）→ `docs/接口清单.md`（97 个接口 + 28 个权限点）；`tests/test_apidoc.py` 保证写接口都声明权限点。
- 登录页选项接口 `GET /auth/login-options`：返回平台版本；仅在非生产且开启演示数据时返回演示账号（生产不显示任何账号提示）。
- 权限点 22 → **28**：`silence:manage`、`report:config`、`notify:manage`、`task:edit`、`collect:manage`、`setting:manage`；升级时由迁移自动授予内置角色（运维负责人 5 个、值班运维 `silence:manage`；`setting:manage` 仅平台管理员；自定义角色不变）。
- 数据库迁移 **0003**：新增 `alarm_silence`、`collect_switch`、`sys_setting`、`worker_heartbeat` 4 张表，`alarm_event` 增加 `silence_id`、`inspection_task` 增加 `cron`，并给内置角色补授新权限点；共 25 张表。

### 部署（单机 docker compose，K8s 清单已删除）

- 新的 `deploy/` 目录即生产部署包：`docker-compose.yml`（postgres / redis / migrate / api / scheduler / notifier / web）+ 叠加文件
  `docker-compose.https.yml`、`docker-compose.demo.yml`、`docker-compose.build.yml`；`.env` 的 `COMPOSE_FILE` 决定叠加哪些。
- 运维脚本与 `deploy/Makefile`：`gen-env`（随机生成全部密钥与口令）、`install`（离线镜像 / 已有镜像 / 源码构建 → 迁移 → 启动 → 健康检查，可重复执行）、
  `upgrade`（先备份 → 新版本先迁移 → 替换进程；迁移失败保持旧版本）、`backup` / `restore`（pg_dump + `.env` + nginx / 证书，保留 14 份）、
  `status`、`probe`、`bundle`（离线安装包：镜像 tar + SHA256SUMS + 部署目录 + 文档）。
- nginx：访问来源白名单 `snippets/allowlist.conf`、`/metrics` 默认拒绝 + 白名单、安全响应头、HTTPS 叠加（证书放 `nginx/certs/`）、SSE 不缓冲、静态资源长缓存。
- **生产配置自检**：`INSPECTOR_ENV=prod` 时拒绝启动的情况——默认或过短的 JWT 密钥（< 32）、邮件链接密钥（< 16）、未设置凭据主密钥、使用 SQLite。
- 依赖锁定 `backend/constraints.txt`（镜像构建、CI、`make init` 统一使用）；`make lock` 重新生成。
- CI（`.gitlab-ci.yml`）重写为 check（后端 / 前端 / 部署文件检查）→ build（镜像）→ package（离线包，tag 触发）。

### 变更

- SSE 实时推送改为**一次性票据**：前端先 `POST /stream/ticket` 换 60 秒有效、只能用一次的票据，登录令牌不再出现在 URL 和访问日志中；“已使用”记录存 Redis（多个 API 进程共享），Redis 不可用时退回进程内记录。
- Element Plus 改为按需引入：首页入口 chunk gzip 约 308 KB → **102 KB**。
- 定时报表的发送时间 / 收件人从环境变量改为页面配置（环境变量仅作默认值）。
- 告警状态增加「已抑制 SUPPRESSED」；告警列表、详情中显示抑制原因与所属静默。
- `run_once` 支持 `data_cleanup`；`bootstrap` 写入 10 个内置任务（新增数据清理）。
- 文档：架构文档（实现版）、开发文档、部署运维手册、联调手册、后期开发方案、接口清单；README 重写。

### 质量

- 自动化用例 61 → **102**（新增静默、采集开关、系统设置、平台自身告警、数据清理、迁移、生产配置自检、status 工具、接口清单 / 权限声明）。
- ruff、mypy（app + mock_hcs）、vue-tsc 全部通过。

### 已知限制

- 镜像构建、`make install` / `upgrade` / `backup` / `restore` 脚本与 nginx 配置在开发沙箱中只做了语法与静态检查，**需在构建机 / 测试机实际演练**。
- 与真实 HCS 8.5.1 的接口字段映射基于官方文档与 Mock，需在局域网用 `make probe` 核对（见 `docs/联调手册.md`）。

## [0.3.0] - 2026-10-21

- **多租户接入**：每个租户独立选择账号密码（IAM Token，提前 1 小时刷新）或 AK/SK（SDK-HMAC-SHA256 签名）；凭据 AES-256-GCM 加密；连通性测试与能力探测（CES / Prometheus / 无）。
- **巡检告警**：DataArts 实时 / 批作业、MRS 集群 / 作业、GaussDB、DCS、ECS（状态 + CPU / 内存 / 磁盘）8 个巡检任务；35 条内置规则（状态 / 多级阈值、连续 N 次确认、滞回恢复）；
  告警去重 / 升级 / 降级 / 恢复 / 30 分钟内复发重开；邮件通知、手机端一键认领（HMAC 签名链接）、未认领按级别升级。
- **CCE 容器事件**：K8s pods / events 采集，10 类事件（重启、OOM、CrashLoop、背压、驱逐、探针失败、调度失败、镜像拉取失败、节点压力、扩缩容）+ 其他 Warning，聚合与告警，背压探针配置。
- **资源台账**：每小时同步 14 类资源，负责人 / 业务系统 / 环境 / 核心标记 / 备注维护，新增 / 变更 / 释放 / 状态变化留痕，疑似闲置识别，Excel 导出。
- **报表中心**：日报 / 周报 / 月报 / 台账报表，按租户或合并生成，定时邮件（正文摘要 + Excel 附件）、手动生成 / 导出 / 补发。
- **平台**：用户名密码登录（锁定策略、7 天免登录）、22 个权限点 + 4 个内置角色 + 自定义角色 + 租户数据范围、操作审计、告警大屏、SSE 实时推送。
- Mock HCS 8.5.1（两个租户）+ 演示数据；61 个自动化用例；docker compose 演示环境与 K8s 部署参考（v0.4 已移除 K8s）。
