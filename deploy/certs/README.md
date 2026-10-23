后端容器信任的内部 CA 证书目录（只读挂载到 api / scheduler / notifier 容器的 /app/certs）：

- 局点 HCS API 网关是内部 CA 签发的证书：放 `hcs-ca.pem`，.env 设置 `INSPECTOR_HCS_CA_FILE=/app/certs/hcs-ca.pem`
- 邮件服务器 STARTTLS 用内部 CA：放 `smtp-ca.pem`，.env 设置 `INSPECTOR_SMTP_CA_FILE=/app/certs/smtp-ca.pem`

不建议用 `INSPECTOR_HCS_VERIFY=false` 关闭证书校验（仅限临时排查）。
