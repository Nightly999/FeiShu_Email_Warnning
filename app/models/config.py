from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.settings import get_settings


@dataclass(frozen=True)
class ModelProfile:
    id: str
    base_url: str | None
    api_key: str
    input_types: set[str]
    timeout_seconds: int


def load_model_profiles() -> dict[str, ModelProfile]:
    settings = get_settings()
    path = Path(settings.models_config_path)
    if not path.exists():
        return default_model_profiles()

    payload = json.loads(path.read_text(encoding="utf-8"))
    defaults = payload.get("defaults") or {}
    provider = payload.get("provider") or {}
    base_url = provider.get("baseUrl") or settings.openai_base_url
    api_key = provider.get("apiKey") or settings.openai_api_key
    timeout_seconds = int(defaults.get("timeoutSeconds") or settings.model_request_timeout_seconds)

    profiles: dict[str, ModelProfile] = {}
    for item in payload.get("models") or []:
        model_id = item["id"]
        profiles[model_id] = ModelProfile(
            id=model_id,
            base_url=item.get("baseUrl") or base_url,
            api_key=item.get("apiKey") or api_key,
            input_types=set(item.get("input") or ["text"]),
            timeout_seconds=int(item.get("timeoutSeconds") or timeout_seconds),
        )
    return profiles or default_model_profiles()


def default_model_profiles() -> dict[str, ModelProfile]:
    settings = get_settings()
    text_model = settings.openai_model
    vision_model = settings.vision_model or "qwen3-vl-plus"
    video_model = settings.video_model or "qwen3.7-plus"
    models = {
        text_model: ModelProfile(
            id=text_model,
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            input_types={"text", "image", "video"},
            timeout_seconds=settings.model_request_timeout_seconds,
        )
    }
    if vision_model not in models:
        models[vision_model] = ModelProfile(
            id=vision_model,
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            input_types={"text", "image"},
            timeout_seconds=settings.model_request_timeout_seconds,
        )
    if video_model not in models:
        models[video_model] = ModelProfile(
            id=video_model,
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            input_types={"text", "image", "video"},
            timeout_seconds=settings.model_request_timeout_seconds,
        )
    return models


def load_model_routes() -> dict[str, list[str]]:
    settings = get_settings()
    path = Path(settings.models_config_path)
    if path.exists():
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        routes = payload.get("routes") or {}
        if routes:
            return {key: [str(item) for item in value] for key, value in routes.items()}

    return {
        "text": [settings.openai_model, *split_csv(settings.openai_fallback_models)],
        "vision": [settings.vision_model or "qwen3-vl-plus", settings.openai_model],
        "video": [settings.video_model or "qwen3.7-plus", settings.vision_model or "qwen3-vl-plus"],
    }


def split_csv(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]
