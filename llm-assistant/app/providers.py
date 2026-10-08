"""Провайдеры LLM: OpenAI-совместимый API, Anthropic, Ollama и офлайн-заглушка (mock)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import LLMConfig

log = logging.getLogger("llm-assistant.providers")


class ProviderError(RuntimeError):
    """Ошибка вызова LLM-провайдера."""


@dataclass
class LLMRequest:
    system: str
    user: str
    prompt_id: str
    temperature: float = 0.1
    max_tokens: int = 1200
    json_mode: bool = True
    metadata: dict[str, str] = field(default_factory=dict)


class _HttpPoster:
    cfg: LLMConfig

    def _timeout(self) -> float:
        return max(5.0, float(self.cfg.timeout_s))

    def _post(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        allow_fallback: bool = False,
    ) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=self._timeout()) as client:
                response = client.post(url, headers=headers, json=payload)
                if (
                    response.status_code >= 400
                    and allow_fallback
                    and "response_format" in payload
                ):
                    # Некоторые совместимые шлюзы не знают response_format — пробуем без него.
                    log.warning("повтор без response_format (HTTP %s)", response.status_code)
                    retry = {k: v for k, v in payload.items() if k != "response_format"}
                    response = client.post(url, headers=headers, json=retry)
                if response.status_code >= 400:
                    raise ProviderError(
                        f"LLM API вернул HTTP {response.status_code}: {response.text[:400]}"
                    )
                try:
                    return response.json()
                except json.JSONDecodeError as exc:
                    raise ProviderError(f"не-JSON ответ LLM API: {response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"нет соединения с LLM API: {type(exc).__name__}") from exc


class BaseProvider(_HttpPoster):
    name = "base"

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg

    def complete(self, request: LLMRequest) -> str:
        raise NotImplementedError


class OpenAICompatibleProvider(BaseProvider):
    """OpenAI / любой OpenAI-совместимый шлюз (внутренний GW, прокси и т.п.)."""

    name = "openai"

    def complete(self, request: LLMRequest) -> str:
        base = (self.cfg.base_url or "https://api.openai.com/v1").rstrip("/")
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"

        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}

        data = self._post(f"{base}/chat/completions", headers, payload, allow_fallback=True)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"неожиданный формат ответа chat/completions: {exc}") from exc


class AnthropicProvider(BaseProvider):
    name = "anthropic"

    def complete(self, request: LLMRequest) -> str:
        base = (self.cfg.base_url or "https://api.anthropic.com").rstrip("/")
        headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        if self.cfg.api_key:
            headers["x-api-key"] = self.cfg.api_key

        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "system": request.system,
            "messages": [{"role": "user", "content": request.user}],
        }
        data = self._post(f"{base}/v1/messages", headers, payload)
        try:
            blocks = data["content"]
            return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        except (KeyError, TypeError) as exc:
            raise ProviderError(f"неожиданный формат ответа messages: {exc}") from exc


class OllamaProvider(BaseProvider):
    name = "ollama"

    def complete(self, request: LLMRequest) -> str:
        base = (self.cfg.base_url or "http://localhost:11434").rstrip("/")
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "options": {"temperature": request.temperature, "num_predict": request.max_tokens},
        }
        if request.json_mode:
            payload["format"] = "json"

        data = self._post(f"{base}/api/chat", {}, payload)
        try:
            return data["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise ProviderError(f"неожиданный формат ответа ollama: {exc}") from exc


class MockProvider(BaseProvider):
    """Офлайн-режим для демо и автотестов: детерминированные ответы по ключевым словам.

    Позволяет показать связку «алерт → анализ → suggested» без обращения к API
    и не блокировать разработку промтов.
    """

    name = "mock"

    def complete(self, request: LLMRequest) -> str:
        handler = {
            "incident_analysis": self._incident,
            "alert_generation": self._alert,
            "remediation_proposal": self._remediation,
            "log_analysis": self._logs,
        }.get(request.prompt_id, self._generic)
        return json.dumps(handler(request), ensure_ascii=False, indent=2)

    # --- детекторы ---------------------------------------------------------
    @staticmethod
    def _severity(request: LLMRequest) -> str:
        # Смотрим только на данные (user): системный промт содержит схему вывода,
        # и её литералы не должны влиять на определение severity.
        blob = request.user.lower()
        for value in re.findall(r"severity[=:\s]+([a-z]+)", blob):
            if value in {"critical", "warning", "info"}:
                return value
            if value == "page":
                return "critical"
        if "critical" in blob or "status=firing" in blob:
            return "critical"
        return "warning"

    @staticmethod
    def _evidence_lines(user: str, limit: int = 5) -> list[str]:
        marks = ("error", "exception", "traceback", "timeout", "oom", "panic", "5xx", "failed")
        picked = [ln.strip() for ln in user.splitlines() if any(m in ln.lower() for m in marks)]
        return picked[:limit]

    def _incident(self, request: LLMRequest) -> dict[str, Any]:
        blob = request.user.lower()
        sev = self._severity(request)
        evidence = self._evidence_lines(request.user)
        hypotheses: list[dict[str, Any]] = []

        if "error_rate" in blob or "5.." in blob or "http_requests_total" in blob:
            hypotheses.append(
                {
                    "hypothesis": "Рост доли 5xx-ответов после последнего изменения/релиза",
                    "confidence": 0.6,
                    "evidence": evidence[:2] or ["метрика error_rate выросла в окне наблюдения"],
                }
            )
        if "oom" in blob or "killed" in blob or "crashloop" in blob:
            hypotheses.append(
                {
                    "hypothesis": "Переполнение памяти: под перезапускается по OOMKilled",
                    "confidence": 0.7,
                    "evidence": evidence[:2] or ["в логах есть признаки OOM/restart"],
                }
            )
        if "cpu" in blob or "pod" in blob or "chaos" in blob or "network" in blob:
            hypotheses.append(
                {
                    "hypothesis": "Деградация одной из реплик (эффект хаос-эксперимента или отказ ноды)",
                    "confidence": 0.5,
                    "evidence": evidence[:2] or ["готовность реплик изменилась"],
                }
            )
        if not hypotheses:
            hypotheses.append(
                {
                    "hypothesis": "Недостаточно данных для уверенной гипотезы",
                    "confidence": 0.3,
                    "evidence": ["нет явных маркеров в переданном контексте"],
                }
            )

        return {
            "summary": (
                f"Офлайн-анализ (mock): severity={sev}, гипотез={len(hypotheses)}, "
                "все действия в режиме suggested."
            ),
            "severity": sev,
            "slo_breach": sev == "critical",
            "impact": "Пользователи demo-app могут получать ошибки/задержки, расходуется error budget.",
            "root_cause_hypotheses": hypotheses,
            "next_checks": [
                "Сверить p95/error_rate с SLO на дашборде demo-app SLO",
                "Посмотреть последние 200 строк логов приложения в Loki",
                "Проверить историю релизов и изменений (kubectl rollout history)",
            ],
            "recommended_actions": [
                {
                    "action": "Проверить состояние реплик: kubectl -n demo get pods -o wide",
                    "risk": "low",
                    "requires_approval": False,
                },
                {
                    "action": "При подтверждённом падении реплик — откат релиза: "
                    "kubectl -n demo rollout undo deployment/demo-app (сначала --dry-run=server)",
                    "risk": "medium",
                    "requires_approval": True,
                },
            ],
            "runbook": {
                "id": "RB-001",
                "url": "https://wiki.example/sre/runbooks/high-error-rate",
                "steps": [
                    "Определить, локальна ли ошибка (по подам/ноде)",
                    "Проверить последние изменения",
                    "Откат/рестарт с dry-run и подтверждением дежурного",
                    "Верификация: error_rate < 1% в течение 5 минут",
                ],
            },
        }

    def _alert(self, request: LLMRequest) -> dict[str, Any]:
        service = "demo-app"
        found = re.findall(r"service[=:\s]+([a-z0-9-]+)", request.user)
        if found:
            service = found[0]
        expr = (
            'sum(rate(http_requests_total{status=~"5.."}[5m])) '
            "/ clamp_min(sum(rate(http_requests_total[5m])), 1e-9)"
        )
        for token in re.findall(r"[a-zA-Z_:][a-zA-Z0-9_:]*", request.user):
            if token.endswith(("_total", "_seconds", "_bucket")):
                expr = f"sum(rate({token}[5m]))"
                break
        camel = re.sub(r"[^A-Za-z0-9]", "", service.title())
        return {
            "name": f"{camel}HighErrorRate",
            "expr": expr,
            "for": "5m",
            "severity": self._severity(request),
            "summary": f"Высокая доля ошибок в {service}",
            "description": "Доля ошибок превышает порог SLO 5 минут подряд.",
            "runbook_url": "https://wiki.example/sre/runbooks/high-error-rate",
            "labels": {"service": service, "team": "sre"},
            "annotations": {"dashboard": "https://grafana.example/d/demo-app-slo"},
            "slo_impact": "consumes_error_budget",
        }

    def _remediation(self, request: LLMRequest) -> dict[str, Any]:
        kind = "deployment"
        name = "demo-app"
        ns = "demo"
        return {
            "problem": request.metadata.get("symptoms", "") or "Симптомы не указаны",
            "options": [
                {
                    "action": "Диагностика без изменений",
                    "command": f"kubectl -n {ns} get {kind}/{name} -o wide",
                    "risk": "low",
                    "reversible": True,
                    "requires_approval": False,
                    "estimated_mttr_s": 60,
                },
                {
                    "action": "Откат релиза (предпочтительно при ошибках после релиза)",
                    "command": f"kubectl -n {ns} rollout undo {kind}/{name}",
                    "risk": "medium",
                    "reversible": True,
                    "requires_approval": True,
                    "estimated_mttr_s": 180,
                },
                {
                    "action": "Перезапуск подов (рестарт)",
                    "command": f"kubectl -n {ns} rollout restart {kind}/{name}",
                    "risk": "medium",
                    "reversible": False,
                    "requires_approval": True,
                    "estimated_mttr_s": 120,
                },
                {
                    "action": "Масштабирование вверх",
                    "command": f"kubectl -n {ns} scale {kind}/{name} --replicas=4",
                    "risk": "high",
                    "reversible": True,
                    "requires_approval": True,
                    "estimated_mttr_s": 90,
                },
            ],
            "default_option": 1,
            "dry_run": f"kubectl -n {ns} rollout undo {kind}/{name} --dry-run=server -o yaml",
            "verification": [
                f"kubectl -n {ns} get pods -l app={name}  # все Ready",
                "promql: error_rate < 0.01 в течение 5 минут",
            ],
            "rollback": f"kubectl -n {ns} rollout undo {kind}/{name}",
        }

    def _logs(self, request: LLMRequest) -> dict[str, Any]:
        evidence = self._evidence_lines(request.user, limit=8)
        return {
            "pattern": "Регулярные error-сообщения в приложении (mock-классификация)",
            "possible_incident": bool(evidence),
            "entities": {"service": "demo-app", "level": "error"},
            "anomalies": [
                {"line": line[:300], "reason": "содержит маркер ошибки"} for line in evidence[:3]
            ],
            "suggested_alert": {
                "name": "DemoAppLogErrorSpike",
                "expr": 'sum(rate({namespace="demo"} |= "error" [5m])) > 5',
                "severity": "warning",
                "for": "5m",
                "runbook_url": "https://wiki.example/sre/runbooks/log-error-spike",
            },
            "evidence_lines": evidence,
        }

    def _generic(self, request: LLMRequest) -> dict[str, Any]:
        return {
            "summary": "Офлайн-режим: подключите реальный LLM (llm.provider / LLM_API_KEY).",
            "prompt_id": request.prompt_id,
            "mode": "suggested",
        }


_PROVIDERS: dict[str, type[BaseProvider]] = {
    "openai": OpenAICompatibleProvider,
    "anthropic": AnthropicProvider,
    "ollama": OllamaProvider,
    "mock": MockProvider,
}


def build_provider(cfg: LLMConfig) -> BaseProvider:
    key = (cfg.provider or "mock").strip().lower()
    try:
        return _PROVIDERS[key](cfg)
    except KeyError:
        raise ProviderError(
            f"неизвестный провайдер '{cfg.provider}'. Доступны: {', '.join(sorted(_PROVIDERS))}"
        ) from None
