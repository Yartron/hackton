"""Audit log: каждое обращение к LLM фиксируется (что отправили, что получили, сколько стоило)."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Optional


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


class AuditLog:
    """Простой append-only JSONL-файл: легко тянуть в SIEM/лог-конвейер (vector/filebeat)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def record(
        self,
        *,
        request_id: str,
        prompt_id: str,
        prompt_version: int,
        provider: str,
        model: str,
        system: str,
        user: str,
        raw: str,
        latency_ms: int,
        status: str,
        parsed: Optional[dict[str, Any]] = None,
        warnings: Optional[list[str]] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "ts": _now_iso(),
            "request_id": request_id,
            "prompt_id": prompt_id,
            "prompt_version": prompt_version,
            "provider": provider,
            "model": model,
            "input_sha256": sha256_text(system + "\n" + user),
            "output_sha256": sha256_text(raw),
            "input_chars": len(system) + len(user),
            "output_chars": len(raw),
            "latency_ms": latency_ms,
            "status": status,
            "mode": "suggested",  # ассистент только предлагает, изменения применяет закрытый контур
            "warnings": warnings or [],
        }
        if parsed is not None:
            entry["parsed_summary"] = {
                "severity": parsed.get("severity"),
                "has_actions": bool(parsed.get("recommended_actions") or parsed.get("options")),
            }
        if extra:
            entry.update(extra)
        self.write(entry)
        return entry

    def read(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        records: list[dict[str, Any]] = []
        for line in lines[-max(1, limit) :]:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        records.reverse()  # свежие сверху
        return records


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
