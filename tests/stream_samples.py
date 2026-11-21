"""Eventos reais do `claude -p --output-format stream-json --verbose` (Claude Code com assinatura,
capturados em 2026-11-18), encurtados. Base dos testes da contagem de tokens."""

INIT_EVENT = {"type": "system", "subtype": "init", "model": "claude-opus-5-5"}

# Um evento `assistant` por bloco de conteúdo: o mesmo `message.id` aparece repetido. O
# `output_tokens` dele é um retrato do streaming, menor que o total da mensagem.
ASSISTANT_EVENT = {
    "type": "assistant",
    "message": {
        "id": "msg_011ChBVCgRW8h2r3ANTJ5jN2",
        "usage": {
            "input_tokens": 2,
            "cache_creation_input_tokens": 3143,
            "cache_read_input_tokens": 37118,
            "output_tokens": 4,
        },
        "content": [{"type": "text", "text": "..."}],
    },
}

# Emitido depois de cada resposta da API; `utilization` vem com granularidade de 0.01.
RATE_LIMIT_EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed",
        "resetsAt": 1795065000,
        "rateLimitType": "five_hour",
        "unifiedWindows": {
            "five_hour": {"utilization": 0.61, "resetsAt": 1795065000, "status": "allowed"},
            "seven_day": {"utilization": 0.1, "resetsAt": 1795568400, "status": "allowed"},
        },
    },
}

# `usage` soma a sessão inteira (todas as mensagens).
RESULT_EVENT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "Há 3 arquivos.",
    "num_turns": 2,
    "total_cost_usd": 0.3330276,
    "usage": {
        "input_tokens": 4,
        "cache_creation_input_tokens": 40261,
        "cache_read_input_tokens": 37118,
        "output_tokens": 175,
    },
}
