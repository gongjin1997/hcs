# ARM64 离线包验收

> 本页为 2026-10-27 初版历史记录，所列哈希不对应当前已更新首包。目标机要求已更新为 Docker 与独立 `docker-compose`；本页初版的 Compose v2 字样不适用于当前部署。当前开关默认值和仅代码发布验收见 [代码更新与默认开关验收.md](代码更新与默认开关验收.md)。

日期：2026-10-27。交付物为正式环境镜像和一键部署脚本；本轮使用独立数据库与 QEMU 验证，未连接原生产数据库、真实 HCS 或目标 ARM64 主机。

## 交付物

- 安装包：`deploy/dist/hcs-inspector-0.4.0-arm64-offline.tar.gz`
- 大小：237,938,088 字节（226.9 MiB）。
- 校验文件：同目录 `.tar.gz.sha256`；包内另有文件校验和、四个镜像的架构及 manifest/config digest。
- 入口：解压后 `bash deploy.sh`。目标机需要 ARM64 Linux、Docker 和 Compose v2，无需 Node / Python / make。

SHA256：

```text
b848dceaf71f47586e9a1c661cd5c32c8476a83d0bdb9c4ff4488fcce3f73807
```

| 镜像 | 实际版本 | 架构核验 |
| --- | --- | --- |
| `hcs-inspector-arm64-backend:0.4.0` | 应用 0.4.0 / Python 3.12 | AArch64；Python、Pydantic Core、cryptography 原生库 ELF 183；psycopg binary 加载成功 |
| `hcs-inspector-arm64-web:0.4.0` | Nginx 1.27.5 | AArch64，ELF 183 |
| `hcs-inspector-arm64-postgres:16-alpine` | PostgreSQL 16.15 | AArch64，ELF 183 |
| `hcs-inspector-arm64-redis:7-alpine` | Redis 7.4.11 | AArch64，ELF 183；模拟运行限制见下文 |

## 已执行

- 从实际安装包校验并 `docker load` 全部镜像；七个服务均固定 `linux/arm64`、`pull_policy: never`，其中 migrate 为一次性服务。
- 正式配置首次初始化：数据库迁移到 `0007`；仅创建首个管理员及系统基础记录，租户数与资源数均为 0，登录接口不提供演示账号。
- API、Web、PostgreSQL、Redis、scheduler、notifier 六个容器均健康；API 保留默认两个 worker。
- 使用临时验证记录核对配置：停止全部服务后再次执行 `bash deploy.sh`，原 `.env` 字节未变，租户密文未重写且仍可解密，Endpoint、任务参数/周期/停用状态、页面设置及原 Nginx 配置保留。
- 实际执行数据库备份并核对包内容与 600 权限；实际 HTTP 导出 XLSX 并读取 15 个工作表。
- 在独立浏览器中登录，检查总览、台账、DCS、系统管理、大屏；脚本错误和外部资源请求均为 0。登录页没有演示账号及旧版权页脚。
- `make check` 通过：后端 1258 项通过、21 项跳过，前端 451 项通过；另有部署脚本 22 项通过，覆盖错误架构、损坏归档、旧镜像 ID、配置保留和恢复取消等场景。
- 修复并实测：严格文件权限导致非 root 用户不能导入新源码；镜像内补充读取权限，宿主机源码权限不改。
- 完成后清理临时容器、网桥、数据卷、数据库和随机凭据；原部署配置与既有局域网服务未操作。

## 验证边界

1. **未在目标 ARM64 真机上执行部署。** 镜像内容是真正的 AArch64 二进制；运行验证由 x86_64 构建机通过 QEMU 执行。
2. QEMU 场景中 Redis 的 `ARM64-COW-BUG` 内核自检触发退出。为继续应用联调，仅在临时验收覆盖文件中添加忽略选项；**此文件和选项均未打入安装包，正式配置保留 Redis 保护检查**。不能据此声称已验证目标机内核兼容性，也不能据此认定目标机有该缺陷。官方检查实现见 [Redis syscheck.c](https://github.com/redis/redis/blob/7.4.11/src/syscheck.c)。
3. 验收使用禁止外网访问的内部网桥，HTTP 从主机直接访问容器网桥地址；该覆盖方式不发布宿主机端口。安装包使用原生产 Compose 端口配置，未包含该验收覆盖文件。
4. 租户与凭据保留测试使用临时记录，不是客户生产租户；安装包不包含真实 `.env`、证书、数据库、备份、Mock 服务或演示租户数据。跨主机迁移正式配置仍须保留原数据库和匹配的加密主密钥。

操作步骤见 [ARM64离线部署.md](ARM64离线部署.md)。
