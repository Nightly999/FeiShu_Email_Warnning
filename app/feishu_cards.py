from __future__ import annotations

import re
from typing import Any


MAX_CARD_TEXT_LENGTH = 5500
MAX_CARD_ELEMENTS = 24
MAX_TABLE_COMPONENTS = 5
MAX_TABLE_COLUMNS = 50
# Feishu table page_size only supports [1, 10]. Load up to 20 rows so the card
# shows built-in ↑↓ pagination (e.g. 1/2) instead of asking users to type commands.
TABLE_PAGE_SIZE = 10
MAX_TABLE_ROWS = 20


def normalize_answer_for_card(answer: str) -> str:
    text = (answer or "").strip()
    text = normalize_headings(text)
    text = normalize_horizontal_rules(text)
    return text or "邮件已处理，但没有生成可展示的分析结果。"


def parse_markdown_row(line: str) -> list[str] | None:
    if not (line.startswith("|") and line.endswith("|")):
        return None
    parts = [clean_cell(part) for part in line.strip("|").split("|")]
    if len(parts) < 2:
        return None
    if all(is_markdown_separator(part) for part in parts):
        return None
    return parts


def build_table_component(headers: list[str], rows: list[list[str]], index: int) -> dict[str, Any]:
    safe_headers = [
        clean_value_text(header) or f"列{idx + 1}"
        for idx, header in enumerate(headers[:MAX_TABLE_COLUMNS])
    ]
    row_objects: list[dict[str, str]] = []
    for row in rows[:MAX_TABLE_ROWS]:
        item: dict[str, str] = {}
        for col_index, _header in enumerate(safe_headers):
            item[f"col_{col_index}"] = clean_value_text(row[col_index]) if col_index < len(row) else "-"
        row_objects.append(item)

    return {
        "tag": "table",
        "element_id": f"table_{index}",
        "page_size": TABLE_PAGE_SIZE,
        "row_height": "low",
        "freeze_first_column": True,
        "columns": [
            {
                "name": f"col_{col_index}",
                "display_name": header,
                "data_type": "text",
                "horizontal_align": "left",
                "width": "auto",
            }
            for col_index, header in enumerate(safe_headers)
        ],
        "rows": row_objects,
    }


def parse_key_value_line(line: str) -> tuple[str, str] | None:
    match = re.match(
        r"^(?:[-*]\s*)?(?:\d+[.)、]\s*)?(?:\*\*)?([^:：|]{1,40})(?:\*\*)?\s*[:：]\s*(.+)$",
        line,
        re.S,
    )
    if not match:
        return None
    key = clean_cell(match.group(1), strip_leading_icons=True)
    value = clean_cell(match.group(2))
    if not key or not value:
        return None
    return key.lstrip("> ").strip(), value


def parse_heading(line: str) -> str | None:
    stripped = line.strip()
    if stripped.startswith("**") and stripped.endswith("**") and len(stripped) > 4:
        return clean_section_line(stripped.strip("*"))
    return None


def clean_section_line(line: str) -> str:
    line = re.sub(r"^[#>\-\s\d.、]*", "", line.strip())
    line = line.strip("* ")
    line = normalize_broken_markdown(line)
    return line


def normalize_list_marker(line: str) -> str:
    if re.match(r"^[-*]\s+", line):
        return re.sub(r"^[-*]\s+", "- ", line)
    return line


def is_markdown_separator(text: str) -> bool:
    return bool(re.fullmatch(r":?-{2,}:?", text.strip()))


def is_markdown_table_separator_line(line: str) -> bool:
    stripped = line.strip().strip("|")
    if "|" not in stripped:
        return False
    cells = [cell.strip() for cell in stripped.split("|")]
    return bool(cells) and all(is_markdown_separator(cell) for cell in cells)


def clean_cell(text: str, *, strip_leading_icons: bool = False) -> str:
    text = normalize_broken_markdown((text or "").strip())
    if strip_leading_icons:
        text = re.sub(r"^[✅❌⚠️📋📊👉💡⏳\s]+", "", text).strip()
    return text


def clean_value_text(text: str) -> str:
    text = clean_cell(text)
    text = re.sub(r"\s+\|\s+", "\n", text)
    return text.replace("|", "｜").strip()


def normalize_broken_markdown(text: str) -> str:
    return re.sub(r"\*{2,}", "", text).strip()


def normalize_headings(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("### "):
            lines.append(f"**{stripped[4:]}**")
        elif stripped.startswith("## "):
            lines.append(f"**{stripped[3:]}**")
        elif stripped.startswith("# "):
            lines.append(f"**{stripped[2:]}**")
        else:
            lines.append(line)
    return "\n".join(lines)


def normalize_horizontal_rules(text: str) -> str:
    return re.sub(r"(?m)^\s*-{3,}\s*$", "", text)


def trim_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n\n...内容较长，已截断。"


def escape_lark_md(text: str) -> str:
    return (text or "").replace("<", "&lt;").replace(">", "&gt;")


# Dynamic Card JSON 2.0 renderer.
# It intentionally does not use business keywords or renderer config:
# - consecutive key/value lines become a markdown list
# - markdown tables become native Feishu table components
# - repeated "item + key/value details" groups become a dynamic table


def should_use_card(answer: str) -> bool:
    text = (answer or "").strip()
    if not text:
        return False
    return len(text) > 180 or "\n" in text or bool(parse_key_value_line(text) or parse_markdown_row(text))


def build_processing_card(question: str) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "正在处理您的请求..."},
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "正在理解您的指令，请稍候。\n\n"
                        f"**您的指令**：{escape_lark_md(trim_text(question, 500))}"
                    ),
                }
            ]
        },
    }


