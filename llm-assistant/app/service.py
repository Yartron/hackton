"""Оркестрация: сбор контекста → рендер промта → вызов LLM → разбор → audit → уведомление."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

from . import notify
from .audit import AuditLog
from .config import AppConfig
from .enrich import loki_tail, now_iso, prometheus_snapshot
from .models import (
    AlertProposalRequest,
    AlertmanagerWebhook,
    AnalyzeRequest,
    AnalysisResult,
    RemediationRequest,
)
from .parsing import parse_json_response
from .prompts import PromptRegistry
from .providers import BaseProvider, LLMRequest, ProviderError, build_provider

log = logging.getLogger("llm-assistant.service")

NO_DATA = "(нет данных: {name})"


class AssistantService:
    def __init__(
        self,
        cfg: AppConfig,
        provider: Optional[BaseProvider] = None,
        registry: Optional[PromptRegistry] = None,
        audit: Optional[AuditLog] = None,
    ):
        self.cfg = cfg
        self.provider = provider or build_provider(cfg.llm)
        self.registry = registry or PromptRegistry.from_dir(cfg.prompts_path)
        self.audit = audit or AuditLog(cfg.audit.path)

    # --- общая часть -------------------------------------------------------
    def _defaults(self) -> dict[str, str]:
        return {
            "service": "demo-app",
            "namespace": "demo",
            "lookback": self.cfg.sources.lookback,
            "slo": self.cfg.sources.slo_text,
            "topology": self.cfg.sources.topology_text,
            "metrics": "(метрики не запрашивались)",
            "logs": "(логи не запрашивались)",
        }

    def _run(
        self,
        prompt_id: str,
        variables: Optional[dict[str, str]] = None,
        *,
        use_sources: bool = False,
        logs: Optional[str] = None,
        extra_audit: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, str]] = None,
    ) -> AnalysisResult:
        template = self.registry.get(prompt_id)
        merged: dict[str, str] = {**self._defaults(), **(variables or {})}
        warnings: list[str] = []

        if use_sources:
            merged["metrics"] = prometheus_snapshot(self.cfg.sources)
            merged["logs"] = logs.strip() if logs else loki_tail(self.cfg.sources)
        elif logs:
            merged["logs"] = logs.strip()

        missing = template.missing_variables(merged)
        for name in missing:
            merged[name] = NO_DATA.format(name=name)
        if missing:
            warnings.append("переменные без данных: " + ", ".join(sorted(missing)))

        system = template.render_system()
        user = template.render_user(merged, strict=False)

        request = LLMRequest(
            system=system,
            user=user,
            prompt_id=template.id,
            temperature=self.cfg.llm.temperature,
            max_tokens=self.cfg.llm.max_tokens,
            json_mode=self.cfg.llm.json_mode,
            metadata={"prompt_version": str(template.version), **(metadata or {})},
        )

        request_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        try:
            raw = self.provider.complete(request)
        except ProviderError as exc:
            self.audit.record(
                request_id=request_id,
                prompt_id=template.id,
                prompt_version=template.version,
                provider=self.provider.name,
                model=self.cfg.llm.model,
                system=system,
                user=user,
                raw="",
                latency_ms=int((time.perf_counter() - started) * 1000),
                status="error",
                warnings=[str(exc)],
                extra=extra_audit,
            )
            raise
        latency_ms = int((time.perf_counter() - started) * 1000)

        parsed = parse_json_response(raw)
        if parsed is None:
            warnings.append("ответ модели не распознан как JSON")

        result = AnalysisResult(
            request_id=request_id,
            prompt_id=template.id,
            prompt_version=template.version,
            provider=self.provider.name,
            model=self.cfg.llm.model,
            latency_ms=latency_ms,
            mode="suggested",
            parsed=parsed,
            raw=raw,
            warnings=warnings,
            created_at=now_iso(),
        )

        self.audit.record(
            request_id=request_id,
            prompt_id=template.id,
            prompt_version=template.version,
            provider=self.provider.name,
            model=self.cfg.llm.model,
            system=system,
            user=user,
            raw=raw,
            latency_ms=latency_ms,
            status="ok" if parsed is not None else "unparsed",
            parsed=parsed,
            warnings=warnings,
            extra=extra_audit,
        )

        notification = notify.maybe_notify(
            self.cfg.notify.enabled, self.cfg.notify.webhook_url, result
        )
        if notification.get("notified"):
            log.info("уведомление отправлено (request_id=%s)", request_id)
        return result

    # --- публичные сценарии ------------------------------------------------
    def analyze_incident(self, webhook: AlertmanagerWebhook) -> AnalysisResult:
        """Сценарий из п.9: алерт Alertmanager → LLM-разбор инцидента."""
        incident_id = webhook.groupKey or f"incident-{uuid.uuid4().hex[:8]}"
        variables = {
            "incident_id": incident_id,
            "status": webhook.status,
            "alerts": _format_alerts(webhook),
        }
        return self._run(
            "incident_analysis",
            variables,
            use_sources=self.cfg.sources.enabled,
            extra_audit={
                "incident_id": incident_id,
                "alerts_total": len(webhook.alerts),
                "trigger": "alertmanager_webhook",
            },
        )

    def analyze(self, request: AnalyzeRequest) -> AnalysisResult:
        variables = {"subject": request.subject, **request.context}
        return self._run(
            request.prompt_id,
            variables,
            use_sources=request.use_sources,
            logs=request.logs,
            extra_audit={"trigger": "api", "subject": request.subject},
        )

    def propose_alert(self, request: AlertProposalRequest) -> AnalysisResult:
        """Генерация правила алерта на основе сигнала/логов."""
        variables = {
            "signal": request.signal or "рост ошибок в сервисе",
            "metric": request.metric or "http_requests_total",
            "service": request.service or "demo-app",
            "severity": request.severity,
        }
        return self._run(
            "alert_generation",
            variables,
            logs=request.logs,
            extra_audit={"trigger": "alert_proposal", "service": variables["service"]},
        )

    def propose_remediation(self, request: RemediationRequest) -> AnalysisResult:
        """Подсказка способа восстановления. Ничего не применяется: только suggested."""
        variables = {
            "target_kind": request.target_kind,
            "target_name": request.target_name,
            "namespace": request.namespace,
            "symptoms": request.symptoms or "не описаны",
            "alert_labels": ", ".join(f"{k}={v}" for k, v in sorted(request.alert_labels.items()))
            or "—",
        }
        return self._run(
            "remediation_proposal",
            variables,
            logs=request.logs,
            metadata={"symptoms": variables["symptoms"]},
            extra_audit={
                "trigger": "remediation_proposal",
                "target": f"{request.namespace}/{request.target_kind}/{request.target_name}",
            },
        )

    def analyze_logs(self, logs: str, service: str = "demo-app") -> AnalysisResult:
        return self._run(
            "log_analysis",
            {"service": service},
            logs=logs,
            extra_audit={"trigger": "log_analysis", "service": service},
        )


def _format_alerts(webhook: AlertmanagerWebhook) -> str:
    if not webhook.alerts:
        return "(алертов нет)"
    lines: list[str] = []
    for alert in webhook.alerts:
        labels = ", ".join(f"{k}={v}" for k, v in sorted(alert.labels.items()))
        annotations = "; ".join(f"{k}={v}" for k, v in sorted(alert.annotations.items()))
        lines.append(
            f"- {alert.alertname} | status={alert.status} | severity={alert.severity} "
            f"| startsAt={alert.startsAt or '?'}\n"
            f"  labels: {labels}\n"
            f"  annotations: {annotations}"
        )
    return "\n".join(lines)
