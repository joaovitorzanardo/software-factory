import os
import unittest
from datetime import datetime
from email.message import EmailMessage
from typing import Any
from unittest import mock
from zoneinfo import ZoneInfo

from software_factory.notify.email import (
    EmailSettings,
    build_usage_alert_email,
    send_email,
)
from software_factory.orchestration.models import UsageAlert

RESET = 1795440000
SUBJECT = "[software-factory] 80% da sessão do Claude usada"
EMAIL = EmailSettings(
    host="smtp.example.com",
    port=587,
    username="bot@example.com",
    password="app-password",
    sender="bot@example.com",
    to="dev@example.com",
    timezone="America/Sao_Paulo",
)


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[str] = []
        self.login_args: tuple[str, str] | None = None
        self.sent: list[EmailMessage] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.calls.append("quit")

    def starttls(self) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append("login")
        self.login_args = (user, password)

    def send_message(self, msg: EmailMessage) -> None:
        self.calls.append("send")
        self.sent.append(msg)


class TestBuildUsageAlertEmail(unittest.TestCase):
    def test_build_usage_alert_email(self) -> None:
        alert = UsageAlert("five_hour", 0.834, RESET, "implement-issue-abc/implement_plan")
        subject, body = build_usage_alert_email(alert, "America/Sao_Paulo")
        self.assertEqual(subject, SUBJECT)
        self.assertIn("83% de uso", body)
        reset = datetime.fromtimestamp(RESET, ZoneInfo("America/Sao_Paulo"))
        self.assertIn(
            f"{reset:%d/%m/%Y} às {reset:%H:%M} (America/Sao_Paulo)", body
        )
        self.assertIn("Detectado em: implement-issue-abc/implement_plan", body)

    def test_without_source(self) -> None:
        _, body = build_usage_alert_email(UsageAlert("five_hour", 0.8, RESET), "UTC")
        self.assertNotIn("Detectado em", body)


class TestSendEmail(unittest.TestCase):
    def test_send_email(self) -> None:
        FakeSMTP.instances.clear()
        send_email(EMAIL, SUBJECT, "corpo", smtp_cls=FakeSMTP)
        [smtp] = FakeSMTP.instances
        self.assertEqual((smtp.host, smtp.port), ("smtp.example.com", 587))
        self.assertEqual(smtp.calls, ["starttls", "login", "send", "quit"])
        self.assertEqual(smtp.login_args, ("bot@example.com", "app-password"))
        [msg] = smtp.sent
        self.assertEqual(msg["From"], "bot@example.com")
        self.assertEqual(msg["To"], "dev@example.com")
        self.assertEqual(msg["Subject"], SUBJECT)
        self.assertEqual(msg.get_content().strip(), "corpo")


class TestEmailSettingsFromEnv(unittest.TestCase):
    SMTP = {
        "SMTP_HOST": "smtp.example.com",
        "SMTP_USERNAME": "bot@example.com",
        "SMTP_PASSWORD": "pw",
        "ALERT_EMAIL_TO": "dev@example.com",
    }

    def test_disabled_without_host(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(EmailSettings.from_env())

    def test_requires_recipient(self) -> None:
        env = {k: v for k, v in self.SMTP.items() if k != "ALERT_EMAIL_TO"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                EmailSettings.from_env()

    def test_defaults(self) -> None:
        with mock.patch.dict(os.environ, self.SMTP, clear=True):
            settings = EmailSettings.from_env()
        assert settings is not None
        self.assertEqual(settings.port, 587)
        self.assertEqual(settings.sender, "bot@example.com")
        self.assertEqual(settings.timezone, "America/Sao_Paulo")
        self.assertEqual(settings.to, "dev@example.com")


if __name__ == "__main__":
    unittest.main()
