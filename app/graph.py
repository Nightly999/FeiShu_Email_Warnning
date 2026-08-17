import logging
import json
import uuid
from functools import lru_cache
from typing import Any, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph

from app.answers import AnswerResult, AnswerStatus
from app.agent_skills import load_agent_skills_prompt
from app.audit import write_audit
from app.business_pagination import register_business_result, render_business_page
from app.export_context import save_export_context
from app.identity import Identity, load_business_permissions, resolve_identity
from app.memory.service import build_memory_prompt, record_agent_exchange
from app.memory.sessions import get_active_session_id
from app.models.router import ModelFallbackError, invoke_chat_with_fallback
from app.mcp_client import McpClient
from app.mcp_tools import (
    IDENTITY_ARGUMENTS,
    apply_business_pagination_defaults,
    get_mcp_openai_tools,
)
from app.policy import check_agent_access, check_tool_access, inject_identity_args
from app.settings import get_settings
from app.tool_result_cache import save_tool_result
from app.tool_config import get_automation_tool_names


logger = logging.getLogger("feishu_agent")


class AgentState(TypedDict, total=False):
    request_id: str
    tenant_key: str
    app_id: str
    bot_code: str | None
    open_id: str
    union_id: str | None
    user_id: str | None
    message_id: str | None
    chat_id: str | None
    chat_type: str | None
    user_message: str
    session_id: str
    identity: Identity
    messages: list[Any]
    tool_result_statuses: list[dict[str, Any]]
    final_answer: str
    answer_status: AnswerStatus
    automation_run: bool
    latest_tool_result_id: int
    latest_tool_row_count: int


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("resolve_identity", resolve_identity_node)
    graph.add_node("guard", guard_node)
    graph.add_node("llm", llm_node)
    graph.add_node("tools", tools_node)
    graph.add_node("final", final_node)

    graph.set_entry_point("resolve_identity")
    graph.add_edge("resolve_identity", "guard")
    graph.add_conditional_edges("guard", guard_router, {"allowed": "llm", "blocked": "final"})
    graph.add_conditional_edges("llm", llm_router, {"tools": "tools", "final": "final"})
    graph.add_edge("tools", "llm")
    graph.add_edge("final", END)
    return graph.compile()


@lru_cache(maxsize=1)
def get_compiled_graph():
    return build_graph()


async def run_agent(event: dict[str, Any]) -> AnswerResult:
    compiled = get_compiled_graph()
    session_id = event.get("_session_id")
    if not session_id:
        session_id = await get_active_session_id(
            tenant_key=event["tenant_key"],
            app_id=event["app_id"],
            open_id=event["open_id"],
            chat_id=event.get("chat_id"),
            bot_code=event.get("bot_code"),
        )
    initial: AgentState = {
        "request_id": str(uuid.uuid4()),
        "tenant_key": event["tenant_key"],
        "app_id": event["app_id"],
        "bot_code": event.get("bot_code"),
        "open_id": event["open_id"],
        "union_id": event.get("union_id"),
        "user_id": event.get("user_id"),
        "message_id": event.get("message_id"),
        "chat_id": event.get("chat_id"),
        "chat_type": event.get("chat_type"),
        "user_message": event.get("text") or "",
        "session_id": session_id,
        "automation_run": bool(event.get("_automation_run")),
    }
    try:
        result = await compiled.ainvoke(
            initial,
            config={"recursion_limit": get_settings().agent_recursion_limit},
        )
        answer = AnswerResult(
            content=result.get("final_answer") or "已处理。",
            status=result.get("answer_status") or "success",
        )
        if (
            not event.get("_automation_run")
            and int(result.get("latest_tool_row_count") or 0)
            > get_settings().business_list_page_size
        ):
            page = await render_business_page(
                tenant_key=event["tenant_key"],
                app_id=event["app_id"],
                open_id=event["open_id"],
                chat_id=event.get("chat_id"),
                session_id=session_id,
                page=1,
                expected_result_id=result.get("latest_tool_result_id"),
            )
            if page:
                answer = AnswerResult(
                    content=f"{answer.content}\n\n{page}",
                    status=answer.status,
                )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Agent execution failed: request_id=%s bot_code=%s message_id=%s",
            initial["request_id"],
            initial.get("bot_code"),
            initial.get("message_id"),
        )
        answer = AnswerResult(
            content="业务查询暂时不可用，请稍后重试；如果问题持续，请联系信息管理中心。",
            status="error",
        )
    if not event.get("_automation_run"):
        await record_agent_exchange(event, answer.content, session_id=session_id)
    return answer


