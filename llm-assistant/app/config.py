"""Конфигурация сервиса: YAML-файл + переопределения через переменные окружения."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent.parent


def _default_queries() -> dict[str, str]:
    return {
        "error_rate": 'sum(rate(http_requests_total{status=~"5.."}[5m])) '
        "/ clamp_min(sum(rate(http_requests_total[5m])), 1e-9)",
        "latency_p95": "histogram_quantile(0.95, sum by (le) "
        "(rate(http_request_duration_seconds_bucket[5m])))",
        "availability": 'sum(rate(http_requests_total{status!~"5.."}[5m])) '
        "/ clamp_min(sum(rate(http_requests_total[5m])), 1e-9)",
        "firing_alerts": "sum by (alertname, severity) (ALERTS == 1)",
    }


class LLMConfig(BaseModel):
    provider: str = "mock"  # openai | anthropic | ollama | mock
    model: str = "gpt-4o-mini"
    base_url: str = ""
    api_key: Optional[str] = None
    temperature: float = 0.1
    max_tokens: int = 1200
    timeout_s: float = 45.0
    json_mode: bool = True
    max_log_chars: int = 24000


class SourcesConfig(BaseModel):
    enabled: bool = True
    prometheus_url: str = "http://prometheus.monitoring:9090"
    loki_url: str = "http://loki.monitoring:3100"
    lookback: str = "30m"
    step: str = "2m"
    loki_query: str = '{namespace="demo"} |~ "(?i)(error|exception|traceback|timeout|oom|panic)"'
    loki_limit: int = 300
    slo_text: str = (
        "demo-app: доступность >= 99.5% за 30 дней; latency p95 <= 300 мс; error rate <= 1%."
    )
    topology_text: str = (
        "demo-app (ns demo): Deployment, 2 реплики; Service/Ingress; "
        "зависимости: postgres, redis."
    )
    queries: dict[str, str] = Field(default_factory=_default_queries)


class NotifyConfig(BaseModel):
    enabled: bool = False
    webhook_url: str = ""


class AuditConfig(BaseModel):
    path: str = "./data/audit.jsonl"


class AppConfig(BaseModel):
    llm: LLMConfig = LLMConfig()
    sources: SourcesConfig = SourcesConfig()
    notify: NotifyConfig = NotifyConfig()
    audit: AuditConfig = AuditConfig()
    prompts_dir: str = str(BASE_DIR / "prompts")

    @property
    def prompts_path(self) -> Path:
        path = Path(self.prompts_dir)
        if not path.is_absolute():
            path = BASE_DIR / path
        return path


def _apply_env(cfg: AppConfig) -> None:
    env = os.environ
    if env.get("LLM_PROVIDER"):
        cfg.llm.provider = env["LLM_PROVIDER"]
    if env.get("LLM_MODEL"):
        cfg.llm.model = env["LLM_MODEL"]
    if env.get("LLM_BASE_URL"):
        cfg.llm.base_url = env["LLM_BASE_URL"]
    if env.get("LLM_API_KEY"):
        cfg.llm.api_key = env["LLM_API_KEY"]
    if env.get("PROMETHEUS_URL"):
        cfg.sources.prometheus_url = env["PROMETHEUS_URL"]
    if env.get("LOKI_URL"):
        cfg.sources.loki_url = env["LOKI_URL"]
    if env.get("NOTIFY_WEBHOOK_URL"):
        cfg.notify.webhook_url = env["NOTIFY_WEBHOOK_URL"]
        cfg.notify.enabled = True
    if env.get("AUDIT_PATH"):
        cfg.audit.path = env["AUDIT_PATH"]
    if env.get("PROMPTS_DIR"):
        cfg.prompts_dir = env["PROMPTS_DIR"]


def load_config(path: Optional[str] = None) -> AppConfig:
    """Загружает конфигурацию. Отсутствующий файл не является ошибкой (работают дефолты)."""
    data: dict = {}
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    elif os.environ.get("LLM_ASSISTANT_CONFIG"):
        candidates.append(Path(os.environ["LLM_ASSISTANT_CONFIG"]))
    else:
        candidates.extend([BASE_DIR / "config.yaml", BASE_DIR / "config.example.yaml"])

    for candidate in candidates:
        if candidate.exists():
            loaded = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                data = loaded
            break

    cfg = AppConfig.model_validate(data)
    _apply_env(cfg)
    return cfg
