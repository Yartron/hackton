"""Модели данных API (Pydantic v2)."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class Alert(BaseModel):
    """Алерт в формате вебхука Alertmanager."""

    status: str = "firing"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: Optional[str] = None
    endsAt: Optional[str] = None
    generatorURL: Optional[str] = None
    fingerprint: Optional[str] = None

    @property
    def alertname(self) -> str:
        return self.labels.get("alertname", "unknown")

    @property
    def severity(self) -> str:
        return self.labels.get("severity", "none")


class AlertmanagerWebhook(BaseModel):
    """Тело вебхука Alertmanager (v4)."""

    version: int = 4
    groupKey: str = ""
    truncatedAlerts: int = 0
    status: str = "firing"
    receiver: str = ""
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    externalURL: str = ""
    alerts: list[Alert] = Field(default_factory=list)


class AnalyzeRequest(BaseModel):
    """Универсальный вызов промта (используется и для отладки, и для CLI)."""

    prompt_id: str = "incident_analysis"
    subject: str = ""
    logs: Optional[str] = None
    context: dict[str, str] = Field(default_factory=dict)
    use_sources: bool = False


class AlertProposalRequest(BaseModel):
    """Просьба предложить правило алерта на основе сигнала/логов."""

    signal: str = ""
    metric: Optional[str] = None
    service: Optional[str] = None
    logs: Optional[str] = None
    severity: str = "warning"


class RemediationRequest(BaseModel):
    """Просьба предложить способ восстановления. Режим всегда suggested (без применения)."""

    target_kind: str = "Deployment"
    target_name: str = "demo-app"
    namespace: str = "demo"
    symptoms: str = ""
    logs: Optional[str] = None
    alert_labels: dict[str, str] = Field(default_factory=dict)


class AnalysisResult(BaseModel):
    """Результат работы ассистента. mode=suggested: LLM только предлагает, ничего не применяет."""

    request_id: str
    prompt_id: str
    prompt_version: int
    provider: str
    model: str
    latency_ms: int
    mode: str = "suggested"
    parsed: Optional[dict[str, Any]] = None
    raw: str = ""
    warnings: list[str] = Field(default_factory=list)
    created_at: str = ""
