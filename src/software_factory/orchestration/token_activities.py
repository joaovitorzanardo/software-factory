import asyncio

from temporalio import activity

from software_factory.orchestration.models import UsageReportRequest
from software_factory.orchestration.token_store import TokenStore
from software_factory.orchestration.token_usage import render_usage_comment


class TokenUsageActivities:
    def __init__(self, store: TokenStore) -> None:
        self.store = store

    @activity.defn
    async def token_usage_report(self, request: UsageReportRequest) -> str:
        """Corpo do comentário de tokens da issue, montado a partir do SQLite."""
        usage = await asyncio.to_thread(self.store.issue_usage, request.issue_id)
        return render_usage_comment(usage, request.final)
