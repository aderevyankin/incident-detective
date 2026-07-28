#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка бюджетов скорости скриптов скилла.

«Скилл должен работать быстро» — требование, которое нельзя нарушить, пока его нечем
измерить. Бенчмарк даёт числа: генерирует эталонный вход, прогоняет на нём каждый скрипт
и сравнивает с бюджетом из спецификации `performance-budgets`.

Вход генерируется здесь же, детерминированно: держать 43 МБ тестовых логов в репозитории
незачем, а скачивать их нельзя — скилл обязан работать без сети, и проверка тоже.

Абсолютные числа зависят от машины, поэтому вывод содержит пометку, на чём измеряли.
Бюджеты — порог для типовой рабочей машины, а не гарантия.

  python3 tools/bench.py
  python3 tools/bench.py --lines 100000     # быстрый прогон
  python3 tools/bench.py --only parse_logs
  python3 tools/bench.py --json             # для сравнения прогонов
"""

import argparse
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO, 'incident-detective', 'scripts')

DEFAULT_LINES = 400000
KB_RECORDS = 1000
SEED = 20260728

# Бюджеты из openspec/specs/performance-budgets. Скорость — в строках в секунду,
# время — в секундах.
BUDGETS = {
    'parse_logs': {'kind': 'rate', 'limit': 50000,
                   'title': 'parse_logs.py без фильтров'},
    'parse_logs_level': {'kind': 'rate', 'limit': 150000,
                         'title': 'parse_logs.py --level ERROR'},
    'parse_logs_window': {'kind': 'rate', 'limit': 150000,
                          'title': 'parse_logs.py --since/--until'},
    'kb_search': {'kind': 'time', 'limit': 1.0,
                  'title': 'kb_search.py'},
    'code_hints': {'kind': 'time', 'limit': 3.0,
                   'title': 'code_hints.py'},
    'confidence': {'kind': 'time', 'limit': 1.0,
                   'title': 'confidence.py'},
    'triage': {'kind': 'time', 'limit': 15.0,
               'title': 'triage.py (вся цепочка)'},
}

ORDER = ['parse_logs', 'parse_logs_level', 'parse_logs_window',
         'kb_search', 'code_hints', 'confidence', 'triage']


# --- эталонный вход -------------------------------------------------------------

MESSAGES = [
    'GET /api/v1/pay/{n} 200 {ms}ms',
    'POST /api/v1/pay 201 {ms}ms user={n}',
    'connection pool exhausted, waited {ms} ms (size=20 active=20)',
    'upstream timed out service=gateway upstream=payment-api after {ms}ms',
    'user {n} authenticated',
    'cache miss key=session:{n}',
    'HikariPool-1 - Connection is not available, request timed out after {ms}ms',
    'failed to commit transaction tx={n}: deadlock detected',
]

SERVICES = ['payment-api', 'gateway', 'db-proxy']

STACK = (
    'java.sql.SQLTimeoutException: connection is not available\n'
    '\tat com.acme.payment.PoolManager.acquire(PoolManager.java:88)\n'
    '\tat com.acme.payment.PaymentService.charge(PaymentService.java:142)\n'
    '\tat com.acme.payment.PaymentController.pay(PaymentController.java:57)\n'
)

# Начало окна эталонного лога. Фиксировано, чтобы --since/--until в прогоне всегда
# попадали в одни и те же записи независимо от даты запуска.
BASE_TS = (2026, 7, 28, 12, 0, 0)


def _ts(offset_ms):
    """Время записи как строка. Без datetime: арифметика по модулю дешевле и
    достаточна — лог укладывается в несколько часов одних суток."""
    total = offset_ms // 1000
    ms = offset_ms % 1000
    sec = BASE_TS[5] + total
    minute = BASE_TS[4] + sec // 60
    sec %= 60
    hour = BASE_TS[3] + minute // 60
    minute %= 60
    return '%04d-%02d-%02d %02d:%02d:%02d,%03d' % (
        BASE_TS[0], BASE_TS[1], BASE_TS[2], hour, minute, sec, ms)


def generate_log(path, lines):
    """Эталонный лог: смесь форматов, разные уровни, trace id, стектрейсы."""
    rnd = random.Random(SEED)
    levels = ['INFO'] * 6 + ['DEBUG'] * 2 + ['WARN'] + ['ERROR']
    written = 0
    offset = 0
    with open(path, 'w', encoding='utf-8') as fh:
        while written < lines:
            offset += 13
            level = rnd.choice(levels)
            msg = rnd.choice(MESSAGES).format(n=rnd.randint(1000, 99999),
                                              ms=rnd.randint(1, 30000))
            # trace_id, а не trace: парсер распознаёт именно такую форму, и на
            # входе без неё корреляция по запросам не проверяется вовсе
            fh.write('%s %s [%s] trace_id=%08x %s\n' % (
                _ts(offset), level, rnd.choice(SERVICES),
                rnd.getrandbits(32), msg))
            written += 1
            if level == 'ERROR' and rnd.random() < 0.05 and written + 4 <= lines:
                fh.write(STACK)
                written += 4
    return written


KB_TEMPLATE = """---
id: INC-BENCH-%(num)04d
title: %(title)s
date: 2026-07-%(day)02d
stands: [%(stand)s]
services: [%(service)s]
tags: [%(tag)s, bench]
severity: medium
status: resolved
files: [src/com/acme/%(service_dir)s/Handler.java]
signatures:
  - "%(exc)s"
  - "http_status=%(code)s"
  - "tmpl:%(tmpl)s"
