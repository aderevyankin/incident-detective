---
id: SRC-stage-payment-api
kind: source
stand: stage
services: [payment-api]
source: {kind: mcp, server: kibana-mcp, tool: search_logs}
address: {index: logs-stage-*}
query: "env:stage AND service:payment-api AND level:(ERROR OR FATAL)"
fields: {time: @timestamp, level: log.level, message: message}
confirmed: 2026-07-20
checked: [{source: loki-mcp, verdict: empty, date: 2026-07-20, note: стенд не пишет в loki}]
---

# Карта источников: stage / payment-api

Откуда берутся логи для этой пары стенда и сервиса. Запись — адресация,
а не кэш: самих логов здесь нет, за ними надо сходить в источник.
