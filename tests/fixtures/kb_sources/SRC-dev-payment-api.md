---
id: SRC-dev-payment-api
kind: source
stand: dev
services: [payment-api]
source: {kind: file, path: /var/log/payment-api/app.log}
confirmed: 2026-05-01
checked: [{source: kibana-mcp, verdict: unavailable, date: 2026-04-20, note: доступа к индексу dev нет}]
---

# Карта источников: dev / payment-api

Откуда берутся логи для этой пары стенда и сервиса. Запись — адресация,
а не кэш: самих логов здесь нет, за ними надо сходить в источник.
