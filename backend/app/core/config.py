import os
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全部配置项均可通过环境变量覆盖，前缀 INSPECTOR_，例如 INSPECTOR_DATABASE_URL。"""

    # env_parse_none_str=""：部署 .env 里留空的可选项（如 INSPECTOR_SMTP_CA_FILE=）按“未配置”处理
    model_config = SettingsConfigDict(env_file=".env", env_prefix="INSPECTOR_", extra="ignore", env_parse_none_str="")

    app_name: str = "HCS 巡检平台"
    env: str = "dev"                                  # dev / test / prod
    env_tag: str = "PROD"                             # 邮件主题中的环境标识
    database_url: str = "sqlite:///./dev.db"          # 生产：postgresql+psycopg://user:pwd@host:5432/inspector
    redis_url: str | None = None                      # 生产必配：分布式锁、事件推送
    jwt_secret: str = "change-me-in-prod-use-32+-random-bytes"   # 生产由 deploy/.env 覆盖（gen-env.sh 生成 ≥32 字节）
    jwt_expire_minutes: int = 12 * 60                 # 登录有效期（默认 12 小时）
    jwt_remember_days: int = 7                        # 勾选“7 天内免登录”时的有效期
    login_max_failures: int = 5                       # 连续输错 N 次临时锁定
    login_lock_minutes: int = 10
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:4173"]
    timezone: str = "Asia/Shanghai"
    master_key: str | None = None                     # base64 编码的 32 字节 AES-256-GCM 主密钥
    demo_seed: bool = True                            # 启动时写入演示数据（仅用于开发 / 演示；prod 环境强制忽略）
    init_admin_password: str | None = None            # 首次部署创建 admin 的口令（python -m app.workers.bootstrap）
    base_url: str = "http://localhost:5173"           # 邮件链接前缀（控制台地址）
    mail_link_secret: str = "change-me-too"
    metrics_token: str | None = None                  # /metrics 访问令牌（Authorization: Bearer）；为空时不校验，只应在内网暴露

    # 本机运行日志，每服务独立轮转文件；无需外部日志平台。
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_dir: str | None = None
    log_max_bytes: int = Field(10 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)
    log_backup_count: int = Field(5, ge=1, le=100)
    log_slow_request_ms: int = Field(2000, ge=100, le=600000)

    # SMTP（本地开发可用 mailpit：localhost:1025）
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_sender: str = "hcs-inspector@example.com"
    smtp_reply_to: str = "ops-oncall@example.com"
    smtp_starttls: bool = False
    smtp_ca_file: str | None = None

    # HCS 接入：以数据库中的租户（hcs_account / hcs_project，“系统管理 - 租户接入”维护）为准；
    # 下面的单账号配置只在一个租户都没有配置时作为兜底（兼容早期版本）
    hcs_version: str = "8.5.1"                        # 适配的 HCS 版本（接口路径 / 字段以该版本 OpenAPI 为准）
    hcs_ces_enabled: bool = False                    # 本局点无 CES；仅旧部署明确启用时兼容，AUTO 不探测 CES
    hcs_gaussdb_api_variant: str = Field("v3.3", pattern=r"^(v3\.3|v3\.2|v3\.1|opengauss_v3)$")
    hcs_scheme: str = "https"
    hcs_iam_url: str = "https://iam-apigateway-proxy.example.com"
    hcs_domain: str = "vdc_ops"
    hcs_username: str = "inspector_ro"
    hcs_password: str = ""
    hcs_project_id: str = ""
    hcs_region: str = "region-01"
    hcs_external_domain: str = "example.com"
    hcs_verify: bool = True                           # 校验 HCS 接口证书（内网自签证书请配 hcs_ca_file，不建议关闭校验）
    hcs_ca_file: str | None = None                    # 内部 CA 证书（PEM）路径，例如 /app/certs/hcs-ca.pem
    hcs_max_attempts: int = Field(3, ge=1, le=4)       # 含首次发送；只有登记的只读查询可重试
    hcs_service_concurrency: int = Field(2, ge=1, le=8)  # 每云服务跨任务在途请求上限
    hcs_total_qps: float = Field(10, ge=0.1, le=100)
    hcs_total_concurrency: int = Field(4, ge=1, le=16)
    hcs_acquire_timeout_seconds: float = Field(30, ge=1, le=120)
    hcs_endpoints: dict[str, str] = {
        "dataarts": "dayu-dlf.{region}.{domain}",
        "mrs": "mrs.{region}.{domain}",
        "gaussdb": "gaussdb-opengauss.{region}.{domain}",
        "dcs": "dcs.{region}.{domain}",
        "ces": "ces.{region}.{domain}",
        # 资源台账同步用到的服务（只读查询接口）
        "ecs": "ecs.{region}.{domain}",
        "evs": "evs.{region}.{domain}",
        "vpc": "vpc.{region}.{domain}",
        "elb": "vpc.{region}.{domain}",
        "cce": "cce.{region}.{domain}",
        "dws": "dws.{region}.{domain}",
        # CCE 集群 Kubernetes API（经 CCE API 网关访问，按集群 ID 区分）：容器事件采集用
        "cce_k8s": "{cluster_id}.cce.{region}.{domain}",
    }
    hcs_workspaces: list[str] = []
    hcs_mock_url: str | None = None                   # 仅开发联调：全部租户的 IAM / 服务 Endpoint 指向 Mock HCS（prod 忽略）

    # CCE 容器事件
    pod_event_retention_days: int = 30                # 容器事件保留天数
    pod_backpressure_pattern: str = r"(?i)back-?pressure|背压|反压"   # K8s Event 的 reason / message 命中即记为“背压”
    pod_backpressure_recover_min: int = 5             # 背压事件超过 N 分钟没有再出现，视为背压结束

    # 巡检任务周期（秒）
    schedule: dict[str, int] = {"dcs_status": 60, "dgc_realtime_status": 60}

    # 报表：定时生成并邮件发送（crontab 格式，时区取 timezone）；收件人为空时只生成不发送
    report_cron: dict[str, str] = {"DAILY": "30 8 * * *", "WEEKLY": "0 9 * * mon", "MONTHLY": "0 9 1 * *"}
    report_mail_to: list[str] = []


@lru_cache
def get_settings() -> Settings:
    env_file = os.environ.get("INSPECTOR_ENV_FILE", ".env")
    return Settings(_env_file=env_file or None)  # type: ignore[call-arg]  # BaseSettings 的运行时参数，mypy 插件未暴露。


_DEFAULT_SECRETS = {"change-me-in-prod-use-32+-random-bytes", "change-me-too", "please-change-me-to-32-plus-random-bytes",
                    "please-change-me-too"}


def prod_problems(s: Settings) -> list[str]:
    """生产环境（env=prod）启动自检：返回不满足项。API / 调度 / 通知 / 初始化进程启动时调用，有问题直接退出。"""
    if s.env != "prod":
        return []
    out = []
    if s.jwt_secret in _DEFAULT_SECRETS or len(s.jwt_secret) < 32:
        out.append("INSPECTOR_JWT_SECRET 仍是默认值或短于 32 字符（openssl rand -hex 32）")
    if s.mail_link_secret in _DEFAULT_SECRETS or len(s.mail_link_secret) < 16:
        out.append("INSPECTOR_MAIL_LINK_SECRET 仍是默认值或短于 16 字符")
    if not s.master_key:
        out.append("未配置 INSPECTOR_MASTER_KEY（租户凭据加密主密钥，openssl rand -base64 32，务必备份）")
    if s.database_url.startswith("sqlite"):
        out.append("生产环境请使用 PostgreSQL（INSPECTOR_DATABASE_URL=postgresql+psycopg://...）")
    return out


def ensure_prod_ready(s: Settings | None = None) -> None:
    problems = prod_problems(s or get_settings())
    if problems:
        raise SystemExit("生产配置自检未通过（修改部署目录 .env 后重新启动）：\n  - " + "\n  - ".join(problems))
