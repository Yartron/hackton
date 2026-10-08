# SRE-платформа наблюдаемости: от метрик и логов к автоматическому реагированию на инциденты

Репозиторий командного проекта (демо-сервис `demo-app` в Kubernetes, SRE-практики).

## Статус пунктов ТЗ

| # | Пункт | Статус |
|---|---|---|
| 1 | Стек наблюдаемости (Prometheus/Grafana/Alertmanager/Loki + collector) | ⏳ план |
| 2 | SLO/SLI и дашборды | ⏳ план |
| 3 | Системные логи Linux (rsyslog/auditd) через Ansible | ⏳ план |
| 4 | Алертинг с эскалацией + runbook | ⏳ план |
| 5 | Хаос-инжиниринг (Chaos Mesh/Litmus) | ⏳ план |
| 6 | Самоисцеление (closed-loop) + MTTR | ⏳ план |
| 7 | mTLS + трейсы (Istio/Linkerd, Tempo/Jaeger) | ⏳ план |
| 8 | Синтетика + нагрузка (Blackbox, k6/Locust) | ⏳ план |
| **9** | **LLM-ассистент: анализ логов/метрик, генерация алертов, предложения по инцидентам + документация промтов** | ✅ **реализован** |
| 10 | Стресс-сценарий «детект → восстановление» | ⏳ план |

## Архитектура (пункт 9)

```
                        ┌──────────────────────────────────────────┐
 Alertmanager ─webhook─▶│  llm-assistant (FastAPI, Python)         │
 Prometheus ──query────▶│  enrich → render(промт) → LLM → JSON     │
 Loki ───────query─────▶│  audit.jsonl + Slack/MS notify           │
                        └────────────────┬─────────────────────────┘
                                         │ mode = "suggested"
                                         ▼
                    закрытый контур самоисцеления (п.6):
                    dry-run → подтверждение → применение → верификация SLO
```

Компонент: [`llm-assistant/`](llm-assistant/) — сервис, промты как код, тесты, Dockerfile,
k8s-манифесты и конфиг вебхука Alertmanager.

Документация промтов: [`docs/prompts.md`](docs/prompts.md).

## SLO/SLI (для демо-сервиса, используется в промтах)

| SLI | Цель |
|---|---|
| Доступность (доля не-5xx) | ≥ 99.5% за 30 дней |
| Latency p95 | ≤ 300 мс |
| Error rate | ≤ 1% |

## Быстрый старт (пункт 9)

```bash
cd llm-assistant
pip install -r requirements.txt
uvicorn app.main:app --port 8080

curl -s -X POST localhost:8080/demo/incident | jq
pytest
```

Офлайн-режим использует провайдер `mock` (ключ не нужен). Подключение реального LLM:

```bash
export LLM_PROVIDER=openai LLM_MODEL=gpt-4o-mini LLM_API_KEY=sk-...
```

## Связка «метрика → трейс → лог» на одном инциденте

Реализация п.7 появится позже; уже сейчас ассистент собирает для одного инцидента
метрики (Prometheus) и логи (Loki) в едином контексте — по `request_id` запрос
сохраняется в audit log и коррелируется с алертом (`incident_id` = `groupKey`
Alertmanager).