---

## Симптомы

%(title)s на стенде %(stand)s, сервис %(service)s.

## Диагностика

Разбор логов показал шаблон `%(tmpl)s` с ростом частоты.

## Причина

%(cause)s

## Решение

Изменена конфигурация сервиса %(service)s.
"""


def generate_kb(kb_dir, count):
    """База знаний для замера поиска. Записи разные, иначе индекс и токенизация
    работают на вырожденном входе и время получается неправдоподобно малым."""
    rnd = random.Random(SEED + 1)
    excs = ['java.sql.SQLTimeoutException', 'java.lang.OutOfMemoryError',
            'ConnectionResetError', 'redis.exceptions.TimeoutError',
            'psycopg2.OperationalError', 'requests.exceptions.ReadTimeout']
    tags = ['timeout', 'db', 'pool', 'memory', 'network', 'cache', 'latency', 'oom']
    causes = ['Пул соединений не покрывал нагрузку после релиза.',
              'Утечка памяти в обработчике фоновых задач.',
              'Сетевой таймаут до внешнего платёжного провайдера.',
              'Кэш инвалидировался целиком при каждом обновлении справочника.']
    os.makedirs(kb_dir, exist_ok=True)
    for i in range(count):
        service = rnd.choice(SERVICES)
        data = {
            'num': i,
            'title': '%s на %s: %s' % (rnd.choice(['504', '500', 'таймаут', 'зависание']),
                                       service, rnd.choice(causes)[:40]),
            'day': rnd.randint(1, 28),
            'stand': rnd.choice(['stage', 'prod', 'test', 'dev']),
            'service': service,
            'service_dir': service.replace('-', ''),
            'tag': rnd.choice(tags),
            'exc': rnd.choice(excs),
            'code': rnd.choice(['500', '502', '503', '504']),
            'tmpl': rnd.choice(['connection pool exhausted, waited <n> ms',
                                'upstream timed out service=<s> upstream=<s>',
                                'read timeout after <n> ms',
                                'deadlock detected on tx <n>']),
            'cause': rnd.choice(causes),
        }
        path = os.path.join(kb_dir, 'INC-BENCH-%04d.md' % i)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(KB_TEMPLATE % data)
    run([sys.executable, os.path.join(SCRIPTS_DIR, 'kb_index.py'), '--kb', kb_dir])


# --- прогон ---------------------------------------------------------------------

def run(cmd, stdout=None):
    """Запуск с подавленным выводом. Возвращает (код, время)."""
    started = time.time()
    with open(os.devnull, 'w') as devnull:
        out = devnull if stdout is None else open(stdout, 'w', encoding='utf-8')
        try:
            proc = subprocess.run(cmd, stdout=out, stderr=subprocess.PIPE)
        finally:
            if stdout is not None:
                out.close()
    return proc.returncode, time.time() - started, proc.stderr.decode('utf-8', 'replace')


def script(name):
    return os.path.join(SCRIPTS_DIR, name)


def measure(work_dir, log_path, kb_dir, lines, only):
    results = []

    def want(key):
        return not only or key in only

    parsed_json = os.path.join(work_dir, 'parsed.json')
    kb_json = os.path.join(work_dir, 'kb.json')
    code_json = os.path.join(work_dir, 'code.json')

    if want('parse_logs'):
        code, elapsed, err = run([sys.executable, script('parse_logs.py'), log_path,
                                  '--format', 'json'], stdout=parsed_json)
        results.append(('parse_logs', elapsed, lines / elapsed if elapsed else 0, code, err))
    elif not os.path.exists(parsed_json):
        # последующие замеры опираются на разбор — делаем его молча
        run([sys.executable, script('parse_logs.py'), log_path, '--format', 'json'],
            stdout=parsed_json)

    if want('parse_logs_level'):
        code, elapsed, err = run([sys.executable, script('parse_logs.py'), log_path,
                                  '--level', 'ERROR'])
        results.append(('parse_logs_level', elapsed, lines / elapsed if elapsed else 0,
                        code, err))

    if want('parse_logs_window'):
        code, elapsed, err = run([sys.executable, script('parse_logs.py'), log_path,
                                  '--since', '2026-07-28 12:10',
                                  '--until', '2026-07-28 12:15'])
        results.append(('parse_logs_window', elapsed, lines / elapsed if elapsed else 0,
                        code, err))

    if want('kb_search'):
        code, elapsed, err = run([sys.executable, script('kb_search.py'),
                                  '--from-parsed', parsed_json, '--kb', kb_dir,
                                  '--format', 'json'], stdout=kb_json)
        results.append(('kb_search', elapsed, None, code, err))
    elif not os.path.exists(kb_json):
        run([sys.executable, script('kb_search.py'), '--from-parsed', parsed_json,
             '--kb', kb_dir, '--format', 'json'], stdout=kb_json)

    if want('code_hints'):
        code, elapsed, err = run([sys.executable, script('code_hints.py'),
                                  '--from-parsed', parsed_json, '--repo', REPO,
                                  '--format', 'json'], stdout=code_json)
        results.append(('code_hints', elapsed, None, code, err))
    elif not os.path.exists(code_json):
        run([sys.executable, script('code_hints.py'), '--from-parsed', parsed_json,
             '--repo', REPO, '--format', 'json'], stdout=code_json)

    if want('confidence'):
        code, elapsed, err = run([sys.executable, script('confidence.py'),
                                  '--parsed', parsed_json, '--kb', kb_json,
                                  '--code', code_json, '--stand', 'stage'])
        results.append(('confidence', elapsed, None, code, err))

    if want('triage'):
        if os.path.exists(script('triage.py')):
            code, elapsed, err = run([sys.executable, script('triage.py'), log_path,
                                      '--repo', REPO, '--kb', kb_dir,
                                      '--out', os.path.join(work_dir, 'triage')])
            results.append(('triage', elapsed, None, code, err))
        else:
            results.append(('triage', None, None, None, 'скрипт ещё не реализован'))

    return results


def verdict(key, elapsed, rate):
    budget = BUDGETS[key]
    if elapsed is None:
        return 'пропущен', None
    if budget['kind'] == 'rate':
        return ('ok' if rate >= budget['limit'] else 'НАРУШЕН'), rate
    return ('ok' if elapsed <= budget['limit'] else 'НАРУШЕН'), elapsed


def main(argv=None):
    ap = argparse.ArgumentParser(description='Бюджеты скорости скриптов скилла.')
    ap.add_argument('--lines', type=int, default=DEFAULT_LINES,
                    help='строк в эталонном логе (%d)' % DEFAULT_LINES)
    ap.add_argument('--kb-records', type=int, default=KB_RECORDS,
                    help='записей в эталонной базе знаний (%d)' % KB_RECORDS)
    ap.add_argument('--only', action='append',
                    help='мерить только указанный замер (можно повторять)')
    ap.add_argument('--json', action='store_true', help='машинный вывод')
    ap.add_argument('--keep', help='директория для эталонного входа (не удалять)')
    args = ap.parse_args(argv)

    work_dir = args.keep or tempfile.mkdtemp(prefix='triage-bench-')
    os.makedirs(work_dir, exist_ok=True)
    log_path = os.path.join(work_dir, 'bench.log')
    kb_dir = os.path.join(work_dir, 'kb')

    env_note = 'Python %s, %s %s' % (platform.python_version(), platform.system(),
                                     platform.machine())
    try:
        if not os.path.exists(log_path):
            written = generate_log(log_path, args.lines)
        else:
            with open(log_path, 'r', encoding='utf-8') as fh:
                written = sum(1 for _ in fh)
        if not os.path.exists(os.path.join(kb_dir, 'index.json')):
            generate_kb(kb_dir, args.kb_records)

        size_mb = os.path.getsize(log_path) / (1024.0 * 1024.0)
        if not args.json:
            sys.stdout.write('Эталонный вход: %d строк, %.1f МБ, база знаний %d записей\n'
                             % (written, size_mb, args.kb_records))
            sys.stdout.write('Окружение: %s\n\n' % env_note)

        results = measure(work_dir, log_path, kb_dir, written,
                          set(args.only) if args.only else None)
    finally:
        if not args.keep:
            shutil.rmtree(work_dir, ignore_errors=True)

    violated = []
    rows = []
    for key, elapsed, rate, code, err in results:
        status, value = verdict(key, elapsed, rate)
        budget = BUDGETS[key]
        if code not in (0, None):
            status = 'ОШИБКА'
        if status in ('НАРУШЕН', 'ОШИБКА'):
            violated.append((key, status, err.strip()[:200]))
        rows.append({
            'key': key,
            'title': budget['title'],
            'kind': budget['kind'],
            'elapsed': round(elapsed, 3) if elapsed is not None else None,
            'rate': round(rate) if rate else None,
            'budget': budget['limit'],
            'status': status,
        })

    if args.json:
        json.dump({'environment': env_note, 'lines': written,
                   'kb_records': args.kb_records, 'results': rows},
                  sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write('\n')
    else:
        sys.stdout.write('%-38s %14s %14s  %s\n' % ('замер', 'измерено', 'бюджет', 'вердикт'))
        sys.stdout.write('%s\n' % ('-' * 78))
        for row in rows:
            if row['elapsed'] is None:
                measured = '—'
                limit = '—'
            elif row['kind'] == 'rate':
                measured = '%d стр/с' % row['rate']
                limit = '≥ %d стр/с' % row['budget']
            else:
                measured = '%.2f с' % row['elapsed']
                limit = '≤ %.1f с' % row['budget']
            sys.stdout.write('%-38s %14s %14s  %s\n'
                             % (row['title'], measured, limit, row['status']))
        if violated:
            sys.stdout.write('\nНарушено бюджетов: %d\n' % len(violated))
            for key, status, err in violated:
                sys.stdout.write('  %s — %s%s\n' % (key, status, (': ' + err) if err else ''))

    return 1 if violated else 0


if __name__ == '__main__':
    sys.exit(main())
