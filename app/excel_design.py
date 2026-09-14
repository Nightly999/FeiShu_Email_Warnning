from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.models.router import invoke_chat_with_fallback


logger = logging.getLogger("excel_design")


class ExcelDesignError(RuntimeError):
    pass


class ExcelFilter(BaseModel):
    model_config = ConfigDict(extra="ignore")

    column: str
    operator: Literal[
        "equals",
        "not_equals",
        "contains",
        "not_contains",
        "in",
        "not_in",
        "greater_than",
        "greater_or_equal",
        "less_than",
        "less_or_equal",
        "is_blank",
        "is_not_blank",
    ]
    value: Any = None


class ExcelDeduplication(BaseModel):
    model_config = ConfigDict(extra="ignore")

    keys: list[str] = Field(default_factory=list)
    keep: Literal["first", "last", "merge_unique"] = "merge_unique"
    include_count: bool = False


class ExcelAggregation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    column: str = "*"
    function: Literal[
        "count",
        "count_distinct",
        "sum",
        "average",
        "min",
        "max",
        "join_unique",
        "first",
        "last",
    ]
    header: str


class ExcelSort(BaseModel):
    model_config = ConfigDict(extra="ignore")

    column: str
    direction: Literal["asc", "desc"] = "asc"


class ExcelOutputColumn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source: str
    header: str | None = None


class ExcelExportPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sheet_name: str = "查询结果"
    title: str = ""
    filename_suffix: str = ""
    filters: list[ExcelFilter] = Field(default_factory=list)
    deduplicate: ExcelDeduplication = Field(default_factory=ExcelDeduplication)
    group_by: list[str] = Field(default_factory=list)
    aggregations: list[ExcelAggregation] = Field(default_factory=list)
    sort_by: list[ExcelSort] = Field(default_factory=list)
    columns: list[ExcelOutputColumn] = Field(default_factory=list)


EXCEL_EXPORT_TOOL_NAME = "design_excel_export"
EXCEL_EXPORT_TOOL = {
    "type": "function",
    "function": {
        "name": EXCEL_EXPORT_TOOL_NAME,
        "description": (
            "根据用户要求为已有查询结果设计 Excel。只返回声明式、安全的表格处理方案，"
            "由系统执行筛选、去重、分组、汇总、排序、选列和改列名。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sheet_name": {"type": "string"},
                "title": {"type": "string"},
                "filename_suffix": {"type": "string"},
                "filters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "column": {"type": "string"},
                            "operator": {
                                "type": "string",
                                "enum": [
                                    "equals",
                                    "not_equals",
                                    "contains",
                                    "not_contains",
                                    "in",
                                    "not_in",
                                    "greater_than",
                                    "greater_or_equal",
                                    "less_than",
                                    "less_or_equal",
                                    "is_blank",
                                    "is_not_blank",
                                ],
                            },
                            "value": {},
                        },
                        "required": ["column", "operator"],
                        "additionalProperties": False,
                    },
                },
                "deduplicate": {
                    "type": "object",
                    "properties": {
                        "keys": {"type": "array", "items": {"type": "string"}},
                        "keep": {
                            "type": "string",
                            "enum": ["first", "last", "merge_unique"],
                        },
                        "include_count": {"type": "boolean"},
                    },
                    "required": ["keys", "keep", "include_count"],
                    "additionalProperties": False,
                },
                "group_by": {"type": "array", "items": {"type": "string"}},
                "aggregations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "column": {"type": "string"},
                            "function": {
                                "type": "string",
                                "enum": [
                                    "count",
                                    "count_distinct",
                                    "sum",
                                    "average",
                                    "min",
                                    "max",
                                    "join_unique",
                                    "first",
                                    "last",
                                ],
                            },
                            "header": {"type": "string"},
                        },
                        "required": ["column", "function", "header"],
                        "additionalProperties": False,
                    },
                },
                "sort_by": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "column": {"type": "string"},
                            "direction": {"type": "string", "enum": ["asc", "desc"]},
                        },
                        "required": ["column", "direction"],
                        "additionalProperties": False,
                    },
                },
                "columns": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source": {"type": "string"},
                            "header": {"type": ["string", "null"]},
                        },
                        "required": ["source"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [
                "sheet_name",
                "title",
                "filename_suffix",
                "filters",
                "deduplicate",
                "group_by",
                "aggregations",
                "sort_by",
                "columns",
            ],
            "additionalProperties": False,
        },
    },
}


