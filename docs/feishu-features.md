# Feishu Feature Notes

This project follows the same practical pattern used by OpenClaw-style Feishu agents:

- receive a Feishu event;
- normalize message metadata such as `message_id`, `chat_id`, `open_id`, resource keys, and tenant app identity;
- do local processing or call the agent;
- reply with text, card, or uploaded file.

## Export Excel

Flow:

1. MCP tool results are cached in `agent_tool_result_cache`.
2. User requests an Excel file and may describe how it should be prepared, for example filtering,
   deduplication, grouping, aggregation, sorting, column selection, or column renaming.
3. The text model receives the real result schema and compact field samples, then calls the internal
   `design_excel_export` tool with a declarative workbook plan. No field-specific export rule is
   hardcoded.
4. The latest cached rows for the current tenant, user, chat, and `/new` session are loaded in full.
   A local executor validates all referenced columns and applies only allowlisted operations from the
   plan before writing the styled `.xlsx` workbook.
5. The workbook is uploaded with Feishu IM file upload API and the bot replies with a file message.

### Quoted replies

Feishu message events preserve `parent_id`, `root_id`, and `thread_id`. When a user replies to a
specific bot card, the referenced card text is added to the Agent context with higher relevance than
unrelated recent turns. Export contexts also store both the user's request message ID and the bot's
reply message ID. Therefore a quoted `导出 Excel` request selects the tool result associated with the
quoted card instead of whichever query happened most recently. Scope and session checks still apply,
and an unmatched quote never falls back to unrelated data.

## Agent scheduled tasks

Known concise schedule commands continue to use the deterministic parser. Natural-language creation
requests that do not match those forms are sent to the text model with the current Shanghai time.
The model calls the internal `plan_scheduled_task` tool to select a validated schedule, execution
mode, and task goal. At run time, `agent` tasks call the full Agent with a new isolated automation
session, so it can select allowlisted MCP tools and fetch current business data instead of replaying a
fixed answer. A mode-only reply to a quoted schedule help card can reuse the original creation request.

Main files:

- `app/excel_export.py`
- `app/tool_result_cache.py`
- `app/feishu.py`
- `app/feishu_ws.py`

## Business Detail Pagination

MCP results with a `rows` array are cached and bound to the current tenant, user, chat,
and `/new` session. Each card carries 20 rows by default and uses Feishu's native table pager to
show 10 rows at a time. When the result exceeds `BUSINESS_LIST_PAGE_SIZE`, the first server page is
appended to the Agent answer as a native Feishu table.

If the MCP response contains `page`, `pageSize`, `totalCount`, and `totalPages`, subsequent page
commands re-check the current user and tool permissions and fetch that page from MCP. Legacy MCP
responses without this metadata continue to use session-scoped local-cache pagination. Full Excel
exports securely fetch and merge every MCP page before creating the workbook.

Conversation commands:

- `下一页`
- `上一页`
- `第 3 页`
- `业务明细 第 3 页`

A new MCP query replaces the previous business-list cursor. `/new`, another chat, or another
user cannot access the old cursor. The card defaults to 20 total columns (including the frozen
sequence column), supports horizontal scrolling, and allows `BUSINESS_LIST_MAX_COLUMNS` up to
Feishu's 50-column limit. Users can still use `导出 Excel` for all rows and columns.

## Uploaded Files

Flow:

1. Feishu message events are normalized into `file_key`, `resource_type`, and `file_name`.
2. `file` and `image` messages are downloaded through the Feishu message resource API.
3. Files are saved under `data/uploads`.
4. Basic analyzers preview `txt`, `csv`, `xlsx`, images, and videos.
5. Images go to the `vision` model route. Videos go to the `video` model route.

This mirrors OpenClaw's resource handling idea: message resources should be fetched by `message_id + file_key + type`.

Main files:

- `app/feishu.py`
- `app/file_analysis.py`
- `app/feishu_ws.py`

## Scheduled Tasks

Flow:

1. User creates a task in chat.
2. The task is stored in `scheduled_task`.
3. A background scheduler checks due tasks with a database lease to prevent duplicate runs.
4. Reminder tasks send text directly; Agent tasks run in a fresh isolated session.
5. Agent tasks re-check identity and tool permissions on every run.
6. The final answer is delivered to the original `chat_id` and the run is recorded.
7. Transient failures retry with backoff; permission denial or exhausted retries disables the task.

Supported commands:

- `定时 2026-08-16 18:30 提醒我提交日报` (reminder mode)
- `每天 09:00 查询样品风险并汇总` (isolated Agent mode)
- `创建名为“每日样品风险”的定时任务：每天 09:00 查询样品风险并汇总`
- `每天 09:00 任务名称：早间风险；查询样品风险并汇总`
- `查看定时任务`
- `查看定时任务 第2页`
- `暂停定时任务 #12`
- `恢复定时任务 #12`
- `立即执行定时任务 #12`
- `查看定时任务 #12 运行记录`
- `查看定时任务 #12 运行记录 第2页`
- `取消定时任务 #12`

Task names:

- An explicit name is preserved as entered and must be unique in the user's chat.
- Without an explicit name, a short name is generated from the task purpose.
- Generated name collisions receive a numeric suffix such as `（2）`.
- Creation responses include name, id, schedule, timezone, execution mode, prompt,
  next run, timeout, and retry policy.

Runtime controls:

- `SCHEDULER_POLL_SECONDS`
- `SCHEDULER_TASK_TIMEOUT_SECONDS`
- `SCHEDULER_MAX_RETRIES`
- `SCHEDULER_CONCURRENCY`
- `SCHEDULER_LIST_PAGE_SIZE` (default `5`)
- `SCHEDULER_HISTORY_PAGE_SIZE` (default `5`)

Task and run-history lists show the current page, total pages, total rows, and copyable
previous/next page commands. Cancelled tasks are hidden; paused and failed tasks remain visible
so users can resume them or inspect their history.

Main files:

- `app/scheduler.py`
- `app/scheduler_runtime.py`
- `app/feishu_ws.py`
- `app/bootstrap.py`
