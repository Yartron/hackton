"""Проверки провайдеров: mock детерминирован и отвечает валидным JSON по каждому промту."""

import json

import pytest

from app.config import LLMConfig
from app.parsing import parse_json_response
from app.providers import (
    LLMRequest,
    MockProvider,
    ProviderError,
    build_provider,
)

SAMPLE_USER = """
service: demo-app
severity=critical
error_rate 0.07 > порога 0.01
2026-10-07T10:12:01Z [demo-app] ERROR upstream timeout
2026-10-07T10:12:02Z [demo-app] Exception: connection refused
"""


def _request(prompt_id: str) -> LLMRequest:
    return LLMRequest(
        system=f"[prompt:id={prompt_id}]",
        user=SAMPLE_USER,
        prompt_id=prompt_id,
        metadata={"symptoms": "высокая ошибка 5xx"},
    )


@pytest.mark.parametrize(
    "prompt_id", ["incident_analysis", "alert_generation", "remediation_proposal", "log_analysis"]
)
def test_mock_returns_valid_json(prompt_id: str):
    provider = MockProvider(LLMConfig(provider="mock"))
    raw = provider.complete(_request(prompt_id))
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    assert parsed, "пустой ответ"


def test_mock_detects_severity_from_data_not_from_schema():
    provider = MockProvider(LLMConfig(provider="mock"))
    parsed = parse_json_response(provider.complete(_request("incident_analysis")))
    assert parsed is not None
    assert parsed["severity"] == "critical"
    assert parsed["root_cause_hypotheses"]


def test_mock_remediation_marks_changes_for_approval():
    provider = MockProvider(LLMConfig(provider="mock"))
    parsed = parse_json_response(provider.complete(_request("remediation_proposal")))
    assert parsed is not None
    assert parsed["dry_run"].endswith("-o yaml")
    assert len(parsed["options"]) >= 2
    changing = [opt for opt in parsed["options"] if opt["risk"] in {"medium", "high"}]
    assert all(opt["requires_approval"] for opt in changing)


def test_unknown_provider_raises():
    with pytest.raises(ProviderError):
        build_provider(LLMConfig(provider="nope"))


def test_build_provider_mock():
    provider = build_provider(LLMConfig(provider="mock"))
    assert provider.name == "mock"