EXCEL_DESIGN_SYSTEM_PROMPT = """
你是企业飞书助手中的 Excel 设计代理。你必须调用 design_excel_export 工具，不能直接回答。

你会收到用户原始要求、真实字段清单和字段样例。请把要求翻译为声明式处理方案：
1. 所有 column、keys、group_by、source 必须使用字段清单中的真实字段名，不得猜造字段。
2. 没有明确要求的筛选、汇总、删列、改名或排序不得擅自增加。
3. 只要求导出时，保留全部数据，所有操作数组留空。
4. 要求去重时，把用户所指字段放入 deduplicate.keys。未指定保留规则时使用 merge_unique，避免丢失其他字段；只有用户明确要求保留第一条或最后一条时才使用 first 或 last。
5. 只有用户明确要求统计、汇总、分组时才设置 group_by 和 aggregations。
6. columns 为空表示保留处理后的全部字段；仅在用户要求选列、列顺序或改列名时填写。
7. title 仅在用户明确要求表内标题时填写，否则留空。sheet_name 和 filename_suffix 应简短、贴合要求。
8. 不生成代码、SQL、公式或不存在的数据。系统会按 filters → deduplicate → group/aggregate → sort → columns 的顺序执行。
"""


async def plan_excel_export(
    user_request: str, rows: list[dict[str, Any]]
) -> ExcelExportPlan:
    if not rows:
        raise ExcelDesignError("没有可用于设计 Excel 的数据")
    dataset = build_dataset_profile(rows)
    payload = {
        "user_request": user_request,
        "dataset": dataset,
    }
    try:
        response = await invoke_chat_with_fallback(
            messages=[
                SystemMessage(content=EXCEL_DESIGN_SYSTEM_PROMPT),
                HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str)),
            ],
            route="text",
            tools=[EXCEL_EXPORT_TOOL],
            temperature=0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Excel design model failed")
        raise ExcelDesignError("Excel 设计模型暂时不可用，请稍后重试") from exc
    arguments = extract_plan_arguments(response)
    try:
        return ExcelExportPlan.model_validate(arguments)
    except ValidationError as exc:
        logger.warning("Invalid Excel export plan: %s", exc)
        raise ExcelDesignError("模型生成的 Excel 方案格式不正确，请换一种说法重试") from exc


def build_dataset_profile(rows: list[dict[str, Any]]) -> dict[str, Any]:
    headers = collect_headers(rows)
    columns: list[dict[str, Any]] = []
    for header in headers:
        samples: list[Any] = []
        seen: set[str] = set()
        non_empty = 0
        for row in rows:
            value = row.get(header)
            if is_blank(value):
                continue
            non_empty += 1
            marker = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            if marker not in seen and len(samples) < 6:
                seen.add(marker)
                samples.append(value)
        columns.append(
            {
                "name": header,
                "non_empty_count": non_empty,
                "sample_values": samples,
            }
        )
    return {"row_count": len(rows), "columns": columns}


def extract_plan_arguments(response: Any) -> dict[str, Any]:
    for call in getattr(response, "tool_calls", None) or []:
        if call.get("name") != EXCEL_EXPORT_TOOL_NAME:
            continue
        arguments = call.get("args") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ExcelDesignError("模型返回了无法解析的 Excel 方案") from exc
        if isinstance(arguments, dict):
            return arguments

    content = str(getattr(response, "content", "") or "").strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:].strip()
    try:
        arguments = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ExcelDesignError("模型没有调用 Excel 设计工具，请重试") from exc
    if not isinstance(arguments, dict):
        raise ExcelDesignError("模型返回的 Excel 方案不是对象")
    return arguments


