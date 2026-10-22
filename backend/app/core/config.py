from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全部配置项均可通过环境变量覆盖，前缀 INSPECTOR_，例如 INSPECTOR_DATABASE_URL。"""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="INSPECTOR_", extra="ignore")

    app_name: str = "HCS 巡检平台"
    env: str = "dev"                                  # dev / test / prod
    env_tag: str = "PROD"                             # 邮件主题中的环境标识
    database_url: str = "sqlite:///./dev.db"          # 生产：postgresql+psycopg://user:pwd@host:5432/inspector
    redis_url: str | None = None                      # 生产必配：分布式锁、事件推送
    jwt_secret: str = "change-me-in-prod-use-32+-random-bytes"   # 生产必须通过 Secret 覆盖（≥32 字节随机串）
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
    hcs_scheme: str = "https"
    hcs_iam_url: str = "https://iam-apigateway-proxy.example.com"
    hcs_domain: str = "vdc_ops"
    hcs_username: str = "inspector_ro"
    hcs_password: str = ""
    hcs_project_id: str = ""
    hcs_region: str = "region-01"
    hcs_external_domain: str = "example.com"
    hcs_verify: bool = True
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
        "elb": "elb.{region}.{domain}",
        "rds": "rds.{region}.{domain}",
        "dms": "dms.{region}.{domain}",
        "cce": "cce.{region}.{domain}",
        "dws": "dws.{region}.{domain}",
        "css": "css.{region}.{domain}",
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
    return Settings()
