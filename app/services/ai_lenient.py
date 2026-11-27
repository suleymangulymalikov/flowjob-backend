"""Keep a usable AI reply when the model slightly overshoots a length limit.

Structured outputs make the model follow the JSON *shape*, but OpenAI does not enforce `maxLength` / `maxItems`
for us: one reason a few characters too long, or a 13th suggestion, used to fail the whole reply (HTTP 502). Models
built on `LenientModel` fit the reply to the limits before validation instead:

- a list longer than its limit keeps its first items (the model lists the most important first);
- an over-long text is shortened at a word boundary with "…", EXCEPT fields named in `_blank_if_too_long`: those
  carry content the user would apply (a rewritten sentence), where a cut-off sentence must never reach the CV, so
  they are emptied instead and the backend's checks then drop just that one item.

Quotes that are shortened stay a prefix of the original quote, so the verbatim checks still work on them.
"""
from typing import Any, ClassVar

from annotated_types import MaxLen
from pydantic import BaseModel, model_validator


def _max_len(field) -> int | None:
    return next((m.max_length for m in field.metadata if isinstance(m, MaxLen)), None)


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if ' ' in cut[limit // 2:]:
        cut = cut[: cut.rindex(' ')]
    return cut.rstrip(' ,;:.-') + '…'


class LenientModel(BaseModel):
    _blank_if_too_long: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode='before')
    @classmethod
    def _fit_limits(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for name, field in cls.model_fields.items():
            limit = _max_len(field)
            value = data.get(name)
            if limit is None or value is None:
                continue
            if isinstance(value, list) and len(value) > limit:
                data[name] = value[:limit]
            elif isinstance(value, str) and len(value) > limit:
                data[name] = None if name in cls._blank_if_too_long else _shorten(value, limit)
        return data
