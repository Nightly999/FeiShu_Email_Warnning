from __future__ import annotations

import csv
from pathlib import Path

from openpyxl import load_workbook

from app.models.router import analyze_image_with_vision_model, analyze_video_with_video_model
from app.settings import get_settings


UPLOAD_DIR = Path("data/uploads")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".mpeg", ".mpg"}


def safe_upload_path(message_id: str, file_name: str | None) -> Path:
    name = file_name or f"{message_id}.bin"
    safe = "".join("_" if ch in '\\/:*?"<>|' else ch for ch in name)
    return UPLOAD_DIR / f"{message_id}_{safe}"


def safe_resource_path(message_id: str, file_name: str | None, resource_type: str | None) -> Path:
    if file_name:
        return safe_upload_path(message_id, file_name)
    suffix = ".png" if resource_type == "image" else ".bin"
    return safe_upload_path(message_id, f"{message_id}{suffix}")


def analyze_uploaded_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".log"}:
        return analyze_text_file(path)
    if suffix == ".csv":
        return analyze_csv_file(path)
    if suffix == ".xlsx":
        return analyze_xlsx_file(path)
    if suffix in IMAGE_SUFFIXES:
        return analyze_image_file(path)
    if suffix in VIDEO_SUFFIXES:
        return analyze_video_file(path)
    return (
        f"已收到文件：{path.name}\n"
        f"大小：{path.stat().st_size} 字节\n\n"
        "当前基础版支持预览 txt、csv、xlsx 文件。这个文件已保存，后续可以扩展对应解析器。"
    )


async def analyze_uploaded_file_async(path: Path) -> str:
    if path.suffix.lower() in IMAGE_SUFFIXES:
        return await analyze_image_with_vision_model(path)
    if path.suffix.lower() in VIDEO_SUFFIXES:
        settings = get_settings()
        size = path.stat().st_size
        if size > settings.max_video_upload_bytes:
            return (
                f"视频文件过大：{path.name}\n"
                f"大小：{size} 字节\n\n"
                f"当前直接送入视频模型的上限是 {settings.max_video_upload_bytes} 字节。"
                "请压缩视频，或后续改用公网 URL/对象存储 URL 方式调用视频模型。"
            )
        return await analyze_video_with_video_model(path)
    return analyze_uploaded_file(path)


def analyze_image_file(path: Path) -> str:
    return (
        f"已收到图片：{path.name}\n"
        f"大小：{path.stat().st_size} 字节\n\n"
        "图片已保存。后续如果启用视觉模型，可以把图片内容交给模型识别和分析。"
    )


def analyze_video_file(path: Path) -> str:
    return (
        f"已收到视频：{path.name}\n"
        f"大小：{path.stat().st_size} 字节\n\n"
        "视频已保存。当前会在异步处理时交给 video 路由的视频理解模型分析。"
    )


def analyze_text_file(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    preview = "\n".join(lines[:20])
    return f"已读取文本文件：{path.name}\n行数：{len(lines)}\n\n前 20 行预览：\n{preview}"


def analyze_csv_file(path: Path) -> str:
    rows: list[list[str]] = []
    with path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as file:
        reader = csv.reader(file)
        for index, row in enumerate(reader):
            rows.append(row)
            if index >= 20:
                break
    if not rows:
        return f"CSV 文件为空：{path.name}"
    header = rows[0]
    return (
        f"已读取 CSV：{path.name}\n"
        f"列数：{len(header)}\n"
        f"预览行数：{max(len(rows) - 1, 0)}\n\n"
        + table_preview(header, rows[1:8])
    )


def analyze_xlsx_file(path: Path) -> str:
    wb = load_workbook(path, read_only=True, data_only=True)
    parts: list[str] = [f"已读取 Excel：{path.name}", f"工作表数：{len(wb.sheetnames)}"]
    for sheet_name in wb.sheetnames[:3]:
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(max_row=8, values_only=True))
        if not rows:
            parts.append(f"\n{sheet_name}：空表")
            continue
        header = [str(value or "") for value in rows[0]]
        body = [[str(value or "") for value in row] for row in rows[1:]]
        parts.append(f"\n{sheet_name}：{ws.max_row} 行 x {ws.max_column} 列\n{table_preview(header, body)}")
    return "\n".join(parts)


def table_preview(header: list[str], rows: list[list[str]]) -> str:
    header = [str(value or "") for value in header[:6]]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * len(header)) + " |",
    ]
    for row in rows:
        values = [str(value or "") for value in row[: len(header)]]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)
