"""Обогащение контекста: метрики Prometheus и логи Loki перед отправкой в LLM."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from .config import SourcesConfig

log = logging.getLogger("llm-assistant.enrich")

_DURATION_RE = re.compile(r"^(\d+)(ms|s|m|h|d|w)$")


def duration_to_seconds(value: str, default: int = 1800) -> int:
    """'30m' -> 1800. Некорректное значение -> default."""
    match = _DURATION_RE.match((value or "").strip())
    if not match:
        return default
    amount, unit = int(match.group(1)), match.group(2)
    factor = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return int(amount * factor)


def _fmt(value: float) -> str:
    if value == 0:
        return "0"
    if abs(value) >= 100:
        return f"{value:.1f}"
    if abs(value) >= 1:
        return f"{value:.3f}"
    return f"{value:.5f}"


def _prometheus_query_range(
    url: str, query: str, start: datetime, end: datetime, step: str, timeout: float
) -> list[dict[str, Any]]:
    params = {
        "query": query,
        "start": start.timestamp(),
        "end": end.timestamp(),
        "step": step,
    }
    response = httpx.get(f"{url.rstrip('/')}/api/v1/query_range", params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"prometheus error: {payload.get('error', 'unknown')}")
    return payload.get("data", {}).get("result", [])


def _prometheus_instant(url: str, query: str, timeout: float) -> list[dict[str, Any]]:
    response = httpx.get(f"{url.rstrip('/')}/api/v1/query", params={"query": query}, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"prometheus error: {payload.get('error', 'unknown')}")
    return payload.get("data", {}).get("result", [])


def _summarize_series(name: str, result: list[dict[str, Any]]) -> str:
    if not result:
        return f"{name}: no data"
    parts: list[str] = []
    for series in result[:4]:
        values = []
        for sample in series.get("values", []):
            try:
                values.append(float(sample[1]))
            except (TypeError, ValueError, IndexError):
                continue
        if not values:
            # мгновенный формат {value: [ts, "v"]}
            single = series.get("value")
            if single:
                try:
                    values.append(float(single[1]))
                except (TypeError, ValueError, IndexError):
                    pass
        if not values:
            continue
        labels = series.get("metric", {})
        label_txt = ",".join(
            f"{k}={v}" for k, v in sorted(labels.items()) if k not in ("__name__", "le")
        )
        suffix = f" [{label_txt}]" if label_txt else ""
        parts.append(
            f"{name}{suffix}: last={_fmt(values[-1])} max={_fmt(max(values))} "
            f"min={_fmt(min(values))} points={len(values)}"
        )
    return "; ".join(parts) or f"{name}: no numeric data"


def prometheus_snapshot(cfg: SourcesConfig) -> str:
    """Сжатый срез SLI-метрик за окно наблюдения — вход для промта."""
    if not cfg.enabled:
        return "(сбор метрик отключён в конфигурации sources.enabled=false)"

    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=duration_to_seconds(cfg.lookback))
    timeout = 10.0
    lines: list[str] = []
    errors: list[str] = []

    for name, query in cfg.queries.items():
        if not query or not query.strip():
            continue
        try:
            if name == "firing_alerts":
                result = _prometheus_instant(cfg.prometheus_url, query, timeout)
                if not result:
                    lines.append("firing_alerts: нет активных алертов")
                else:
                    for series in result[:10]:
                        labels = series.get("metric", {})
                        value = (series.get("value") or ["", "0"])[1]
                        lines.append(
                            "firing_alerts: "
                            f"{labels.get('alertname', '?')} "
                            f"severity={labels.get('severity', '?')} value={value}"
                        )
            else:
                result = _prometheus_query_range(
                    cfg.prometheus_url, query, start, end, cfg.step, timeout
                )
                lines.append(_summarize_series(name, result))
        except Exception as exc:  # noqa: BLE001 — источник не должен ронять анализ
            log.warning("prometheus query '%s' failed: %s", name, exc)
            errors.append(f"{name}: недоступен ({type(exc).__name__})")

    if errors:
        lines.append("недоступные источники: " + ", ".join(errors))
    return "\n".join(lines) if lines else "(метрики не получены)"


def loki_tail(cfg: SourcesConfig) -> str:
    """Последние строки логов, прошедшие фильтр, усечённые до max_log_chars."""
    if not cfg.enabled:
        return "(сбор логов отключён в конфигурации sources.enabled=false)"

    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=duration_to_seconds(cfg.lookback))
    params = {
        "query": cfg.loki_query,
        "start": str(int(start.timestamp() * 1e9)),
        "end": str(int(end.timestamp() * 1e9)),
        "limit": cfg.loki_limit,
        "direction": "backward",
    }
    try:
        response = httpx.get(
            f"{cfg.loki_url.rstrip('/')}/loki/api/v1/query_range", params=params, timeout=10.0
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("loki query failed: %s", exc)
        return f"(логи недоступны: {type(exc).__name__})"

    lines: list[str] = []
    for stream in payload.get("data", {}).get("result", []):
        labels = stream.get("stream", {})
        source = labels.get("pod") or labels.get("app") or labels.get("job") or "?"
        for ts, raw in stream.get("values", []):
            text = str(raw).replace("\n", " ").strip()
            if not text:
                continue
            stamp = _ns_to_iso(ts)
            lines.append(f"{stamp} [{source}] {text}")

    lines = lines[-cfg.loki_limit :]
    blob = "\n".join(lines)
    if len(blob) > cfg.max_log_chars:
        blob = "...(обрезано)...\n" + blob[-cfg.max_log_chars:]
    return blob or "(за окно наблюдения строк по фильтру нет)"


def _ns_to_iso(ns_value: Any) -> str:
    try:
        seconds = int(ns_value) / 1e9
    except (TypeError, ValueError):
        return "?"
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%H:%M:%S")


def collect_context(cfg: SourcesConfig, logs: Optional[str] = None) -> dict[str, str]:
    """Собирает метрики и логи; готовые строки (например, переданные в запросе) имеют приоритет."""
    metrics = prometheus_snapshot(cfg)
    loki = logs.strip() if logs else loki_tail(cfg)
    return {"metrics": metrics, "logs": loki, "lookback": cfg.lookback}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