def apply_excel_export_plan(
    rows: list[dict[str, Any]], plan: ExcelExportPlan
) -> list[dict[str, Any]]:
    transformed = [dict(row) for row in rows]
    headers = collect_headers(transformed)

    for item in plan.filters:
        column = resolve_column(headers, item.column)
        transformed = [
            row
            for row in transformed
            if matches_filter(row.get(column), item.operator, item.value)
        ]

    if plan.deduplicate.keys:
        keys = [resolve_column(headers, key) for key in plan.deduplicate.keys]
        transformed = deduplicate_rows(
            transformed,
            keys,
            keep=plan.deduplicate.keep,
            include_count=plan.deduplicate.include_count,
        )
        headers = collect_headers(transformed)

    if plan.group_by or plan.aggregations:
        if not plan.group_by:
            raise ExcelDesignError("汇总方案缺少分组字段")
        group_keys = [resolve_column(headers, key) for key in plan.group_by]
        transformed = aggregate_rows(
            transformed,
            headers,
            group_keys,
            plan.aggregations,
        )
        headers = collect_headers(transformed)

    for item in reversed(plan.sort_by):
        column = resolve_column(headers, item.column)
        transformed = sort_rows(transformed, column, item.direction)

    if plan.columns:
        selected: list[tuple[str, str]] = []
        for item in plan.columns:
            source = resolve_column(headers, item.source)
            target = (item.header or source).strip() or source
            selected.append((source, target))
        targets = [target for _, target in selected]
        if len(targets) != len(set(targets)):
            raise ExcelDesignError("Excel 方案包含重复的输出列名")
        transformed = [
            {target: row.get(source) for source, target in selected}
            for row in transformed
        ]

    if not transformed:
        raise ExcelDesignError("按要求处理后没有可导出的数据")
    return transformed


def collect_headers(rows: list[dict[str, Any]]) -> list[str]:
    headers: list[str] = []
    for row in rows:
        for key in row:
            name = str(key)
            if name not in headers:
                headers.append(name)
    return headers


def resolve_column(headers: list[str], requested: str) -> str:
    if requested in headers:
        return requested
    normalized = normalize_column_name(requested)
    matches = [header for header in headers if normalize_column_name(header) == normalized]
    if len(matches) == 1:
        return matches[0]
    raise ExcelDesignError(f"Excel 方案引用了不存在的字段：{requested}")


def normalize_column_name(value: str) -> str:
    return re.sub(r"[\s_\-（）()]+", "", str(value)).casefold()


