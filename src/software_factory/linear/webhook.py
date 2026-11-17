import json
import logging
import time

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from temporalio.service import RPCError, RPCStatusCode

from software_factory.config import Settings
from software_factory.linear.events import (
    CommentReply,
    LinearIssue,
    parse_issue,
    parse_reply,
    should_implement,
    should_process,
    should_review,
)
from software_factory.linear.signature import is_fresh, verify_signature
from software_factory.orchestration.dispatch import (
    KIND_IMPLEMENT,
    KIND_PLAN,
    KIND_REVIEW,
    start_issue_workflow,
)
from software_factory.orchestration.implement_workflow import ImplementIssueWorkflow
from software_factory.orchestration.models import PlanIssueInput
from software_factory.orchestration.review_workflow import ReviewIssueWorkflow

logger = logging.getLogger(__name__)

router = APIRouter()


async def handle_issue(
    client: Any,
    task_queue: str,
    issue: LinearIssue,
    delivery_id: str | None,
    kind: str = KIND_PLAN,
) -> bool:
    """Inicia o workflow `kind` da issue; retorna False se já há um rodando para ela."""
    return await start_issue_workflow(
        client,
        task_queue,
        kind,
        PlanIssueInput(
            issue_id=issue.id,
            identifier=issue.identifier,
            title=issue.title,
            description=issue.description,
            url=issue.url,
        ),
        f"delivery={delivery_id}",
    )


async def handle_reply(client: Any, reply: CommentReply, delivery_id: str | None) -> bool:
    """Entrega a resposta aos workflows de implementação e de review da issue.

    O que não tem pergunta pendente ignora o signal. False se nenhum está rodando.
    """
    delivered = False
    for prefix, workflow_cls in (
        ("implement-issue", ImplementIssueWorkflow),
        ("review-issue", ReviewIssueWorkflow),
    ):
        workflow_id = f"{prefix}-{reply.issue_id}"
        try:
            await client.get_workflow_handle(workflow_id).signal(
                workflow_cls.human_answer, reply.answer
            )
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                logger.info("sem workflow %s rodando; delivery=%s", workflow_id, delivery_id)
                continue
            raise
        logger.info("resposta enviada a %s; delivery=%s", workflow_id, delivery_id)
        delivered = True
    return delivered


@router.post("/webhooks/linear")
async def linear_webhook(request: Request) -> dict[str, str]:
    settings: Settings = request.app.state.settings
    body = await request.body()

    if not verify_signature(
        body, request.headers.get("Linear-Signature"), settings.linear_webhook_secret
    ):
        raise HTTPException(status_code=401, detail="assinatura inválida")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="JSON inválido")

    timestamp = payload.get("webhookTimestamp") if isinstance(payload, dict) else None
    if not isinstance(timestamp, int) or not is_fresh(timestamp, int(time.time() * 1000)):
        raise HTTPException(status_code=400, detail="webhookTimestamp ausente ou expirado")

    delivery_id = request.headers.get("Linear-Delivery")
    reply = parse_reply(payload)
    if reply is not None:
        try:
            delivered = await handle_reply(request.app.state.temporal, reply, delivery_id)
        except Exception:
            logger.exception("falha ao enviar resposta; delivery=%s", delivery_id)
            raise HTTPException(status_code=503, detail="orquestrador indisponível")
        return {"status": "accepted" if delivered else "ignored"}

    # Label adicionada e status mudado no mesmo evento: o planejamento vence.
    if should_process(payload, settings.trigger_labels):
        kind = KIND_PLAN
    elif should_implement(payload, settings.trigger_labels):
        kind = KIND_IMPLEMENT
    elif should_review(payload, settings.trigger_labels):
        kind = KIND_REVIEW
    else:
        logger.debug(
            "evento ignorado: %s %s delivery=%s",
            payload.get("type"),
            payload.get("action"),
            delivery_id,
        )
        return {"status": "ignored"}

    try:
        started = await handle_issue(
            request.app.state.temporal,
            settings.task_queue,
            parse_issue(payload),
            delivery_id,
            kind=kind,
        )
    except Exception:
        # 503 faz o Linear reenviar quando o Temporal voltar.
        logger.exception("falha ao iniciar workflow; delivery=%s", delivery_id)
        raise HTTPException(status_code=503, detail="orquestrador indisponível")
    return {"status": "accepted" if started else "duplicate"}
