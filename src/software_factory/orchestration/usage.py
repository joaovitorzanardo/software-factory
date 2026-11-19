"""Detecção do uso da sessão do Claude nos eventos `rate_limit_event` do stream-json."""

import json
from typing import Any

from software_factory.orchestration.models import UsageAlert

# "Sessão" da assinatura: a janela de 5 horas. As janelas de 7 dias não avisam.
SESSION_WINDOW = "five_hour"
SESSION_ALERT_THRESHOLD = 0.8


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def session_usage(event: dict[str, Any]) -> UsageAlert | None:
    """O alerta se o evento mostra a janela de sessão em ou acima do limiar; senão None."""
    if event.get("type") != "rate_limit_event":
        return None
    info = event.get("rate_limit_info")
    if not isinstance(info, dict):
        return None
    windows = info.get("unifiedWindows")
    if isinstance(windows, dict):
        window = windows.get(SESSION_WINDOW)
    elif info.get("rateLimitType") == SESSION_WINDOW:
        # CLI mais velho, sem unifiedWindows: o topo descreve a janela que limita agora.
        window = info
    else:
        window = None
    if not isinstance(window, dict):
        return None
    utilization, resets_at = window.get("utilization"), window.get("resetsAt")
    if not _number(utilization) or not _number(resets_at):
        return None
    if utilization < SESSION_ALERT_THRESHOLD:
        return None
    return UsageAlert(window=SESSION_WINDOW, utilization=utilization, resets_at=int(resets_at))


def usage_alert_workflow_id(alert: UsageAlert) -> str:
    return f"usage-alert-{alert.window.replace('_', '-')}-{alert.resets_at}"


class UsageWatcher:
    """Remonta as linhas do stdout em chunks e emite no máximo um alerta por janela."""

    def __init__(self, source: str) -> None:
        self.source = source
        self._pending = ""
        self._seen: set[int] = set()

    def feed(self, chunk: str) -> list[UsageAlert]:
        *lines, self._pending = (self._pending + chunk).split("\n")
        alerts: list[UsageAlert] = []
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            alert = session_usage(event) if isinstance(event, dict) else None
            if alert is None or alert.resets_at in self._seen:
                continue
            self._seen.add(alert.resets_at)
            alerts.append(UsageAlert(alert.window, alert.utilization, alert.resets_at, self.source))
        return alerts