def deduplicate_rows(
    rows: list[dict[str, Any]],
    keys: list[str],
    *,
    keep: Literal["first", "last", "merge_unique"],
    include_count: bool,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        values = tuple(normalize_group_value(row.get(key)) for key in keys)
        if all(not value for value in values):
            values = (*values, f"__blank_row_{index}")
        groups.setdefault(values, []).append(row)

    if keep == "last":
        result = [dict(group[-1]) for group in groups.values()]
    elif keep == "first":
        result = [dict(group[0]) for group in groups.values()]
    else:
        headers = collect_headers(rows)
        result = []
        for group in groups.values():
            merged: dict[str, Any] = {}
            for header in headers:
                if header in keys:
                    merged[header] = first_non_blank(row.get(header) for row in group)
                else:
                    merged[header] = merge_distinct_values(
                        row.get(header) for row in group
                    )
            result.append(merged)

    if include_count:
        count_header = unique_header(collect_headers(result), "合并记录数")
        for item, group in zip(result, groups.values(), strict=True):
            item[count_header] = len(group)
    return result


def aggregate_rows(
    rows: list[dict[str, Any]],
    headers: list[str],
    group_keys: list[str],
    aggregations: list[ExcelAggregation],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        key = tuple(normalize_group_value(row.get(column)) for column in group_keys)
        if all(not value for value in key):
            key = (*key, f"__blank_row_{index}")
        groups.setdefault(key, []).append(row)

    resolved_aggregations: list[tuple[str, ExcelAggregation]] = []
    output_headers = list(group_keys)
    for item in aggregations:
        column = "*" if item.column == "*" else resolve_column(headers, item.column)
        header = item.header.strip()
        if not header or header in output_headers:
            raise ExcelDesignError(f"汇总输出列名无效或重复：{item.header}")
        output_headers.append(header)
        resolved_aggregations.append((column, item))

    result: list[dict[str, Any]] = []
    for group in groups.values():
        output = {
            column: first_non_blank(row.get(column) for row in group)
            for column in group_keys
        }
        for column, item in resolved_aggregations:
            values = [row.get(column) for row in group] if column != "*" else []
            output[item.header] = aggregate_values(values, item.function, len(group))
        result.append(output)
    return result


def aggregate_values(values: list[Any], function: str, row_count: int) -> Any:
    non_blank = [value for value in values if not is_blank(value)]
    if function == "count":
        return row_count if not values else len(non_blank)
    if function == "count_distinct":
        return len({normalize_group_value(value) for value in non_blank})
    if function == "join_unique":
        return merge_distinct_values(non_blank)
    if function == "first":
        return first_non_blank(non_blank)
    if function == "last":
        return first_non_blank(reversed(non_blank))

    numbers = [number for value in non_blank if (number := to_decimal(value)) is not None]
    if function in {"sum", "average"}:
        if not numbers:
            return ""
        total = sum(numbers, Decimal(0))
        value = total if function == "sum" else total / len(numbers)
        return decimal_to_number(value)
    if function in {"min", "max"}:
        if not non_blank:
            return ""
        if len(numbers) == len(non_blank):
            value = min(numbers) if function == "min" else max(numbers)
        else:
            selector = min if function == "min" else max
            value = selector(non_blank, key=lambda item: str(item).casefold())
        return decimal_to_number(value) if isinstance(value, Decimal) else value
    raise ExcelDesignError(f"不支持的汇总方式：{function}")


def matches_filter(value: Any, operator: str, expected: Any) -> bool:
    if operator == "is_blank":
        return is_blank(value)
    if operator == "is_not_blank":
        return not is_blank(value)
    if operator in {"in", "not_in"}:
        choices = expected if isinstance(expected, list) else [expected]
        matched = any(values_equal(value, choice) for choice in choices)
        return matched if operator == "in" else not matched
    if operator in {"contains", "not_contains"}:
        matched = str(expected or "").casefold() in str(value or "").casefold()
        return matched if operator == "contains" else not matched
    if operator in {"equals", "not_equals"}:
        matched = values_equal(value, expected)
        return matched if operator == "equals" else not matched

    comparison = compare_values(value, expected)
    if comparison is None:
        return False
    return {
        "greater_than": comparison > 0,
        "greater_or_equal": comparison >= 0,
        "less_than": comparison < 0,
        "less_or_equal": comparison <= 0,
    }[operator]


def values_equal(left: Any, right: Any) -> bool:
    left_number = to_decimal(left)
    right_number = to_decimal(right)
    if left_number is not None and right_number is not None:
        return left_number == right_number
    return str(left or "").strip().casefold() == str(right or "").strip().casefold()


def compare_values(left: Any, right: Any) -> int | None:
    if is_blank(left) or is_blank(right):
        return None
    left_number = to_decimal(left)
    right_number = to_decimal(right)
    if left_number is not None and right_number is not None:
        return (left_number > right_number) - (left_number < right_number)
    left_date = to_datetime(left)
    right_date = to_datetime(right)
    if left_date is not None and right_date is not None:
        left_date = normalize_datetime(left_date)
        right_date = normalize_datetime(right_date)
        return (left_date > right_date) - (left_date < right_date)
    left_text = str(left).strip().casefold()
    right_text = str(right).strip().casefold()
    return (left_text > right_text) - (left_text < right_text)


def sort_rows(
    rows: list[dict[str, Any]], column: str, direction: Literal["asc", "desc"]
) -> list[dict[str, Any]]:
    populated = [row for row in rows if not is_blank(row.get(column))]
    blank = [row for row in rows if is_blank(row.get(column))]
    populated.sort(
        key=lambda row: sortable_value(row.get(column)),
        reverse=direction == "desc",
    )
    return [*populated, *blank]


def sortable_value(value: Any) -> tuple[int, Any]:
    number = to_decimal(value)
    if number is not None:
        return (0, number)
    date = to_datetime(value)
    if date is not None:
        return (1, normalize_datetime(date))
    return (2, str(value).casefold())


def to_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def to_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def decimal_to_number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def normalize_group_value(value: Any) -> str:
    if is_blank(value):
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).casefold()


def first_non_blank(values: Any) -> Any:
    return next((value for value in values if not is_blank(value)), "")


def merge_distinct_values(values: Any) -> Any:
    distinct: list[Any] = []
    seen: set[str] = set()
    for value in values:
        if is_blank(value):
            continue
        normalized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if normalized in seen:
            continue
        seen.add(normalized)
        distinct.append(value)
    if not distinct:
        return ""
    if len(distinct) == 1:
        return distinct[0]
    return "；".join(str(normalize_cell(value)) for value in distinct)


def normalize_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def unique_header(headers: list[str], base: str) -> str:
    if base not in headers:
        return base
    suffix = 2
    while f"{base}{suffix}" in headers:
        suffix += 1
    return f"{base}{suffix}"
