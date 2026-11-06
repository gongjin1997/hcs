from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JsonType, UtcDateTime

from .base import TimestampMixin


class HcsAccount(TimestampMixin, Base):
    """租户接入：一个 HCS 租户（VDC / 账号域）一条记录。平台用它登录租户调用只读 OpenAPI。

    - auth_type=PASSWORD：IAM 用户名 + 密码换取 Token（X-Auth-Token），Token 缓存并提前刷新
    - auth_type=AKSK：每个请求用 AK/SK 签名（SDK-HMAC-SHA256），不需要 Token
    - 密码 / SK 用 AES-256-GCM 加密后存 secret_cipher，接口只返回脱敏值
    """

    __tablename__ = "hcs_account"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)              # 租户代码，例如 tenant-a（告警指纹 / 筛选）
    name: Mapped[str] = mapped_column(String(64), unique=True)              # 显示名，例如 “租户A · 核心生产”
    short_name: Mapped[str] = mapped_column(String(16), default="")         # 简称，邮件主题 / 标签用，例如 “租户A”
    color: Mapped[str] = mapped_column(String(16), default="#1d63f0")       # 界面标识色
    iam_endpoint: Mapped[str] = mapped_column(String(255))
    domain_name: Mapped[str] = mapped_column(String(128))
    domain_id: Mapped[str | None] = mapped_column(String(64))               # 账号 ID：密码认证可替代账号名；全局签名请求使用
    auth_type: Mapped[str] = mapped_column(String(16), default="PASSWORD")  # PASSWORD / AKSK
    username: Mapped[str | None] = mapped_column(String(128))               # PASSWORD：IAM 用户名
    access_key: Mapped[str | None] = mapped_column(String(128))             # AKSK：AK（非机密，界面脱敏展示）
    secret_cipher: Mapped[str] = mapped_column(Text, default="")            # 密码或 SK 的密文
    endpoints: Mapped[dict] = mapped_column(JsonType, default=dict)         # 服务 → host 模板 / 完整 URL
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    report_user_ids: Mapped[list] = mapped_column(JsonType, default=list, server_default="[]")
    # 旧邮箱快照仅供迁移、降级；运行时使用 report_user_ids。
    report_mail_to: Mapped[list] = mapped_column(JsonType, default=list)
    # 来源：AUTO（可选 Prometheus，否则原生字段）/ CES（需全局显式启用）/ PROMETHEUS / NONE；旧 AOM 值保留但全局停用
    metrics_source: Mapped[str] = mapped_column(String(16), default="AUTO")
    # ManageOne 独立接入，默认关闭；不继承 IAM 凭据或自动启用采集。
    manageone_config: Mapped[dict] = mapped_column(JsonType, default=dict, server_default="{}")
    prometheus_url: Mapped[str | None] = mapped_column(String(255))         # 可选：租户侧 Prometheus HTTP API 地址
    # 能力探测结果 {ces|prometheus|cce_api: {available, reason, at}}；旧 aom 仅历史记录，不探测、不选源
    capabilities: Mapped[dict] = mapped_column(JsonType, default=dict)
    remark: Mapped[str | None] = mapped_column(Text)
    last_check: Mapped[dict] = mapped_column(JsonType, default=dict)        # 最近一次连通性测试 {at, ok, checks}
    last_check_at: Mapped[datetime | None] = mapped_column(UtcDateTime())


class HcsProject(Base):
    __tablename__ = "hcs_project"
    __table_args__ = (UniqueConstraint("account_id", "region_id", "project_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("hcs_account.id"))
    region_id: Mapped[str] = mapped_column(String(64))
    project_id: Mapped[str] = mapped_column(String(64))
    project_name: Mapped[str | None] = mapped_column(String(128))
    external_domain: Mapped[str] = mapped_column(String(128))
    env: Mapped[str] = mapped_column(String(16), default="PROD")
    workspaces: Mapped[list] = mapped_column(JsonType, default=list)           # DataArts 工作空间
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
