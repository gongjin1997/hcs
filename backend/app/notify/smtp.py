import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from app.core.config import Settings, get_settings


def build_message(*, subject: str, html: str, text: str, recipients: list[str], message_id: str,
                  thread_root: str | None = None, severity: str | None = None, alarm_id: int | None = None,
                  fingerprint: str | None = None, settings: Settings | None = None) -> EmailMessage:
    s = settings or get_settings()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("HCS巡检平台", s.smtp_sender))
    msg["To"] = ", ".join(recipients)
    msg["Reply-To"] = s.smtp_reply_to
    msg["Message-ID"] = message_id
    if thread_root and thread_root != message_id:   # 同一告警的后续邮件归入同一会话
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


def send(msg: EmailMessage, settings: Settings | None = None) -> None:
    s = settings or get_settings()
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
        if s.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context(cafile=s.smtp_ca_file))
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password or "")
        smtp.send_message(msg)
