import asyncio
import logging

from dotenv import load_dotenv
from temporalio.client import Client
from temporalio.worker import Worker

from software_factory.config import FactorySettings
from software_factory.github.client import github_clients, static_token
from software_factory.linear.client import LinearClient
from software_factory.orchestration.activities import SandboxActivities
from software_factory.orchestration.token_activities import TokenUsageActivities
from software_factory.orchestration.token_store import TokenStore
from software_factory.orchestration.github_activities import GitHubActivities
from software_factory.orchestration.implement_workflow import ImplementIssueWorkflow
from software_factory.orchestration.linear_activities import LinearActivities
from software_factory.orchestration.notify_activities import NotifyActivities, UsageAlertNotifier
from software_factory.orchestration.reconcile_activities import ReconcileActivities
from software_factory.orchestration.reconcile_workflow import ReconcileIssuesWorkflow
from software_factory.orchestration.repo_activities import RepoActivities
from software_factory.orchestration.review_activities import ReviewActivities
from software_factory.orchestration.review_workflow import ReviewIssueWorkflow
from software_factory.orchestration.usage_alert_workflow import UsageAlertWorkflow
from software_factory.orchestration.workflow import PlanIssueWorkflow


async def run_worker(settings: FactorySettings) -> None:
    logger = logging.getLogger(__name__)
    client = await Client.connect(settings.temporal_address)
    workflows: list[type] = [
        PlanIssueWorkflow,
        ImplementIssueWorkflow,
        ReviewIssueWorkflow,
        ReconcileIssuesWorkflow,
    ]
    alert_activities: list = []
    notifier = None
    if settings.alert_email:
        notifier = UsageAlertNotifier(client, settings.task_queue)
        workflows.append(UsageAlertWorkflow)
        alert_activities.append(NotifyActivities(settings.alert_email).send_usage_alert)
    else:
        logger.info("aviso de uso desligado: SMTP_HOST não definida")
    token_store = TokenStore(settings.token_usage_db)
    github_tokens = static_token(settings.github_token)
    clients = github_clients(github_tokens)
    activities = SandboxActivities(
        settings,
        github_tokens,
        usage_notifier=notifier,
        token_store=token_store,
    )
    tokens = TokenUsageActivities(token_store)
    review = ReviewActivities(activities)
    linear_client = LinearClient(settings.linear_api_key)
    linear = LinearActivities(linear_client)
    reconcile = ReconcileActivities(linear_client, client, settings.task_queue)
    github = GitHubActivities(clients)
    repo = RepoActivities(linear_client, clients)
    worker = Worker(
        client,
        task_queue=settings.task_queue,
        workflows=workflows,
        activities=[
            activities.create_sandbox,
            activities.clone_repo,
            activities.generate_plan,
            activities.commit_plan,
            activities.clone_branch,
            activities.implement_plan,
            activities.analyze_failure,
            activities.implement_fix,
            activities.run_tests,
            activities.commit_implementation,
            activities.kill_sandbox,
            review.review_pull,
            review.implement_review_fix,
            linear.move_issue,
            linear.post_comment,
            linear.upsert_comment,
            linear.create_issue,
            tokens.token_usage_report,
            github.plan_exists,
            github.open_pull_request,
            github.pull_request_info,
            github.merge_pull_request,
            reconcile.list_pending_issues,
            reconcile.start_missed_workflow,
            repo.resolve_repo,
            *alert_activities,
        ],
    )
    logger.info("worker ouvindo task queue %s", settings.task_queue)
    await worker.run()


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker(FactorySettings.from_env()))


if __name__ == "__main__":
    main()
