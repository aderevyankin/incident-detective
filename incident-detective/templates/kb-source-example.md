---
id: SRC-stage-payment-api
kind: source
stand: stage
services: [payment-api]
source: {kind: mcp, server: kibana-mcp, tool: search_logs}
address: {index: logs-stage-*}
query: "env:stage AND service:payment-api AND level:(ERROR OR FATAL)"
fields: {time: @timestamp, level: log.level, message: message, service: service.name}
confirmed: 2026-07-28
checked: [{source: loki-mcp, verdict: empty, date: 2026-07-28, note: стенд не пишет в loki}]
---

# Карта источников: stage / payment-api

Откуда берутся логи для этой пары стенда и сервиса. Запись — адресация,
а не кэш: самих логов здесь нет, за ними надо сходить в источник.

Как читать поля:

- `source` — чем и как обращаться: MCP-сервер и инструмент, либо `kind: file` с путём,
  либо `kind: cmd` с командой. Сеть — дело агента или пользователя, скрипты в неё не ходят.
- `address` — адрес внутри источника: индекс, namespace, поток.
- `query` — запрос, который сработал в прошлый раз; секреты в нём маскируются при записи.
- `fields` — соответствие полей источника канонической схеме разбора, чтобы не выяснять
  его заново.
- `confirmed` — дата последнего подтверждения. Старше 30 дней — запись выдаётся, но
  помечается как требующая проверки: инструмент могли перенастроить.
- `checked` — проверенные и отвергнутые инструменты: `empty` (за окно ничего нет),
  `unavailable` (отказ доступа или сервера). Пометка привязана к имени инструмента, а не
  к каналу целиком, и стареет по тому же сроку. Инструмента, которого в перечне нет,
  считается непроверенным.

Пример записи для формата карты. Реальные записи добавляются через
`kb_add.py --kind source` и `kb_add.py --mark-checked`, читаются — через
`kb_search.py --sources --stand <стенд> --service <сервис>`.
