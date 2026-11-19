from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from software_factory.orchestration.models import UsageAlert


@workflow.defn
class UsageAlertWorkflow:
    """Envia o aviso de uso de uma janela. O id (um por janela) é a deduplicação."""

    @workflow.run
    async def run(self, alert: UsageAlert) -> None:
        await workflow.execute_activity(
            "send_usage_alert",
            alert,
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=RetryPolicy(
                maximum_attempts=10,
                initial_interval=timedelta(seconds=10),
                backoff_coefficient=2,
            ),
        )
