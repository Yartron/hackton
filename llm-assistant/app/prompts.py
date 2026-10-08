"""Загрузка и рендер промтов (промты живут в отдельных YAML-файлах — их правит SRE, а не кодер)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml
from pydantic import BaseModel, Field

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


class MissingVariablesError(ValueError):
    def __init__(self, prompt_id: str, missing: Iterable[str]):
        self.prompt_id = prompt_id
        self.missing = sorted(set(missing))
        super().__init__(f"промт '{prompt_id}' ждёт переменные: {', '.join(self.missing)}")


class PromptTemplate(BaseModel):
    id: str
    version: int = 1
    description: str = ""
    variables: list[str] = Field(default_factory=list)
    system: str
    user: str
    output_schema: str = ""

    def placeholders(self) -> set[str]:
        """Все {{ var }} и в system, и в user (включая схему вывода)."""
        blob = "\n".join([self.system, self.user, self.output_schema])
        return set(_PLACEHOLDER_RE.findall(blob))

    def missing_variables(self, values: Mapping[str, Any]) -> list[str]:
        known = set(values)
        return sorted(self.placeholders() - known)

    def render_system(self) -> str:
        parts = [f"[prompt:id={self.id} version={self.version}]", self.system.strip()]
        if self.output_schema.strip():
            parts.append(
                "Формат ответа — строго один JSON-объект без markdown и пояснений:\n"
                + self.output_schema.strip()
            )
        return "\n\n".join(parts)

    def render_user(self, values: Mapping[str, Any], strict: bool = True) -> str:
        missing = self.missing_variables(values)
        if missing and strict:
            raise MissingVariablesError(self.id, missing)

        def _sub(match: re.Match) -> str:
            key = match.group(1)
            if key not in values:
                return f"(нет данных: {key})"
            return str(values[key])

        rendered = _PLACEHOLDER_RE.sub(_sub, self.user)
        return f"[incident context]\n{rendered}".strip()


class PromptRegistry:
    def __init__(self, templates: Mapping[str, PromptTemplate]):
        self._templates = dict(templates)

    @classmethod
    def from_dir(cls, directory: Path | str) -> "PromptRegistry":
        directory = Path(directory)
        if not directory.exists():
            raise FileNotFoundError(f"каталог промтов не найден: {directory}")

        templates: dict[str, PromptTemplate] = {}
        for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                raise ValueError(f"{path.name}: ожидается YAML-объект")
            template = PromptTemplate.model_validate(data)
            if template.id in templates:
                raise ValueError(f"дубликат id промта: {template.id} ({path.name})")
            templates[template.id] = template

        if not templates:
            raise ValueError(f"в {directory} не найдено ни одного промта (*.yaml)")
        return cls(templates)

    def get(self, prompt_id: str) -> PromptTemplate:
        try:
            return self._templates[prompt_id]
        except KeyError:
            raise KeyError(
                f"неизвестный промт '{prompt_id}'. Доступны: {', '.join(sorted(self._templates))}"
            ) from None

    def ids(self) -> list[str]:
        return sorted(self._templates)

    def metadata(self) -> list[dict[str, Any]]:
        return [
            {
                "id": t.id,
                "version": t.version,
                "description": t.description.strip(),
                "variables": t.variables,
                "placeholders": sorted(t.placeholders()),
            }
            for t in sorted(self._templates.values(), key=lambda t: t.id)
        ]
