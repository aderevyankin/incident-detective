---
id: INC-2026-06-010
title: "payment-api на stage: таймауты из-за исчерпанного пула соединений"
date: 2026-06-10
stands: [stage]
services: [payment-api]
tags: [pool, timeout]
severity: high
status: resolved
signatures:
  - "ConnectionTimeoutError"
  - "tmpl:connection pool exhausted, waiting for free connection"
---

## Симптомы

Оплата на stage отваливается по таймауту, в логах payment-api таймауты пула.

## Диагностика

Смотрели метрики пула и логи payment-api.

## Причина

Размер пула не покрывал нагрузку после релиза.

## Решение

Увеличили пул до 40 соединений.

## Проверка

Оплата проходит, таймаутов пула в логах нет.
