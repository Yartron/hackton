"""Интеграционные проверки сервиса и HTTP API (источники отключены, провайдер mock)."""

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig, AuditConfig, LLMConfig, NotifyConfig, SourcesConfig
from app.main import create_app
from app.models import Alert, AlertmanagerWebhook, AnalyzeRequest, RemediationRequest
from app.service import AssistantService


@pytest.fixture
def cfg(tmp_path) -> AppConfig:
    return AppConfig(
        llm=LLMConfig(provider="mock", model="mock-1"),
        sources=SourcesConfig(enabled=False),
        notify=NotifyConfig(enabled=False),
        audit=AuditConfig(path=str(tmp_path / "audit.jsonl")),
    )


@pytest.fixture
def service(cfg: AppConfig) -> AssistantService:
    return AssistantService(cfg)


@pytest.fixture
def client(cfg: AppConfig, service: AssistantService) -> TestClient:
    return TestClient(create_app(cfg, service))


def _webhook() -> AlertmanagerWebhook:
    return AlertmanagerWebhook(
        groupKey='group:{alertname="HighErrorRate"}',
        status="firing",
        receiver="llm-assistant",
        alerts=[
            Alert(
                status="firing",
                labels={"alertname": "HighErrorRate", "severity": "critical", "service": "demo-app"},
                annotations={"summary": "error_rate 7.4% > 1%", "runbook_url": "RB-001"},
                startsAt="2026-10-07T10:12:00Z",
            )
        ],
    )


def test_healthz(client: TestClient):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["provider"] == "mock"
    assert "incident_analysis" in body["prompts"]


def test_list_prompts(client: TestClient):
    body = client.get("/prompts").json()
    assert {item["id"] for item in body} == {
        "incident_analysis",
        "alert_generation",
        "remediation_proposal",
        "log_analysis",
    }


def test_webhook_returns_analysis_and_writes_audit(client: TestClient, cfg: AppConfig):
    response = client.post("/webhook/alertmanager", json=_webhook().model_dump())
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["mode"] == "suggested"
    assert body["prompt_id"] == "incident_analysis"
    assert body["parsed"]["severity"] == "critical"
    assert body["parsed"]["recommended_actions"]

    records = client.get("/audit").json()
    assert records, "audit log пуст"
    assert records[0]["prompt_id"] == "incident_analysis"
    assert records[0]["status"] in {"ok", "unparsed"}
    assert records[0]["input_sha256"]


def test_webhook_requires_alerts(client: TestClient):
    response = client.post("/webhook/alertmanager", json={"alerts": []})
    assert response.status_code == 400


def test_unknown_prompt_is_404(client: TestClient):
    response = client.post("/analyze", json={"prompt_id": "nope"})
    assert response.status_code == 404


def test_propose_remediation_is_suggested_only(client: TestClient):
    response = client.post(
        "/remediation/propose",
        json=RemediationRequest(
            symptoms="5xx растёт после релиза", logs="ERROR upstream timeout"
        ).model_dump(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "suggested"
    assert body["parsed"]["dry_run"]
    assert body["parsed"]["default_option"] is not None


def test_propose_alert(client: TestClient):
    response = client.post(
        "/alerts/propose",
        json={"signal": "рост ошибок 5xx", "metric": "http_requests_total", "logs": "ERROR x"},
    )
    assert response.status_code == 200, response.text
    parsed = response.json()["parsed"]
    assert parsed["expr"]
    assert parsed["runbook_url"]


def test_analyze_with_logs(client: TestClient):
    request = AnalyzeRequest(
        prompt_id="log_analysis",
        logs="ERROR boom\nINFO ok\nERROR boom again",
    )
    response = client.post("/analyze", json=request.model_dump(exclude_none=True))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["prompt_id"] == "log_analysis"
    assert body["parsed"]["possible_incident"] is True
    assert body["parsed"]["evidence_lines"]
