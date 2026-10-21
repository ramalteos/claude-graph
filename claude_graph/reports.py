"""Role reports: the one fenced ```json block each role ends its final message with."""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError


class _Report(BaseModel):
    # strict: a QA box that writes `"passed": "yes"` has not passed anything.
    model_config = ConfigDict(strict=True, extra="ignore")


class PmReport(_Report):
    spec: str
    acceptance_criteria: list[str]


class ArchitectReport(_Report):
    plan: str


class CoderReport(_Report):
    summary: str


class QaReport(_Report):
    passed: bool
    report: str


class ReviewReport(_Report):
    verdict: Literal["approve", "changes", "redesign"]
    notes: str


_FENCE = re.compile(r"```json[ \t]*\n(.*?)\n[ \t]*```", re.DOTALL | re.IGNORECASE)


def parse_report[T: _Report](text: str | None, model: type[T]) -> T | None:
    """The last fenced json block in `text` as `model`, or None when there is no valid one."""
    if not text:
        return None
    blocks = _FENCE.findall(text)
    if not blocks:
        return None
    try:
        return model.model_validate(json.loads(blocks[-1]))
    except (json.JSONDecodeError, ValidationError):
        return None
