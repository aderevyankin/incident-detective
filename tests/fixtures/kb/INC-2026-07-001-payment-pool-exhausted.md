---
id: INC-2026-07-001
title: "payment-api отдаёт таймауты: исчерпан пул соединений к базе"
date: 2026-07-14
stands: [stage]
services: [payment-api]
tags: [pool, timeout, database]
severity: high
status: resolved
files: [src/payment/pool.py]
signatures:
  - "ConnectionTimeoutError"
  - "tmpl:ConnectionTimeoutError: timed out waiting for connection from pool after <n> ms"
  - "tmpl:connection pool exhausted, waiting for free connection"
---

## Симптомы

Оплаты на стенде перестали проходить, платёжный сервис отвечает таймаутом.
В логах payment-api пачка ConnectionTimeoutError, перед ней предупреждения про
исчерпанный пул соединений.

## Диагностика

Разобрали логи payment-api: доминирующий шаблон — таймаут ожидания соединения из
пула, первое предупреждение появилось за три минуты до первой ошибки.

## Причина

Размер пула соединений (20) меньше числа параллельных обработчиков после
включения батчевой обработки платежей.

## Решение

Подняли размер пула до 60 и вернули таймаут ожидания к 2 с, чтобы очередь не
копилась молча.

## Проверка

Прогнали батч из 200 платежей: предупреждений про исчерпанный пул нет,
ConnectionTimeoutError не появляется.

## Заметки

Вымышленная запись, используется в фикстурах прогона проверок.