def build_welcome_card() -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "green",
            "title": {"tag": "plain_text", "content": "来邮速递｜AI 邮件助手 📬"},
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "我可以按你的指令同步、筛选和分析公司邮箱，"
                        "并将邮件简报私聊发送给你。"
                    ),
                },
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": (
                        "**🔐 绑定邮箱**\n"
                        "- 首次发送“分析我的邮箱”，按卡片提示填写邮箱账号和密码\n"
                        "- 发送“重新绑定邮箱”可以覆盖原绑定"
                    ),
                },
                {
                    "tag": "markdown",
                    "content": (
                        "**📨 查询与分析**\n"
                        "- 可指定最近几小时或几天、邮件数量、发件人和关键词\n"
                        "- 支持筛选未处理、收件人、抄送和正文提及你的邮件\n"
                        "- 输出摘要、优先级、待办、负责人、截止时间、风险和附件"
                    ),
                },
                {
                    "tag": "markdown",
                    "content": (
                        "**⏰ 定时邮件简报**\n"
                        "- 可用自然语言设置每天一个或多个推送时间\n"
                        "- 到点自动同步、分析邮件，并私聊发送结果\n"
                        "- 支持查看、修改或取消定时任务"
                    ),
                },
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": (
                        "💡 **示例**：分析最近两天的邮件\n"
                        "💡 **示例**：每天 9:30 和 17:20 分析未处理邮件并推送给我"
                    ),
                },
                {
                    "tag": "markdown",
                    "content": (
                        "<font color=\"grey\">邮箱密码仅用于 POP3 登录验证，不用于模型训练。"
                        "</font>"
                    ),
                },
            ]
        },
    }


def build_answer_card(
    question: str,
    answer: str,
    *,
    status: str = "success",
    title: str | None = None,
    footer_label: str = "您的指令",
) -> dict[str, Any]:
    content = normalize_answer_for_card(answer)
    template, default_title = {
        "success": ("green", "邮件处理结果"),
        "error": ("red", "邮件处理失败"),
        "denied": ("orange", "操作提示"),
    }.get(status, ("green", "邮件处理结果"))
    card_title = title or default_title
    dynamic_elements = _enforce_table_limit(_build_dynamic_elements(content))
    dynamic_elements = _limit_elements_preserving_tail(
        dynamic_elements, MAX_CARD_ELEMENTS - 2
    )
    elements = [
        *dynamic_elements,
        {"tag": "hr"},
        {
            "tag": "markdown",
            "content": (
                f"<font color=\"grey\">{escape_lark_md(footer_label)}："
                f"{escape_lark_md(trim_text(question, 160))}</font>"
            ),
        },
    ]
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": card_title},
        },
        "body": {"elements": elements},
    }


