---
id: INC-2026-07-020
kind: incident
title: "payment-api на prod: те же таймауты пула после выкатки"
date: 2026-07-20
stands: [prod]
services: [payment-api]
tags: [pool, timeout]
severity: high
status: resolved
outcome: confirmed
outcome_date: 2026-07-22
signatures:
  - "ConnectionTimeoutError"
  - "tmpl:connection pool exhausted, waiting for free connection"
---

## Симптомы

После выкатки на prod оплата отваливается по таймауту так же, как раньше на stage.

## Диагностика

Сравнили с разбором на stage, проверили размер пула на prod.

## Причина

На prod пул остался прежним — настройку не перенесли вместе с релизом.

## Решение

Перенесли настройку пула на prod.

## Проверка

Таймаутов пула в логах prod нет сутки.
