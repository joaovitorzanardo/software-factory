"""Envio de email por SMTP (stdlib) para os avisos do worker."""

import os
import smtplib
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Any
from zoneinfo import ZoneInfo

from software_factory.orchestration.models import UsageAlert

DEFAULT_SMTP_PORT = 587
DEFAULT_ALERT_TIMEZONE = "America/Sao_Paulo"
SMTP_TIMEOUT_S = 30
USAGE_ALERT_SUBJECT = "[software-factory] 80% da sessão do Claude usada"


@dataclass(frozen=True)
class EmailSettings:
    host: str
    port: int
    username: str
    password: str
    sender: str
    to: str
    timezone: str = DEFAULT_ALERT_TIMEZONE

    def __repr__(self) -> str:
        # A senha não vai para logs nem tracebacks.
        return f"EmailSettings(host={self.host!r}, port={self.port}, to={self.to!r})"

    @classmethod
    def from_env(cls) -> "EmailSettings | None":
        """None sem SMTP_HOST (aviso desligado); com ele, o resto é obrigatório."""
        # Import tardio: config importa este módulo.
        from software_factory.config import _required

        host = os.environ.get("SMTP_HOST")
        if not host:
            return None
        username = _required("SMTP_USERNAME")
        return cls(
            host=host,
            port=int(os.environ.get("SMTP_PORT") or DEFAULT_SMTP_PORT),
            username=username,
            password=_required("SMTP_PASSWORD"),
            sender=os.environ.get("ALERT_EMAIL_FROM") or username,
            to=_required("ALERT_EMAIL_TO"),
            timezone=os.environ.get("ALERT_TIMEZONE") or DEFAULT_ALERT_TIMEZONE,
        )


def build_usage_alert_email(alert: UsageAlert, timezone: str) -> tuple[str, str]:
    reset = datetime.fromtimestamp(alert.resets_at, ZoneInfo(timezone))
    lines = [
        "A sessão atual do Claude Code (janela de 5 horas da assinatura) chegou a "
        f"{round(alert.utilization * 100)}% de uso.",
        f"O limite será resetado em {reset:%d/%m/%Y} às {reset:%H:%M} ({timezone}).",
    ]
    if alert.source:
        lines += ["", f"Detectado em: {alert.source}"]
    return USAGE_ALERT_SUBJECT, "\n".join(lines) + "\n"


def send_email(
    settings: EmailSettings, subject: str, body: str, smtp_cls: Any = smtplib.SMTP
) -> None:
    """Envia por SMTP com STARTTLS. Síncrona: chame com asyncio.to_thread."""
    msg = EmailMessage()
    msg["From"] = settings.sender
    msg["To"] = settings.to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtp_cls(settings.host, settings.port, timeout=SMTP_TIMEOUT_S) as smtp:
        smtp.starttls()
        smtp.login(settings.username, settings.password)
        smtp.send_message(msg)
