# LLM-ассистент платформы наблюдаемости (пункт 9)

Сервис, который берёт алерты/логи/метрики, отправляет их в LLM и возвращает
структурированный разбор: гипотезы причины, влияние на SLO, предлагаемые действия
и runbook. Всё в режиме **suggested** — ассистент ничего не применяет.

* Промты и их документация: [`../docs/prompts.md`](../docs/prompts.md)
* Исходники промтов: [`prompts/`](prompts/)
* Манифесты: [`deploy/`](deploy/)

## Зависимости (uv)

Менеджер зависимостей — [uv](https://docs.astral.sh/uv/). Источник правды —
`pyproject.toml`, зафиксированные версии — `uv.lock` (коммитится в репозиторий,
проверяется в CI через `uv lock --check`). Нужна версия Python 3.12: она закреплена
в `.python-version`, и uv скачает интерпретатор сам, если системного нет.

```bash
cd llm-assistant

uv python pin 3.12          # закрепить интерпретатор (один раз)
uv sync                     # создать .venv и поставить зависимости из uv.lock
uv sync --frozen            # строго по lock-файлу (как в CI и Docker)
uv run --frozen pytest      # тесты с гарантированно теми же версиями

# добавить / обновить / удалить пакет
uv add httpx
uv add --dev ruff
uv remove httpx

# обновить lock-файл после правки pyproject.toml
uv lock
```

Прод-зависимости лежат в `[project.dependencies]`, инструменты для разработки
(pytest) — в группе `dev` (`[dependency-groups]`). Docker-образ ставит только прод-группу
(`uv sync --frozen --no-dev`), поэтому pytest в образ не попадает.

## Быстрый старт

```bash
cd llm-assistant
uv sync

# офлайн-режим (провайдер mock, ключ не нужен)
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080
```

```bash
curl -s localhost:8080/healthz | jq
curl -s -X POST localhost:8080/demo/incident | jq '.mode, .parsed'
curl -s -X POST localhost:8080/webhook/alertmanager -H 'content-type: application/json' -d @sample_alert.json
```

Сценарий целиком одним скриптом: `bash scripts/demo.sh` (health → промты →
демо-инцидент → вебхук Alertmanager → remediation → audit).

Реальный провайдер:

```bash
export LLM_PROVIDER=openai           # openai | anthropic | ollama | mock
export LLM_MODEL=gpt-4o-mini
export LLM_API_KEY=sk-...
export LLM_BASE_URL=https://api.openai.com/v1
uv run uvicorn app.main:app --port 8080
```

Для локального приватного запуска: `LLM_PROVIDER=ollama LLM_MODEL=llama3.1`.

## API

| Метод/путь | Назначение |
|---|---|
| `GET /healthz` | статус, провайдер, список промтов |
| `GET /prompts`, `GET /prompts/{id}` | каталог промтов и их содержимое |
| `POST /webhook/alertmanager` | вебхук Alertmanager → разбор инцидента |
| `POST /analyze` | универсальный вызов любого промта |
| `POST /alerts/propose` | предложить правило алерта (PromQL) |
| `POST /remediation/propose` | предложить действия по восстановлению (dry-run + approval) |
| `POST /logs/analyze` | анализ фрагмента логов + предлагаемый алерт |
| `POST /demo/incident` | демо-инцидент без Alertmanager (для видео/смоуков) |
| `GET /audit?limit=` | audit log обращений к LLM |

Ответ любого аналитического эндпоинта — `AnalysisResult`:

```json
{
  "request_id": "1f2e3d4c5b6a",
  "prompt_id": "incident_analysis",
  "prompt_version": 2,
  "provider": "mock",
  "model": "mock-1",
  "latency_ms": 2,
  "mode": "suggested",
  "parsed": {"severity": "critical", "root_cause_hypotheses": ["..."], "...": "..."},
  "raw": "...",
  "warnings": [],
  "created_at": "2026-10-07T10:12:00+00:00"
}
```

## Как это работает

1. **Enrichment** (`app/enrich.py`): Prometheus — срез SLI (error_rate, p95,
   availability, firing alerts) за `sources.lookback`; Loki — хвост логов по
   фильтру `sources.loki_query`, усечённый до `llm.max_log_chars`.
2. **Рендер промта** (`app/prompts.py`): шаблон из YAML + переменные; недостающие
   переменные подставляются как `(нет данных: name)` и попадают в `warnings`.
3. **Вызов LLM** (`app/providers.py`): `openai` / `anthropic` / `ollama` / `mock`.
   Ответ парсится в JSON (`app/parsing.py`) — фенсы и обёртка текста поддержаны.
4. **Аудит и уведомление** (`app/audit.py`, `app/notify.py`): JSONL с хэшами
   входа/выхода и latency; опционально пост в Slack/MS webhook.

## Демо-сценарий (для видео ≤ 5 минут)

```bash
# 1) детект: алерт приходит в Alertmanager (или используем демо-инцидент)
curl -s -X POST localhost:8080/demo/incident | jq '{mode, severity: .parsed.severity,
  hyp: .parsed.root_cause_hypotheses, act: .parsed.recommended_actions}'

# 2) LLM предлагает восстановление (всё ещё suggested)
curl -s -X POST localhost:8080/remediation/propose -H 'content-type: application/json' \
  -d '{"symptoms":"5xx растёт после релиза","namespace":"demo","target_name":"demo-app"}' \
  | jq '.parsed.dry_run, .parsed.options'

# 3) audit: что было отправлено/получено
curl -s 'localhost:8080/audit?limit=3' | jq '.[] | {ts, prompt_id, status, latency_ms, mode}'
```

Порядок доказательства для отчёта: `детект (алерт) → разбор LLM (suggested) →
закрытый контур п.6 (dry-run → подтверждение → применение) → верификация SLO →
MTTR до/после`.

## Тесты

```bash
cd llm-assistant
uv run pytest
```

Покрытие: рендер всех промтов без «сырых» плейсхолдеров, валидность JSON от mock,
гард-райлы (изменения только с approval), webhook → анализ → audit, 404 на
неизвестный промт.

## Развёртывание

```bash
kubectl apply -f deploy/k8s.yaml               # Deployment/Service/ConfigMap/Secret
kubectl apply -f deploy/alertmanager-webhook.yaml  # вебхук Alertmanager (фрагмент route)
```

Образ: `Dockerfile` (python:3.12-slim, uv из `ghcr.io/astral-sh/uv`, non-root, healthcheck).
Зависимости ставятся из `uv.lock` без dev-группы, поэтому образ воспроизводим.

## Структура

```
llm-assistant/
├── app/
│   ├── main.py       # FastAPI-эндпоинты
│   ├── service.py    # оркестрация сценариев
│   ├── enrich.py     # Prometheus/Loki → контекст
│   ├── prompts.py    # загрузка/рендер YAML-промтов
│   ├── providers.py  # openai / anthropic / ollama / mock
│   ├── parsing.py    # JSON из ответа модели
│   ├── audit.py      # audit.jsonl
│   ├── notify.py     # Slack/MS webhook
│   ├── models.py     # схемы API
│   └── config.py     # YAML + env
├── prompts/*.yaml    # промты как код
├── tests/            # pytest
├── deploy/           # k8s + alertmanager
├── config.example.yaml
├── pyproject.toml    # зависимости и настройки pytest (uv)
├── uv.lock           # зафиксированные версии зависимостей
├── .python-version   # Python 3.12
└── Dockerfile
```
