from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


AnswerStatus = Literal["success", "error", "denied"]


@dataclass(frozen=True)
class AnswerResult:
    content: str
    status: AnswerStatus = "success"