def _enforce_table_limit(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    table_indexes = [
        index for index, element in enumerate(elements) if element.get("tag") == "table"
    ]
    if len(table_indexes) <= MAX_TABLE_COMPONENTS:
        return elements

    # Preserve the first summary tables and the final table, which is commonly
    # the paged MCP business detail appended after the model's summary.
    keep = set(table_indexes[: MAX_TABLE_COMPONENTS - 1])
    keep.add(table_indexes[-1])
    return [
        element if index in keep or element.get("tag") != "table" else _table_as_markdown(element)
        for index, element in enumerate(elements)
    ]


def _table_as_markdown(table: dict[str, Any]) -> dict[str, str]:
    columns = list(table.get("columns") or [])
    lines = ["**更多汇总**"]
    for row in list(table.get("rows") or [])[:MAX_TABLE_ROWS]:
        if not isinstance(row, dict):
            continue
        values = []
        for column in columns:
            name = str(column.get("name") or "")
            label = str(column.get("display_name") or name)
            value = clean_value_text(str(row.get(name, "-"))).replace("\n", " / ")
            values.append(f"**{escape_lark_md(label)}**：{escape_lark_md(value)}")
        if values:
            lines.append("- " + "；".join(values))
    return {"tag": "markdown", "content": trim_text("\n".join(lines), MAX_CARD_TEXT_LENGTH)}


def _limit_elements_preserving_tail(
    elements: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    if len(elements) <= limit:
        return elements
    return [*elements[: limit - 1], elements[-1]]


def _build_dynamic_elements(content: str) -> list[dict[str, Any]]:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    elements: list[dict[str, Any]] = []
    pending_text: list[str] = []
    pending_pairs: list[tuple[str, str]] = []
    pending_group_rows: list[dict[str, str]] = []
    index = 0

    def flush_text() -> None:
        if not pending_text:
            return
        text = "\n".join(normalize_list_marker(line) for line in pending_text).strip()
        pending_text.clear()
        if text:
            elements.append({"tag": "markdown", "content": trim_text(text, MAX_CARD_TEXT_LENGTH)})

    def flush_pairs() -> None:
        if not pending_pairs:
            return
        lines_md = [
            f"- **{escape_lark_md(key)}**：{escape_lark_md(clean_value_text(value).replace(chr(10), ' / '))}"
            for key, value in pending_pairs
        ]
        pending_pairs.clear()
        elements.append({"tag": "markdown", "content": trim_text("\n".join(lines_md), MAX_CARD_TEXT_LENGTH)})

    def flush_group_rows() -> None:
        if not pending_group_rows:
            return
        rows = pending_group_rows[:]
        pending_group_rows.clear()
        headers = _dynamic_headers(rows)
        values = [[row.get(header, "-") for header in headers] for row in rows]
        elements.append(build_table_component(headers, values, len(elements)))

    def flush_all() -> None:
        flush_group_rows()
        flush_pairs()
        flush_text()

    def add_heading(title_text: str) -> None:
        flush_all()
        elements.append({"tag": "markdown", "content": f"**{escape_lark_md(clean_section_line(title_text))}**"})

    while index < len(lines):
        line = lines[index]
        if is_markdown_table_separator_line(line):
            index += 1
            continue

        table = _collect_markdown_table(lines, index)
        if table:
            flush_all()
            headers, rows, next_index = table
            elements.append(build_table_component(headers, rows, len(elements)))
            index = next_index
            continue

        group = _collect_key_value_group(lines, index, has_pending=bool(pending_group_rows))
        if group:
            flush_pairs()
            flush_text()
            pending_group_rows.append(group[0])
            index = group[1]
            continue

        heading = parse_heading(line)
        if heading:
            add_heading(heading)
            index += 1
            continue

        if _looks_like_dynamic_heading(lines, index):
            add_heading(line)
            index += 1
            continue

        key_value = parse_key_value_line(line)
        if key_value:
            flush_group_rows()
            flush_text()
            pending_pairs.append(key_value)
            index += 1
            continue

        flush_group_rows()
        flush_pairs()
        pending_text.append(line)
        index += 1

    flush_all()
    return elements or [{"tag": "markdown", "content": "邮件处理完成。"}]


def _collect_markdown_table(lines: list[str], start: int) -> tuple[list[str], list[list[str]], int] | None:
    first = parse_markdown_row(lines[start])
    if not first:
        return None

    rows: list[list[str]] = [first]
    index = start + 1
    while index < len(lines):
        if is_markdown_table_separator_line(lines[index]):
            index += 1
            continue
        row = parse_markdown_row(lines[index])
        if not row:
            break
        rows.append(row)
        index += 1

    if len(rows) < 2:
        return None
    return rows[0], rows[1:], index


def _collect_key_value_group(lines: list[str], start: int, *, has_pending: bool = False) -> tuple[dict[str, str], int] | None:
    title_text = clean_cell(lines[start], strip_leading_icons=True)
    if not _is_plain_label(title_text):
        return None
    if start + 1 >= len(lines) or not parse_key_value_line(lines[start + 1]):
        return None

    row: dict[str, str] = {"项目": title_text}
    index = start + 1
    count = 0
    while index < len(lines):
        parsed = parse_key_value_line(lines[index])
        if not parsed:
            break
        key, value = parsed
        row[key] = clean_value_text(value)
        count += 1
        index += 1

    if count < 2:
        return None
    if not has_pending and not _has_following_key_value_group(lines, index):
        return None
    return row, index


def _has_following_key_value_group(lines: list[str], index: int) -> bool:
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index + 1 >= len(lines):
        return False
    title_text = clean_cell(lines[index], strip_leading_icons=True)
    return _is_plain_label(title_text) and bool(parse_key_value_line(lines[index + 1]))


def _looks_like_dynamic_heading(lines: list[str], index: int) -> bool:
    line = clean_cell(lines[index], strip_leading_icons=True)
    if not _is_plain_label(line):
        return False
    if index + 1 >= len(lines):
        return False
    next_line = lines[index + 1]
    if parse_markdown_row(next_line) or parse_key_value_line(next_line):
        return True
    if index + 2 < len(lines) and _is_plain_label(next_line) and parse_key_value_line(lines[index + 2]):
        return True
    return False


def _dynamic_headers(rows: list[dict[str, str]]) -> list[str]:
    headers: list[str] = []
    for row in rows:
        for key in row:
            if key not in headers:
                headers.append(key)
    return headers[:6]


def _is_plain_label(text: str) -> bool:
    if not text or len(text) > 32:
        return False
    return not any(mark in text for mark in (":", "：", "|", "。", "，", ",", "、", "；", ";"))
