from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage

from app.models.router import invoke_chat_with_fallback


logger = logging.getLogger("feishu_export_intent")
MIN_EXPORT_CONFIDENCE = 0.8


@dataclass(frozen=True)
class ExportIntentDecision:
    intent: str
    confidence: float
    reason: str


def is_explicit_excel_export_request(text: str) -> bool:
    normalized = re.sub(r"\s+", "", (text or "").lower())
    if "excel" not in normalized:
        return False
    export_markers = (
        "导出",
        "生成",
        "整理成",
        "整理为",
        "制作",
        "创建",
        "做成",
        "转成",
        "转为",
        "保存成",
        "保存为",
        "下载",
        "发我",
        "给我",
    )
    return any(marker in normalized for marker in export_markers)


def is_possible_export_request(text: str) -> bool:
    normalized = re.sub(r"\s+", "", (text or "").lower())
    object_markers = ("excel", "电子表格", "表格", "报表", "文件", "附件")
    action_markers = (
        "导出",
        "生成",
        "整理",
        "制作",
        "创建",
        "做成",
        "转成",
        "转为",
        "保存",
        "下载",
        "打包",
        "发我",
        "给我",
        "弄个",
        "来一份",
    )
    return any(marker in normalized for marker in object_markers) and any(
        marker in normalized for marker in action_markers
    )


async def should_export_excel(text: str) -> bool:
    if is_explicit_excel_export_request(text):
        return True
    if not is_possible_export_request(text):
        return False

    messages = [
        SystemMessage(content=EXPORT_INTENT_SYSTEM_PROMPT),
        HumanMessage(content=json.dumps({"user_text": text}, ensure_ascii=False)),
    ]
    try:
        response = await invoke_chat_with_fallback(
            messages=messages,
            route="text",
            temperature=0,
        )
        decision = parse_export_intent_response(str(response.content or ""))
    except Exception:
        logger.exception("Excel export intent classification failed")
        return False
    return (
        decision.intent == "export_excel"
        and decision.confidence >= MIN_EXPORT_CONFIDENCE
    )


def parse_export_intent_response(content: str) -> ExportIntentDecision:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        logger.warning("Invalid Excel export intent response: %s", content[:500])
        return ExportIntentDecision("normal", 0.0, "invalid response")

    intent = str(payload.get("intent") or "normal")
    if intent not in {"export_excel", "normal"}:
        intent = "normal"
    try:
        confidence = min(1.0, max(0.0, float(payload.get("confidence") or 0)))
    except (TypeError, ValueError):
        confidence = 0.0
    return ExportIntentDecision(
        intent=intent,
        confidence=confidence,
        reason=str(payload.get("reason") or ""),
    )


EXPORT_INTENT_SYSTEM_PROMPT = """
你是企业飞书助手的 Excel 导出意图分类器，只分类，不回答用户问题。

可选 intent：
- export_excel：用户希望把当前会话中已经查询、展示或汇总的数据制作成可下载的 Excel 文件。
- normal：用户是在分析/读取上传文件、查询新的业务数据、询问功能，或没有明确要求交付 Excel 文件。

判断原则：
1. “把上面的数据弄个电子表格”“刚才结果给我一份文件”属于 export_excel。
2. “分析这个 Excel”“查一下本月风险”“生成风险分析报告”属于 normal。
3. 只有明确要求把已有结果交付为表格文件时才判为 export_excel。
4. 不确定时判为 normal，不能猜。

只输出 JSON，不要 Markdown，不要解释正文：
{"intent":"export_excel|normal","confidence":0.0到1.0,"reason":"一句简短原因"}
"""
