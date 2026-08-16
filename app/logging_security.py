from __future__ import annotations

import logging
import re


_QUERY_SECRET = re.compile(
    r"(?i)(access_key|ticket|token|app_secret|appSecret)=([^&\s]+)"
)
_JSON_SECRET = re.compile(
    r'(?i)(["\'](?:access_key|ticket|token|app_secret|appSecret)["\']\s*:\s*["\'])([^"\']+)'
)
_OPEN_ID = re.compile(r"\bou_[A-Za-z0-9_-]+\b")


def redact_sensitive_text(value: object) -> str:
    text = str(value)
    text = _QUERY_SECRET.sub(r"\1=***", text)
    text = _JSON_SECRET.sub(r"\1***", text)
    return _OPEN_ID.sub(_mask_open_id, text)


def _mask_open_id(match: re.Match[str]) -> str:
    value = match.group(0)
    if len(value) <= 10:
        return "ou_***"
    return f"{value[:6]}***{value[-4:]}"


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_sensitive_text(record.getMessage())
            record.args = ()
        except Exception:  # pragma: no cover - logging must never break application flow
            pass
        return True


def install_sensitive_log_filter() -> None:
    root = logging.getLogger()
    if not any(isinstance(item, SensitiveDataFilter) for item in root.filters):
        root.addFilter(SensitiveDataFilter())
    for handler in root.handlers:
        if not any(isinstance(item, SensitiveDataFilter) for item in handler.filters):
            handler.addFilter(SensitiveDataFilter())
