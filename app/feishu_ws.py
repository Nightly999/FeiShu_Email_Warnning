import asyncio
import base64
import http
import json
import logging
import multiprocessing
import os
import signal
import threading
import time
import warnings
from contextlib import contextmanager
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator
from collections.abc import Coroutine
import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    P2ImChatAccessEventBotP2pChatEnteredV1,
    P2ImMessageReceiveV1,
)
from lark_oapi.core.const import UTF_8
from lark_oapi.core.json import JSON
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from lark_oapi.ws.client import _get_by_key
from lark_oapi.ws.const import (
    HEADER_BIZ_RT,
    HEADER_MESSAGE_ID,
    HEADER_SEQ,
    HEADER_SUM,
    HEADER_TYPE,
)
from lark_oapi.ws.enum import MessageType
from lark_oapi.ws.model import Response

from app.bootstrap import bootstrap
from app.builtin_commands import handle_builtin_text_command
from app.event_dedup import claim_event, finish_event
from app.email_pop3 import EmailAuthenticationError, EmailConnectionError
from app.email_service import (
    bind_email_account,
    cancel_email_analysis,
    consume_bind_token,
    get_bind_scope,
    initial_sync,
    is_stop_email_analysis_command,
    render_email_report_page,
)
from app.feishu import (
    TenantApp,
    download_message_file,
    event_scope_matches,
    list_enabled_tenant_apps,
    normalize_event,
    reply_card,
    reply_message,
    send_card,
    update_card,
)
from app.feishu_cards import build_answer_card, build_processing_card, build_welcome_card, should_use_card
from app.files.service import register_uploaded_file
from app.file_analysis import safe_resource_path
from app.graph import run_agent
from app.logging_security import install_sensitive_log_filter
from app.memory.sessions import get_active_session_id
from app.reply_context import hydrate_reply_context
from app.scheduler import start_scheduler_thread
from app.settings import get_settings
from app.welcome import send_daily_welcome_once


logger = logging.getLogger("feishu_ws")
PID_FILE = Path("data/feishu_ws_pids.json")
ConversationKey = tuple[str, str, str]


