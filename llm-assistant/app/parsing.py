"""Разбор ответов LLM: извлечение JSON из свободного текста модели."""

from __future__ import annotations

import json
import re
from typing import Any, Optional

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _try_load(text: str) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def parse_json_response(text: str) -> Optional[dict[str, Any]]:
    """Возвращает dict из ответа модели либо None, если JSON собрать не удалось.

    Поддерживает: чистый JSON, JSON в ```json-фенсе, JSON внутри текста.
    """
    if not text:
        return None

    stripped = text.strip()
    data = _try_load(stripped)
    if data is not None:
        return data

    for match in _FENCE_RE.finditer(stripped):
        data = _try_load(match.group(1).strip())
        if data is not None:
            return data

    start = stripped.find("{")
    end = stripped.rfind("}")
    if 0 <= start < end:
        data = _try_load(stripped[start : end + 1])
        if data is not None:
            return data
    return None
