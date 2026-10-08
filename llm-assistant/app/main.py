"""HTTP-слой LLM-ассистента (FastAPI)."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

from . import __version__
from .config import AppConfig, load_config
from .models import (
    AlertProposalRequest,
    AlertmanagerWebhook,
    AnalyzeRequest,
    AnalysisResult,
    RemediationRequest,
)
from .prompts import MissingVariablesError
from .providers import ProviderError
from .service import AssistantService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("llm-assistant.api")


def create_app(
    cfg: Optional[AppConfig] = None, service: Optional[AssistantService] = None
) -> FastAPI:
    cfg = cfg or load_config()
    service = service or AssistantService(cfg)

    app = FastAPI(
        title="LLM-ассистент платформы наблюдаемости",
        description=(
            "Пункт 9 (SRE-платформа): LLM-анализ логов/метрик, генерация алертов "
            "и предложение действий по инцидентам. Всё в режиме suggested — "
            "автоприменение выполняет отдельный закрытый контур с dry-run и подтверждением."
        ),
        version=__version__,
    )

    @app.exception_handler(ProviderError)
    async def _provider_error(_request, exc: ProviderError):  # type: ignore[no-untyped-def]
        from fastapi.responses import JSONResponse

        log.error("LLM provider error: %s", exc)
        return JSONResponse(status_code=502, content={"detail": f"LLM недоступен: {exc}"})

    @app.exception_handler(KeyError)
    async def _key_error(_request, exc: KeyError):  # type: ignore[no-untyped-def]
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(MissingVariablesError)
    async def _missing(_request, exc: MissingVariablesError):  # type: ignore[no-untyped-def]
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.get("/healthz")
    def healthz() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "provider": service.provider.name,
            "model": cfg.llm.model,
            "prompts": service.registry.ids(),
        }

    @app.get("/prompts")
    def list_prompts() -> list[dict]:
        return service.registry.metadata()

    @app.get("/prompts/{prompt_id}")
    def get_prompt(prompt_id: str) -> dict:
        template = service.registry.get(prompt_id)
        return {
            "id": template.id,
            "version": template.version,
            "description": template.description,
            "variables": template.variables,
            "system": template.render_system(),
            "user_template": template.user,
            "output_schema": template.output_schema,
        }

    @app.post("/webhook/alertmanager", response_model=AnalysisResult)
    def alertmanager_webhook(payload: AlertmanagerWebhook) -> AnalysisResult:
        """Вебхук Alertmanager: алерты → контекст (Prometheus/Loki) → LLM-разбор."""
        if not payload.alerts:
            raise HTTPException(status_code=400, detail="в вебхуке нет алертов")
        return service.analyze_incident(payload)

    @app.post("/analyze", response_model=AnalysisResult)
    def analyze(request: AnalyzeRequest) -> AnalysisResult:
        return service.analyze(request)

    @app.post("/alerts/propose", response_model=AnalysisResult)
    def propose_alert(request: AlertProposalRequest) -> AnalysisResult:
        """LLM предлагает правило Prometheus-алерта (само правило применяется через GitOps)."""
        return service.propose_alert(request)

    @app.post("/remediation/propose", response_model=AnalysisResult)
    def propose_remediation(request: RemediationRequest) -> AnalysisResult:
        """LLM предлагает действия по восстановлению (mode=suggested, ничего не применяется)."""
        return service.propose_remediation(request)

    @app.post("/logs/analyze", response_model=AnalysisResult)
    def analyze_logs(payload: dict) -> AnalysisResult:
        logs = payload.get("logs", "")
        if not isinstance(logs, str) or not logs.strip():
            raise HTTPException(status_code=422, detail="ожидается поле logs (string)")
        service_name = str(payload.get("service", "demo-app"))
        return service.analyze_logs(logs, service=service_name)

    @app.get("/audit")
    def audit(limit: int = Query(default=50, ge=1, le=500)) -> list[dict]:
        """Audit log обращений к LLM: хэши входа/выхода, latency, статус."""
        return service.audit.read(limit=limit)

    @app.post("/demo/incident", response_model=AnalysisResult)
    def demo_incident(
        use_sources: bool = Query(default=False),
    ) -> AnalysisResult:
        """Демо-инцидент без внешнего Alertmanager (для видео и смоук-тестов)."""
        payload = AlertmanagerWebhook(
            groupKey="{alertname=\"HighErrorRate\", service=\"demo-app\"}",
            status="firing",
            receiver="llm-assistant",
            commonLabels={"service": "demo-app", "severity": "critical"},
            externalURL="https://alertmanager.example/#/alerts",
            alerts=[
                _demo_alert(
                    name="HighErrorRate",
                    severity="critical",
                    summary="Доля ошибок 5xx 7.4% выше порога 1% (SLO demo-app)",
                    description="error_rate breach for 10m",
                    service="demo-app",
                ),
                _demo_alert(
                    name="PodNotReady",
                    severity="warning",
                    summary="Под demo-app-7d9f4c-x2x8l не готов 5m",
                    description="readiness probe failed",
                    service="demo-app",
                ),
            ],
        )
        return service.analyze_incident(payload)

    return app


def _demo_alert(name: str, severity: str, summary: str, description: str, service: str):
    from .models import Alert

    return Alert(
        status="firing",
        labels={
            "alertname": name,
            "severity": severity,
            "service": service,
            "namespace": "demo",
            "pod": "demo-app-7d9f4c-x2x8l",
        },
        annotations={"summary": summary, "description": description, "runbook_url": "RB-001"},
        startsAt="2026-10-07T10:12:00Z",
        generatorURL="https://prometheus.example/graph",
        fingerprint="deadbeef",
    )


app = create_app()