async def resolve_identity_node(state: AgentState) -> AgentState:
    identity = await resolve_identity(
        tenant_key=state["tenant_key"],
        app_id=state["app_id"],
        open_id=state["open_id"],
        union_id=state.get("union_id"),
        user_id=state.get("user_id"),
    )
    identity = await load_business_permissions(identity)
    logger.info(
        "Identity resolved: request_id=%s bot_code=%s message_id=%s tenant_key=%s app_id=%s open_id=%s username=%s known=%s",
        state["request_id"],
        state.get("bot_code"),
        state.get("message_id"),
        identity.tenant_key,
        identity.app_id,
        identity.open_id,
        identity.internal_username,
        identity.is_known,
    )
    memory_prompt = await build_memory_prompt(
        tenant_key=state["tenant_key"],
        app_id=state["app_id"],
        open_id=state["open_id"],
        chat_id=state.get("chat_id"),
        chat_type=state.get("chat_type"),
        bot_code=state.get("bot_code"),
        session_id=state["session_id"],
    )
    return {
        **state,
        "identity": identity,
        "messages": [
            SystemMessage(content=build_system_prompt(memory_prompt)),
            HumanMessage(content=state["user_message"]),
        ],
    }


async def guard_node(state: AgentState) -> AgentState:
    identity = state["identity"]
    policy = check_agent_access(identity)
    if policy.allowed:
        await write_audit(
            request_id=state["request_id"],
            tenant_key=identity.tenant_key,
            app_id=identity.app_id,
            open_id=identity.open_id,
            internal_username=identity.internal_username,
            bot_code=state.get("bot_code"),
            message_id=state.get("message_id"),
            chat_id=state.get("chat_id"),
            user_message=state["user_message"],
            permission_result="allowed",
        )
        return state

    await write_audit(
        request_id=state["request_id"],
        tenant_key=identity.tenant_key,
        app_id=identity.app_id,
        open_id=identity.open_id,
        internal_username=identity.internal_username,
        bot_code=state.get("bot_code"),
        message_id=state.get("message_id"),
        chat_id=state.get("chat_id"),
        user_message=state["user_message"],
        permission_result="blocked",
        tool_result_summary=policy.reason,
    )
    return {**state, "final_answer": policy.reason, "answer_status": "denied"}


def guard_router(state: AgentState) -> str:
    return "blocked" if state.get("final_answer") else "allowed"


def sanitize_tool_args(arguments: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in (arguments or {}).items() if key not in IDENTITY_ARGUMENTS}


async def llm_node(state: AgentState) -> AgentState:
    answer_status = state.get("answer_status") or "success"
    try:
        mcp_tools = await get_mcp_openai_tools()
        response = await invoke_chat_with_fallback(
            messages=state["messages"],
            route="text",
            tools=mcp_tools,
            temperature=0,
        )
    except ModelFallbackError:
        logger.exception("LLM request failed")
        response = AIMessage(content="模型服务暂时不可用，请稍后再试或联系信息管理中心检查模型配置。")
        answer_status = "error"
    return {
        **state,
        "messages": [*state["messages"], response],
        "answer_status": answer_status,
    }


def llm_router(state: AgentState) -> str:
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else "final"


async def tools_node(state: AgentState) -> AgentState:
    identity = state["identity"]
    last = state["messages"][-1]
    tool_messages: list[ToolMessage] = []
    answer_status = state.get("answer_status") or "success"

    for call in last.tool_calls:
        tool_name = call.get("name")
        tool_args = call.get("args") or {}

        if not tool_name:
            content = "缺少 tool_name"
            answer_status = "error"
        else:
            paged_args = await apply_business_pagination_defaults(tool_name, tool_args)
            protected_args = inject_identity_args(identity, paged_args)
            policy = check_tool_access(identity, tool_name, protected_args)
            if state.get("automation_run") and tool_name not in get_automation_tool_names():
                policy.allowed = False
                policy.reason = f"工具 {tool_name} 未授权用于无人值守定时任务"
            if not policy.allowed:
                content = policy.reason
                answer_status = "denied"
                await write_audit(
                    request_id=state["request_id"],
                    tenant_key=identity.tenant_key,
                    app_id=identity.app_id,
                    open_id=identity.open_id,
                    internal_username=identity.internal_username,
                    bot_code=state.get("bot_code"),
                    message_id=state.get("message_id"),
                    chat_id=state.get("chat_id"),
                    tool_name=tool_name,
                    permission_result="blocked",
                    tool_args=sanitize_tool_args(protected_args),
                    tool_result_summary=content,
                )
            else:
                content = await McpClient().call_tool(tool_name, protected_args)
                status = summarize_tool_result(tool_name, content)
                if status.get("denied"):
                    answer_status = "denied"
                content_for_model = protect_tool_result_for_model(tool_name, content, status)
                cache_id = await save_tool_result(
                    request_id=state["request_id"],
                    tenant_key=identity.tenant_key,
                    app_id=identity.app_id,
                    open_id=identity.open_id,
                    bot_code=state.get("bot_code"),
                    message_id=state.get("message_id"),
                    chat_id=state.get("chat_id"),
                    tool_name=tool_name,
                    tool_args=sanitize_tool_args(protected_args),
                    tool_result=content,
                )
                if not state.get("automation_run"):
                    await save_export_context(
                        tenant_key=identity.tenant_key,
                        app_id=identity.app_id,
                        open_id=identity.open_id,
                        chat_id=state.get("chat_id"),
                        session_id=state["session_id"],
                        source_type="tool_result",
                        source_ref=str(cache_id),
                        source_name=tool_name,
                    )
                    row_count = await register_business_result(
                        tenant_key=identity.tenant_key,
                        app_id=identity.app_id,
                        open_id=identity.open_id,
                        chat_id=state.get("chat_id"),
                        session_id=state["session_id"],
                        result_id=cache_id,
                        tool_name=tool_name,
                        tool_result=content,
                    )
                    state["latest_tool_result_id"] = cache_id
                    state["latest_tool_row_count"] = row_count
                await write_audit(
                    request_id=state["request_id"],
                    tenant_key=identity.tenant_key,
                    app_id=identity.app_id,
                    open_id=identity.open_id,
                    internal_username=identity.internal_username,
                    bot_code=state.get("bot_code"),
                    message_id=state.get("message_id"),
                    chat_id=state.get("chat_id"),
                    tool_name=tool_name,
                    permission_result="allowed",
                    tool_args=sanitize_tool_args(protected_args),
                    tool_result_summary=content[:1000],
                )

        if "status" not in locals():
            status = {"tool_name": tool_name, "has_rows": False, "row_count": 0}
            content_for_model = content
        tool_messages.append(ToolMessage(content=content_for_model, tool_call_id=call["id"]))
        state.setdefault("tool_result_statuses", []).append(status)
        if "status" in locals():
            del status
        if "content_for_model" in locals():
            del content_for_model

    return {
        **state,
        "messages": [*state["messages"], *tool_messages],
        "answer_status": answer_status,
    }


