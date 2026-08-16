from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from app.models.config import ModelProfile, load_model_profiles, load_model_routes


logger = logging.getLogger("feishu_models")
ModelRoute = Literal["text", "vision", "video"]


class ModelFallbackError(RuntimeError):
    pass


def select_model_chain(route: ModelRoute) -> list[ModelProfile]:
    profiles = load_model_profiles()
    routes = load_model_routes()
    chain: list[ModelProfile] = []
    for model_id in routes.get(route, []):
        profile = profiles.get(model_id)
        if profile and profile not in chain:
            chain.append(profile)

    required = {"vision": "image", "video": "video"}.get(route, "text")
    chain = [profile for profile in chain if required in profile.input_types or route == "text"]
    if not chain:
        chain = [profile for profile in profiles.values() if required in profile.input_types]
    if not chain:
        chain = list(profiles.values())
    return chain


async def invoke_chat_with_fallback(
    *,
    messages: list[Any],
    route: ModelRoute = "text",
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0,
) -> Any:
    errors: list[str] = []
    for profile in select_model_chain(route):
        logger.info("LLM invoke: route=%s model=%s base_url=%s", route, profile.id, profile.base_url or "default")
        try:
            llm = build_chat_model(profile, temperature=temperature)
            runnable = llm.bind_tools(tools) if tools else llm
            return await runnable.ainvoke(messages)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM failed: route=%s model=%s error=%s", route, profile.id, exc)
            errors.append(f"{profile.id}: {exc}")
    raise ModelFallbackError("; ".join(errors))


async def analyze_image_with_vision_model(path: Path, prompt: str | None = None) -> str:
    image_url = encode_image_as_data_url(path)
    message = HumanMessage(
        content=[
            {
                "type": "text",
                "text": prompt
                or "请用中文简洁分析这张图片。说明图片内容、关键文字、可能的业务含义；如果看不清，请直接说明。",
            },
            {"type": "image_url", "image_url": {"url": image_url}},
        ]
    )
    response = await invoke_chat_with_fallback(messages=[message], route="vision", temperature=0)
    return str(response.content or "").strip() or "图片已处理，但模型没有返回可展示内容。"


async def analyze_video_with_video_model(path: Path, prompt: str | None = None) -> str:
    video_url = encode_file_as_data_url(path, default_mime="video/mp4")
    message = HumanMessage(
        content=[
            {
                "type": "text",
                "text": prompt
                or "请用中文分析这个视频。总结视频内容、关键动作、可见文字、异常点和可能的业务含义；如果无法识别，请说明原因。",
            },
            {
                "type": "video_url",
                "video_url": {
                    "url": video_url,
                    "fps": 1.0,
                },
            },
        ]
    )
    response = await invoke_chat_with_fallback(messages=[message], route="video", temperature=0)
    return str(response.content or "").strip() or "视频已处理，但模型没有返回可展示内容。"


def build_chat_model(profile: ModelProfile, *, temperature: float) -> ChatOpenAI:
    return ChatOpenAI(
        model=profile.id,
        api_key=profile.api_key,
        base_url=profile.base_url or None,
        temperature=temperature,
        timeout=profile.timeout_seconds,
    )


def encode_image_as_data_url(path: Path) -> str:
    return encode_file_as_data_url(path, default_mime="image/png")


def encode_file_as_data_url(path: Path, *, default_mime: str) -> str:
    mime = mimetypes.guess_type(path.name)[0] or default_mime
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"
