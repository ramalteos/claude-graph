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
    pr_title: str = ""  # commit-style, follows the repo's commitlint; empty = use the request
    pr_body: str = ""  # short, with a checklist; empty = use the spec


class QaReport(_Report):
    passed: bool
    report: str


class ReviewReport(_Report):
    verdict: Literal["approve", "changes", "redesign"]
    notes: str


_OPEN = re.compile(r"```json\b", re.IGNORECASE)
_CLOSE = re.compile(r"```")


def parse_report[T: _Report](text: str | None, model: type[T]) -> T | None:
    """The last fenced json block in `text` as `model`, or None.

    Starts at the last ```json fence; closing fences are tried from the end backwards, so
    fences quoted inside strings or a stray fence after the block do not lose the report.
    """
    if not text:
        return None
    opens = list(_OPEN.finditer(text))
    if not opens:
        return None
    rest = text[opens[-1].end() :]
    for close in reversed(list(_CLOSE.finditer(rest))):
        try:
            # strict=False: models leave raw newlines inside markdown strings
            return model.model_validate(json.loads(rest[: close.start()].strip(), strict=False))
        except (json.JSONDecodeError, ValidationError):
            continue
    return None