async def final_node(state: AgentState) -> AgentState:
    if state.get("final_answer"):
        return state
    last = state["messages"][-1]
    if isinstance(last, AIMessage):
        answer = str(last.content)
        protected_answer = guard_final_answer_against_tool_results(answer, state.get("tool_result_statuses") or [])
        return {**state, "final_answer": protected_answer}
    return {**state, "final_answer": str(last.content)}


def summarize_tool_result(tool_name: str | None, content: str) -> dict[str, Any]:
    status: dict[str, Any] = {"tool_name": tool_name, "has_rows": False, "row_count": 0}
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return status

    rows = payload.get("rows") if isinstance(payload, dict) else None
    authorization = payload.get("authorization") if isinstance(payload, dict) else None
    if isinstance(authorization, dict) and authorization.get("authorized") is False:
        status["denied"] = True
        status["final_answer"] = payload.get("finalAnswer") or authorization.get(
            "message"
        )
    if isinstance(rows, list):
        status["row_count"] = len(rows)
        status["has_rows"] = len(rows) > 0
    elif isinstance(payload, dict):
        row_count = payload.get("rowCount") or payload.get("count") or payload.get("total")
        if isinstance(row_count, int):
            status["row_count"] = row_count
            status["has_rows"] = row_count > 0
    return status


def protect_tool_result_for_model(tool_name: str | None, content: str, status: dict[str, Any]) -> str:
    if not status.get("has_rows"):
        return content
    prefix = (
        f"工具 {tool_name} 已成功返回 {status.get('row_count')} 条数据。"
        "必须基于 rows 字段整理结果；不得回答未查询到、无数据或不存在。"
    )
    return f"{prefix}\n\n{content}"


def guard_final_answer_against_tool_results(answer: str, statuses: list[dict[str, Any]]) -> str:
    has_rows = any(status.get("has_rows") for status in statuses)
    if not has_rows:
        return answer
    no_data_markers = ("未查询到", "没有查询到", "无相关", "不存在", "无数据")
    if not any(marker in answer for marker in no_data_markers):
        return answer
    return (
        "已从业务系统查询到相关数据，但模型整理结果时发生误判。"
        "请重新发送一次问题，我会基于已返回的数据重新整理。"
    )


SYSTEM_PROMPT = """
你是企业内部飞书智能体。你可以调用 MCP 业务工具查询库存、订单、OA 等数据。

规则：
1. 只能调用系统提供的 MCP 工具访问业务数据，不要编造工具名。
2. 不要向用户索要 open_id、tenant_key、app_id，这些由系统注入。
3. 如果工具返回 finalAnswer，优先直接回复 finalAnswer。
4. 没有权限时只说明无权限，不要猜测数据。
5. 回复要简洁、中文、面向业务用户。
6. 当用户问“你会什么/有什么功能”时，根据当前系统提供给你的 MCP 工具说明回答，不要提及未提供的工具能力。
7. 查询结果用于飞书卡片展示，基础属性优先用键值列表，多行详情、对比、排名和流程类数据优先用 Markdown 表格。
8. 不要输出大段原始 JSON 或数据库字段堆叠；先筛选业务用户最关心的字段，再补充必要明细。
9. 如果用户要汇总、趋势、排名或对比，且工具结果里有数值，最多展示前 8 项。
"""


def build_system_prompt(memory_prompt: str = "") -> str:
    return SYSTEM_PROMPT + memory_prompt + load_agent_skills_prompt()
