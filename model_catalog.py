from __future__ import annotations

from typing import Any


STEPFUN_STEP_PLAN_BASE_URL = "https://api.stepfun.com/step_plan/v1"

STEPFUN_MODELS: tuple[dict[str, Any], ...] = (
    {
        "id": "step-3.5-flash-2603",
        "capability": "text",
        "label": "Step 3.5 Flash 2603",
        "transport": "chat_completions",
        "product_ready": True,
    },
    {
        "id": "step-3.7-flash",
        "capability": "text",
        "label": "Step 3.7 Flash",
        "transport": "chat_completions",
        "product_ready": True,
    },
    {
        "id": "step-router-v1",
        "capability": "text",
        "label": "Step Router V1",
        "transport": "chat_completions",
        "product_ready": True,
    },
    {
        "id": "step-image-edit-2",
        "capability": "image",
        "label": "Step Image Edit 2",
        "transport": "images",
        "product_ready": True,
    },
    {
        "id": "stepaudio-2.5-asr",
        "capability": "asr",
        "label": "StepAudio 2.5 ASR",
        "transport": "http_sse",
        "product_ready": True,
    },
    {
        "id": "stepaudio-2.5-chat",
        "capability": "audio_chat",
        "label": "StepAudio 2.5 Chat",
        "transport": "chat_completions",
        "product_ready": True,
    },
    {
        "id": "stepaudio-2.5-realtime",
        "capability": "realtime",
        "label": "StepAudio 2.5 Realtime",
        "transport": "websocket",
        "product_ready": False,
    },
    {
        "id": "stepaudio-2.5-tts",
        "capability": "tts",
        "label": "StepAudio 2.5 TTS",
        "transport": "audio_speech",
        "product_ready": True,
    },
)


def stepfun_models(*capabilities: str) -> list[dict[str, Any]]:
    selected = set(capabilities)
    return [dict(model) for model in STEPFUN_MODELS if model["capability"] in selected]


def stepfun_model_ids(*capabilities: str) -> set[str]:
    return {str(model["id"]) for model in stepfun_models(*capabilities)}


def public_model_catalog() -> dict[str, Any]:
    return {
        "provider": "stepfun",
        "base_url": STEPFUN_STEP_PLAN_BASE_URL,
        "models": [dict(model) for model in STEPFUN_MODELS],
    }
