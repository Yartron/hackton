"""Уведомления: результат анализа публикуется в Slack/MS-generic webhook."""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from .models import AnalysisResult

log = logging.getLogger("llm-assistant.notify")

_SEVERITY_EMOJI = {"critical": "🔴", "warning": "🟠", "info": "🔵"}


def format_message(result: AnalysisResult) -> str:
    parsed = result.parsed or {}
    emoji = _SEVERITY_EMOJI.get(str(parsed.get("severity", "")), "⚪")
    lines = [
        f"{emoji} LLM-разбор инцидента [{result.prompt_id}] — {result.model}",
        f"severity: {parsed.get('severity', 'n/a')}"
        + (" | SLO под угрозой" if parsed.get("slo_breach") else ""),
        f"summary: {parsed.get('summary', '—')}",
    ]

    hypotheses = parsed.get("root_cause_hypotheses") or []
    if hypotheses:
        lines.append("гипотезы:")
        for item in hypotheses[:3]:
            if isinstance(item, dict):
                lines.append(
                    f"  • {item.get('hypothesis', '?')} "
                    f"(confidence={item.get('confidence', '?')})"
                )

    actions = parsed.get("recommended_actions") or parsed.get("options") or []
    if actions:
        lines.append("рекомендации (требуют подтверждения, ничего не применено):")
        for item in actions[:4]:
            if isinstance(item, dict):
                risk = item.get("risk", "")
                cmd = item.get("command") or item.get("action") or ""
                lines.append(f"  • [{risk}] {cmd}")

    runbook = parsed.get("runbook") or parsed.get("runbook_url")
    if isinstance(runbook, dict) and runbook.get("url"):
        lines.append(f"runbook: {runbook['url']}")
    elif isinstance(runbook, str):
        lines.append(f"runbook: {runbook}")

    lines.append(f"request_id={result.request_id} mode={result.mode}")
    if result.warnings:
        lines.append("warnings: " + "; ".join(result.warnings))
    return "\n".join(lines)


def send(webhook_url: str, text: str, timeout: float = 5.0) -> bool:
    """Отправка best-effort: сбой уведомления не должен ронять анализ."""
    if not webhook_url:
        return False
    try:
        response = httpx.post(webhook_url, json={"text": text}, timeout=timeout)
        response.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("не удалось отправить уведомление: %s", exc)
        return False


def maybe_notify(
    enabled: bool, webhook_url: Optional[str], result: AnalysisResult
) -> dict[str, Any]:
    if not enabled or not webhook_url:
        return {"notified": False, "reason": "disabled"}
    ok = send(webhook_url, format_message(result))
    return {"notified": ok, "reason": "" if ok else "delivery_failed"}
