"""SMTP 发信与只读邮件连接测试；外部异常统一映射，避免凭据进入 Outbox、日志或 API。"""
import imaplib
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from time import perf_counter

from app.core.config import Settings
from app.services import mail_settings_service
from app.services.mail_settings_service import ImapRuntime, SmtpRuntime

log = logging.getLogger(__name__)


def _log_failure(event: str, exc: Exception, started: float, protocol: str = "smtp") -> None:
    log.warning(event, extra={"event": event, "protocol": protocol, "exception_type": type(exc).__name__,
                "cost_ms": round((perf_counter() - started) * 1000, 2), "status": "FAILED"},
                exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))


class MailTransportError(Exception):
    pass


def safe_error(exc: Exception) -> str:
    """只按异常类型映射，绝不拼接服务器响应、异常文本、账号或口令。"""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "SMTP 认证失败，请检查账号、密码或授权码"
    if isinstance(exc, imaplib.IMAP4.error):
        return "IMAP 连接或认证失败，请检查账号、密码和服务器设置"
    if isinstance(exc, ssl.SSLError):
        return "邮件服务器 TLS 校验或握手失败，请检查证书与加密方式"
    if isinstance(exc, TimeoutError):
        return "邮件服务器连接超时，请检查网络与端口"
    if isinstance(exc, ConnectionRefusedError):
        return "邮件服务器拒绝连接（Connection refused），请检查主机与端口"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "SMTP 拒绝收件人，请检查邮箱地址与中继策略"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "SMTP 拒绝发件人，请检查发件地址与中继策略"
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return "SMTP 不支持所选加密或认证方式"
    return "邮件服务连接或认证失败，请检查邮件设置和服务器状态"


def _settings(settings: Settings | SmtpRuntime | None) -> SmtpRuntime:
    if isinstance(settings, SmtpRuntime):
        return settings
    if settings is not None:
        return mail_settings_service.from_environment(settings).smtp
    return mail_settings_service.get().smtp


def build_message(*, subject: str, html: str, text: str, recipients: list[str], message_id: str,
                  thread_root: str | None = None, severity: str | None = None, alarm_id: int | None = None,
                  fingerprint: str | None = None, settings: Settings | SmtpRuntime | None = None) -> EmailMessage:
    started = perf_counter()
    try:
        s = _settings(settings)
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr(("HCS巡检平台", s.sender))
        msg["To"] = ", ".join(recipients)
        if s.reply_to:
            msg["Reply-To"] = s.reply_to
        msg["Message-ID"] = message_id
        if thread_root and thread_root != message_id:
            msg["In-Reply-To"] = thread_root
            msg["References"] = thread_root
        if severity == "P1":
            msg["X-Priority"] = "1"
            msg["Importance"] = "High"
        if alarm_id is not None:
            msg["X-HCS-Alarm-Id"] = str(alarm_id)
        if severity:
            msg["X-HCS-Severity"] = severity
        if fingerprint:
            msg["X-HCS-Fingerprint"] = fingerprint
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        return msg
    except Exception as exc:
        _log_failure("mail_build_failed", exc, started)
        raise MailTransportError("邮件构建失败，请检查邮件设置") from None


def _connect_smtp(s: SmtpRuntime) -> smtplib.SMTP:
    smtp: smtplib.SMTP
    if s.security == "SSL":
        smtp = smtplib.SMTP_SSL(s.host, s.port, timeout=15, context=ssl.create_default_context(cafile=s.ca_file))
    else:
        smtp = smtplib.SMTP(s.host, s.port, timeout=15)
    try:
        if s.security == "STARTTLS":
            smtp.starttls(context=ssl.create_default_context(cafile=s.ca_file))
        if s.auth:
            if not s.username or not s.password.get_secret_value():
                raise MailTransportError("SMTP 认证需要账号和密码")
            smtp.login(s.username, s.password.get_secret_value())
        return smtp
    except Exception:
        smtp.close()
        raise


def send(msg: EmailMessage, settings: Settings | SmtpRuntime | None = None) -> None:
    started = perf_counter()
    try:
        with _connect_smtp(_settings(settings)) as smtp:
            refused = smtp.send_message(msg)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
    except Exception as exc:
        _log_failure("smtp_send_failed", exc, started)
        raise MailTransportError(safe_error(exc)) from None
    log.debug("smtp_send_finished", extra={"event": "smtp_send_finished", "status": "SUCCESS",
                                         "cost_ms": round((perf_counter() - started) * 1000, 2)})


def check_smtp(s: SmtpRuntime) -> None:
    started = perf_counter()
    try:
        with _connect_smtp(s) as smtp:
            code, _ = smtp.noop()
            if code != 250:
                raise MailTransportError("SMTP NOOP 失败")
    except Exception as exc:
        _log_failure("smtp_check_failed", exc, started)
        raise MailTransportError(safe_error(exc)) from None


def check_imap(s: ImapRuntime) -> None:
    started = perf_counter()
    client: imaplib.IMAP4 | None = None
    try:
        if not s.enabled or not s.host or not s.username or not s.password.get_secret_value():
            raise MailTransportError("请启用 IMAP 并填写服务器、账号和密码")
        if s.security == "SSL":
            client = imaplib.IMAP4_SSL(s.host, s.port, ssl_context=ssl.create_default_context(), timeout=15)
        else:
            client = imaplib.IMAP4(s.host, s.port, timeout=15)
        if s.security == "STARTTLS":
            client.starttls(ssl_context=ssl.create_default_context())
        login_status, _ = client.login(s.username, s.password.get_secret_value())
        if login_status != "OK":
            raise MailTransportError("IMAP 认证失败")
        noop_status, _ = client.noop()
        if noop_status != "OK":
            raise MailTransportError("IMAP NOOP 失败")
    except Exception as exc:
        _log_failure("imap_check_failed", exc, started, "imap")
        raise MailTransportError(safe_error(exc)) from None
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                # 不调用 CLOSE / EXPUNGE，也不选择或处理任何邮件。
                try:
                    client.shutdown()
                except Exception:
                    pass
