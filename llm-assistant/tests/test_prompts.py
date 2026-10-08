"""Проверки промтов: загрузка, обязательные поля, рендер без «сырых» плейсхолдеров."""

import pytest

from app.config import AppConfig, AuditConfig, LLMConfig, NotifyConfig, SourcesConfig
from app.prompts import MissingVariablesError, PromptRegistry

EXPECTED_PROMPTS = {"incident_analysis", "alert_generation", "remediation_proposal", "log_analysis"}


@pytest.fixture
def registry() -> PromptRegistry:
    cfg = AppConfig(llm=LLMConfig(provider="mock"), audit=AuditConfig(path="./data/audit.jsonl"))
    return PromptRegistry.from_dir(cfg.prompts_path)


def test_all_prompts_loaded(registry: PromptRegistry):
    assert set(registry.ids()) == EXPECTED_PROMPTS


def test_metadata_has_versions_and_variables(registry: PromptRegistry):
    for item in registry.metadata():
        assert item["id"] in EXPECTED_PROMPTS
        assert item["version"] >= 1
        assert item["description"]
        assert item["placeholders"], f"у {item['id']} нет переменных"


def test_render_leaves_no_placeholders(registry: PromptRegistry):
    for prompt_id in registry.ids():
        template = registry.get(prompt_id)
        values = {name: f"<{name}>" for name in template.placeholders()}
        system = template.render_system()
        user = template.render_user(values)

        assert "{{" not in system
        assert "{{" not in user
        assert "Формат ответа" in system
        assert values["lookback"] in user


def test_strict_render_reports_missing(registry: PromptRegistry):
    template = registry.get("incident_analysis")
    with pytest.raises(MissingVariablesError) as exc:
        template.render_user({}, strict=True)
    assert "alerts" in exc.value.missing


def test_non_strict_render_fills_no_data_marker(registry: PromptRegistry):
    template = registry.get("incident_analysis")
    rendered = template.render_user({"alerts": "нет алертов"}, strict=False)
    assert "нет алертов" in rendered
    assert "(нет данных:" in rendered