class ConversationSequencer:
    """Preserve receive order within one conversation while allowing cross-chat concurrency."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._issued: dict[ConversationKey, int] = {}
        self._serving: dict[ConversationKey, int] = {}

    def issue(self, key: ConversationKey) -> int:
        with self._condition:
            ticket = self._issued.get(key, 0)
            self._issued[key] = ticket + 1
            self._serving.setdefault(key, 0)
            return ticket

    @contextmanager
    def turn(self, key: ConversationKey, ticket: int) -> Iterator[None]:
        with self._condition:
            self._condition.wait_for(lambda: self._serving.get(key, 0) == ticket)
        try:
            yield
        finally:
            with self._condition:
                next_ticket = ticket + 1
                self._serving[key] = next_ticket
                if next_ticket >= self._issued.get(key, 0):
                    self._serving.pop(key, None)
                    self._issued.pop(key, None)
                self._condition.notify_all()


class CardCallbackWsClient(lark.ws.Client):
    """Dispatch CARD frames until lark-oapi fixes its WebSocket client."""

    # ponytail: remove this override when upstream dispatches MessageType.CARD.
    async def _handle_data_frame(self, frame) -> None:
        message_type = MessageType(_get_by_key(frame.headers, HEADER_TYPE))
        if message_type != MessageType.CARD:
            await super()._handle_data_frame(frame)
            return

        payload = frame.payload
        total = int(_get_by_key(frame.headers, HEADER_SUM))
        if total > 1:
            message_id = next(
                header.value
                for header in frame.headers
                if header.key == HEADER_MESSAGE_ID
            )
            payload = self._combine(
                message_id,
                total,
                int(_get_by_key(frame.headers, HEADER_SEQ)),
                payload,
            )
            if payload is None:
                return

        response = Response(code=http.HTTPStatus.OK)
        started_at = int(round(time.time() * 1000))
        try:
            result = self._event_handler._do_without_validation(payload)
            if result is not None:
                response.data = base64.b64encode(JSON.marshal(result).encode(UTF_8))
        except Exception:
            logger.exception("Failed to handle Feishu card callback")
            response = Response(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)
        header = frame.headers.add()
        header.key = HEADER_BIZ_RT
        header.value = str(int(round(time.time() * 1000)) - started_at)
        frame.payload = JSON.marshal(response).encode(UTF_8)
        await self._write_message(frame.SerializeToString())


def main() -> None:
    multiprocessing.freeze_support()
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    install_sensitive_log_filter()
    warn_stale_processes()
    apps = asyncio.run(load_apps())
    if not apps:
        raise RuntimeError("No enabled Feishu apps found. Check config/feishu_apps.local.json")

    processes: list[multiprocessing.Process] = []
    for app in apps:
        process = multiprocessing.Process(
            target=run_client_process,
            args=(asdict(app),),
            name=f"feishu-ws-{app.bot_code}",
            daemon=False,
        )
        process.start()
        processes.append(process)

    write_pid_file(processes)
    start_scheduler_thread(apps)
    logger.info("Started %s Feishu websocket processes. Press Ctrl+C to stop.", len(processes))
    logger.info("Listening bot_code: %s", ", ".join(app.bot_code for app in apps))

    try:
        while True:
            for process in processes:
                if process.exitcode is not None:
                    logger.error("Websocket process exited: name=%s exitcode=%s", process.name, process.exitcode)
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Stopping Feishu websocket processes...")
    finally:
        stop_processes(processes)
        clear_pid_file()


def stop_processes(processes: list[multiprocessing.Process]) -> None:
    for process in processes:
        if process.is_alive():
            logger.info("Terminating websocket process: name=%s pid=%s", process.name, process.pid)
            process.terminate()

    deadline = time.time() + 10
    for process in processes:
        timeout = max(0.1, deadline - time.time())
        process.join(timeout=timeout)

    for process in processes:
        if process.is_alive():
            logger.warning("Killing unresponsive websocket process: name=%s pid=%s", process.name, process.pid)
            try:
                process.kill()
            except AttributeError:
                if process.pid:
                    os.kill(process.pid, signal.SIGTERM)
            process.join(timeout=5)


def write_pid_file(processes: list[multiprocessing.Process]) -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "parent_pid": os.getpid(),
        "children": [
            {"pid": process.pid, "name": process.name}
            for process in processes
            if process.pid
        ],
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    PID_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_pid_file() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to remove websocket pid file: %s", PID_FILE)


def warn_stale_processes() -> None:
    if not PID_FILE.exists():
        return

    try:
        payload = json.loads(PID_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Found unreadable websocket pid file: %s", PID_FILE)
        return

    pids = [
        int(item["pid"])
        for item in payload.get("children", [])
        if item.get("pid") and is_process_alive(int(item["pid"]))
    ]
    parent_pid = payload.get("parent_pid")
    if parent_pid and is_process_alive(int(parent_pid)):
        pids.insert(0, int(parent_pid))

    if not pids:
        clear_pid_file()
        return

    joined_pids = ",".join(str(pid) for pid in pids)
    raise RuntimeError(
        "Feishu websocket is already running with PID(s): "
        f"{', '.join(str(pid) for pid in pids)}. "
        f"Stop it first with: Stop-Process -Id {joined_pids} -Force"
    )


def is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return is_windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except (OSError, SystemError, ValueError):
        return False
    return True


def is_windows_process_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == error_access_denied
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)

def run_callback_coro(coro: Coroutine[Any, Any, Any], *, label: str, bot_code: str) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro)
        return

    task = loop.create_task(coro)

    def log_failure(done: asyncio.Task[Any]) -> None:
        try:
            done.result()
        except Exception:
            logger.exception("Async callback failed: label=%s bot_code=%s", label, bot_code)

    task.add_done_callback(log_failure)

async def load_apps() -> list[TenantApp]:
    await bootstrap()
    return [
        app
        for app in await list_enabled_tenant_apps()
        if app.app_id != "cli_sample_app_id"
    ]


def run_client_process(app_payload: dict[str, Any]) -> None:
    app = TenantApp(**app_payload)
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    install_sensitive_log_filter()
    settings = get_settings()
    worker_count = max(1, settings.feishu_event_workers)
    event_slots = threading.BoundedSemaphore(
        worker_count + max(0, settings.feishu_event_queue_size)
    )
    event_executor = ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix=f"feishu-event-{app.bot_code}"
    )
    conversation_sequencer = ConversationSequencer()

    def release_event_slot(_future: Future[None]) -> None:
        event_slots.release()

    def handle_message(data: P2ImMessageReceiveV1) -> None:
        try:
            payload = json.loads(lark.JSON.marshal(data))
            event = normalize_event(payload, app, trust_event_tenant=True)
            if is_stop_email_analysis_command(str(event.get("text") or "")):
                cancel_email_analysis(event, str(event.get("message_id") or ""))
            if not event_slots.acquire(blocking=False):
                logger.error("Event queue is full: bot_code=%s", app.bot_code)
                return
            try:
                key = conversation_key(payload, app)
                ticket = conversation_sequencer.issue(key)
                future = event_executor.submit(
                    process_event_thread,
                    app_payload,
                    payload,
                    conversation_sequencer,
                    key,
                    ticket,
                )
                future.add_done_callback(release_event_slot)
            except Exception:
                event_slots.release()
                raise
        except Exception:
            logger.exception("Failed to start event worker thread: bot_code=%s", app.bot_code)

    def handle_bot_p2p_entered(data: P2ImChatAccessEventBotP2pChatEnteredV1) -> None:
        try:
            payload = json.loads(lark.JSON.marshal(data))
            if not event_scope_matches(payload, app, allow_unconfigured_tenant=True):
                logger.warning("Ignore entered event with mismatched tenant scope: bot_code=%s", app.bot_code)
                return
            event = normalize_event(payload, app, trust_event_tenant=True)
            logger.info(
                "User opened bot chat: bot_code=%s open_id=%s chat_id=%s",
                app.bot_code,
                event.get("open_id"),
                event.get("chat_id"),
            )
            run_callback_coro(
                send_daily_welcome_once(app, event),
                label="daily_welcome",
                bot_code=app.bot_code,
            )
        except Exception:
            logger.exception("Failed to handle bot chat entered event: bot_code=%s", app.bot_code)

    def handle_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        try:
            payload = json.loads(lark.JSON.marshal(data))
            page_action = extract_email_page_card_action(payload, app)
            bind_action = extract_email_bind_card_action(payload, app)
            if not page_action and not bind_action:
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "warning", "content": "无法识别该卡片操作。"}}
                )
            if not event_slots.acquire(blocking=False):
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "error", "content": "当前请求较多，请稍后重试。"}}
                )
            try:
                future = event_executor.submit(
                    process_email_page_card_thread if page_action else process_email_bind_card_thread,
                    app_payload,
                    page_action or bind_action,
                )
                future.add_done_callback(release_event_slot)
            except Exception:
                event_slots.release()
                raise
            return P2CardActionTriggerResponse(
                {
                    "toast": {
                        "type": "info",
                        "content": "正在切换页面…" if page_action else "正在验证邮箱账号，请稍候…",
                    }
                }
            )
        except Exception:
            logger.exception("Failed to enqueue card action: bot_code=%s", app.bot_code)
            return P2CardActionTriggerResponse(
                {"toast": {"type": "error", "content": "卡片操作失败，请重试。"}}
            )

    event_handler = (
        lark.EventDispatcherHandler.builder(
            app.encrypt_key or "",
            app.verification_token or "",
        )
        .register_p2_im_message_receive_v1(handle_message)
        .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(handle_bot_p2p_entered)
        .register_p2_card_action_trigger(handle_card_action)
        .build()
    )
    client = CardCallbackWsClient(
        app_id=app.app_id,
        app_secret=app.app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.WARNING,
        auto_reconnect=True,
    )
    logger.info("Starting Feishu websocket: bot_code=%s app_id=%s", app.bot_code, app.app_id)
    client.start()


def extract_email_bind_card_action(
    payload: dict[str, Any], app: TenantApp
) -> dict[str, str] | None:
    event = payload.get("event") or {}
    operator = event.get("operator") or {}
    action = event.get("action") or {}
    value = action.get("value") or {}
    form = action.get("form_value") or {}
    context = event.get("context") or {}
    if value.get("action") != "email_bind" or action.get("tag") != "button":
        return None
    tenant_key = str(operator.get("tenant_key") or app.tenant_key or "")
    if app.tenant_key and tenant_key != app.tenant_key:
        return None
    result = {
        "tenant_key": tenant_key,
        "app_id": app.app_id,
        "bot_code": app.bot_code,
        "open_id": str(operator.get("open_id") or ""),
        "chat_id": str(context.get("open_chat_id") or ""),
        "message_id": str(context.get("open_message_id") or ""),
        "token": str(value.get("token") or ""),
        "email": str(form.get("email_account") or ""),
        "password": str(form.get("email_password") or ""),
    }
    if not all(result.values()):
        return None
    return result


def extract_email_page_card_action(
    payload: dict[str, Any], app: TenantApp
) -> dict[str, Any] | None:
    event = payload.get("event") or {}
    operator = event.get("operator") or {}
    action = event.get("action") or {}
    value = action.get("value") or {}
    context = event.get("context") or {}
    if value.get("action") != "email_page" or action.get("tag") != "button":
        return None
    tenant_key = str(operator.get("tenant_key") or app.tenant_key or "")
    if app.tenant_key and tenant_key != app.tenant_key:
        return None
    try:
        page = int(value.get("page") or 0)
    except (TypeError, ValueError):
        return None
    run_ref = str(value.get("run_ref") or "")
    result = {
        "tenant_key": tenant_key,
        "app_id": app.app_id,
        "open_id": str(operator.get("open_id") or ""),
        "chat_id": str(context.get("open_chat_id") or ""),
        "message_id": str(context.get("open_message_id") or ""),
        "run_ref": run_ref,
        "page": page,
    }
    if not all(result[key] for key in ("tenant_key", "app_id", "open_id", "chat_id", "message_id")):
        return None
    if not run_ref or len(run_ref) > 100 or not 1 <= page <= 100:
        return None
    return result


def process_email_page_card_thread(
    app_payload: dict[str, Any], action: dict[str, Any]
) -> None:
    try:
        asyncio.run(process_email_page_card(TenantApp(**app_payload), action))
    except Exception:
        logger.exception("Failed to process email page card")


async def process_email_page_card(
    app: TenantApp, action: dict[str, Any]
) -> None:
    card = await render_email_report_page(
        action["tenant_key"],
        action["app_id"],
        action["open_id"],
        action["run_ref"],
        action["page"],
    )
    if not card:
        await send_card(
            app,
            action["chat_id"],
            build_answer_card("邮件分析", "分页结果不存在或已过期。", status="error"),
        )
        return
    if not await update_card(app, action["message_id"], card):
        await send_card(app, action["chat_id"], card)


def process_email_bind_card_thread(
    app_payload: dict[str, Any], action: dict[str, str]
) -> None:
    try:
        asyncio.run(process_email_bind_card(TenantApp(**app_payload), action))
    except Exception:
        logger.exception("Failed to process email bind card")


async def process_email_bind_card(app: TenantApp, action: dict[str, str]) -> None:
    scope = await get_bind_scope(action["token"])
    if not scope or any(
        str(scope[key] or "") != action[key]
        for key in ("tenant_key", "app_id", "open_id", "chat_id")
    ):
        await send_card(
            app,
            action["chat_id"],
            build_answer_card("邮箱绑定", "绑定卡片已失效，请重新发起绑定。", status="error"),
        )
        return

    event = {
        "tenant_key": scope["tenant_key"],
        "app_id": scope["app_id"],
        "bot_code": scope.get("bot_code") or app.bot_code,
        "open_id": scope["open_id"],
        "chat_id": scope["chat_id"],
    }
    try:
        account = await bind_email_account(event, action["email"], action["password"])
    except EmailAuthenticationError:
        message = "邮箱账号或密码错误，请检查后重试。"
    except EmailConnectionError:
        message = "暂时无法连接邮箱服务器，请稍后重试。"
    except ValueError as exc:
        message = str(exc)
    except Exception:
        logger.exception("Email account binding failed")
        message = "邮箱绑定暂时无法保存，请联系信息管理中心。"
    else:
        await consume_bind_token(action["token"])
        try:
            count = await initial_sync(account)
        except Exception:
            logger.exception("Initial email synchronization failed")
            message = "邮箱绑定成功，但首次同步失败；请稍后发送“分析我的邮件”重试。"
        else:
            message = f"邮箱绑定成功，首次同步新增 {count} 封邮件。"
        card = build_answer_card("邮箱绑定", message, title="登录成功")
        if not await update_card(app, action["message_id"], card):
            await send_card(app, scope["chat_id"], card)
        await send_card(app, scope["chat_id"], build_welcome_card(logged_in=True))
        return

    await send_card(
        app,
        scope["chat_id"],
        build_answer_card("邮箱绑定", message, status="error"),
    )


def conversation_key(payload: dict[str, Any], app: TenantApp) -> ConversationKey:
    event = normalize_event(payload, app, trust_event_tenant=True)
    conversation_id = event.get("chat_id") or f"user:{event.get('open_id') or 'unknown'}"
    return (event["tenant_key"], event["app_id"], conversation_id)


def process_event_thread(
    app_payload: dict[str, Any],
    payload: dict[str, Any],
    sequencer: ConversationSequencer | None = None,
    key: ConversationKey | None = None,
    ticket: int | None = None,
) -> None:
    app = TenantApp(**app_payload)
    try:
        if sequencer is not None and key is not None and ticket is not None:
            with sequencer.turn(key, ticket):
                asyncio.run(handle_queued_event(app, payload))
        else:
            asyncio.run(handle_queued_event(app, payload))
    except Exception:
        logger.exception("Failed to process Feishu message: bot_code=%s", app.bot_code)


async def handle_queued_event(app: TenantApp, payload: dict[str, Any]) -> None:
    if not event_scope_matches(payload, app, allow_unconfigured_tenant=True):
        logger.warning("Ignore event with mismatched tenant scope: bot_code=%s", app.bot_code)
        return
    event = normalize_event(payload, app, trust_event_tenant=True)

    message_id = event.get("message_id")
    if message_id and not await claim_event(
        tenant_key=event["tenant_key"], app_id=event["app_id"], message_id=message_id
    ):
        logger.info("Ignore duplicate message: bot_code=%s message_id=%s", app.bot_code, message_id)
        return

    try:
        await dispatch_event(app, event)
    except Exception as exc:
        if message_id:
            await finish_event(
                tenant_key=event["tenant_key"],
                app_id=event["app_id"],
                message_id=message_id,
                error=str(exc),
            )
        raise
    else:
        if message_id:
            await finish_event(
                tenant_key=event["tenant_key"], app_id=event["app_id"], message_id=message_id
            )


async def dispatch_event(app: TenantApp, event: dict[str, Any]) -> None:
    if event.get("open_id") and event.get("message_type") in {
        "text",
        "file",
        "image",
        "media",
        "video",
    }:
        event["_session_id"] = await get_active_session_id(
            tenant_key=event["tenant_key"],
            app_id=event["app_id"],
            open_id=event["open_id"],
            chat_id=event.get("chat_id"),
            bot_code=event.get("bot_code"),
        )
        await hydrate_reply_context(app, event)
    if event.get("message_type") in {"file", "image", "media", "video"}:
        await handle_file_event(app, event)
        return
    if event.get("message_type") != "text":
        logger.info("Ignore non-text message: bot_code=%s message_type=%s", app.bot_code, event.get("message_type"))
        return
    if not event.get("open_id") or not event.get("text"):
        logger.info("Ignore message without open_id/text: bot_code=%s", app.bot_code)
        return

    logger.info(
        "Received Feishu message: bot_code=%s tenant_key=%s open_id=%s message_id=%s chat_id=%s text_length=%s",
        app.bot_code,
        event.get("tenant_key"),
        event.get("open_id"),
        event.get("message_id"),
        event.get("chat_id"),
        len(event.get("text") or ""),
    )
    progress_message_id = None
    if event.get("message_id"):
        progress_message_id = await reply_card(app, event["message_id"], build_processing_card(event["text"]))
        event["_reply_message_id"] = progress_message_id
    handled = await handle_builtin_text_command(app, event, progress_message_id)
    if handled:
        return
    result = await run_agent(event)
    answer = result.content
    if event.get("message_id"):
        if should_use_card(answer):
            answer_card = build_answer_card(event["text"], answer, status=result.status)
            if progress_message_id:
                updated = await update_card(app, progress_message_id, answer_card)
                if not updated:
                    await reply_card(app, event["message_id"], answer_card)
            else:
                await reply_card(app, event["message_id"], answer_card)
        else:
            if progress_message_id:
                updated = await update_card(
                    app,
                    progress_message_id,
                    build_answer_card(event["text"], answer, status=result.status),
                )
                if not updated:
                    await reply_message(app, event["message_id"], answer)
            else:
                await reply_message(app, event["message_id"], answer)


async def handle_file_event(app: TenantApp, event: dict[str, Any]) -> None:
    message_id = event.get("message_id")
    file_key = event.get("file_key")
    if not message_id or not file_key:
        logger.info("Ignore resource message without message_id/file_key: bot_code=%s", app.bot_code)
        return

    logger.info(
        "Received Feishu resource: bot_code=%s message_id=%s resource_type=%s file_name=%s",
        app.bot_code,
        message_id,
        event.get("resource_type"),
        event.get("file_name"),
    )
    progress_message_id = await reply_card(app, message_id, build_processing_card(event.get("file_name") or "文件"))
    event["_reply_message_id"] = progress_message_id
    path = safe_resource_path(message_id, event.get("file_name"), event.get("resource_type"))
    try:
        await download_message_file(
            app,
            message_id=message_id,
            file_key=file_key,
            save_path=path,
            resource_type=event.get("resource_type") or "file",
        )
        answer = await register_uploaded_file(event, path)
        answer_status = "success"
    except Exception:  # noqa: BLE001
        logger.exception("Failed to download/analyze Feishu file: bot_code=%s", app.bot_code)
        answer = "文件处理失败，请稍后重试；如果问题持续，请联系信息管理中心。"
        answer_status = "error"
    if progress_message_id:
        await update_card(
            app,
            progress_message_id,
            build_answer_card(
                event.get("file_name") or "文件",
                answer,
                status=answer_status,
            ),
        )
    else:
        await reply_message(app, message_id, answer)


if __name__ == "__main__":
    main()
