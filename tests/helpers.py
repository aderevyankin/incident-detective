# -*- coding: utf-8 -*-
"""Общее для проверок: пути, запуск скриптов, сверка с ожидаемым результатом.

Скрипты проверяются как процессы, а не как импортированные модули: скилл
запускают именно так, и сравнивать надо то, что видит агент, — код возврата,
стандартный вывод, JSON.
"""

import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCRIPTS = os.path.join(REPO, 'incident-detective', 'scripts')
FIXTURES = os.path.join(HERE, 'fixtures')
LOGS = os.path.join(FIXTURES, 'logs')
KB = os.path.join(FIXTURES, 'kb')
EXPECTED = os.path.join(HERE, 'expected')

# «Сейчас» для всех проверок. Любая фикстура, результат которой зависит от
# текущего времени, считается от этого значения — иначе прогон, прошедший
# сегодня, упадёт в следующем году, и падение будет ложным.
NOW = '2026-07-28 20:00:00'


def log(name):
    return os.path.join(LOGS, name)


def script_env(now=NOW, extra=None):
    env = os.environ.copy()
    env.pop('INCIDENT_TRACE_FILE', None)
    env.pop('INCIDENT_KB_DIR', None)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    if now is None:
        env.pop('INCIDENT_NOW', None)
    else:
        env['INCIDENT_NOW'] = now
    if extra:
        for key, val in extra.items():
            if val is None:
                env.pop(key, None)
            else:
                env[key] = val
    return env


def run(name, args, now=NOW, env=None, cwd=None):
    """Запускает скрипт скилла. Возвращает (код возврата, stdout, stderr).

    По умолчанию рабочая директория — корень файловой системы: прогон не должен
    зависеть от того, откуда его запустили.
    """
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, name)] + [str(a) for a in args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=script_env(now, env), cwd=cwd or os.path.abspath(os.sep))
    return (proc.returncode,
            proc.stdout.decode('utf-8', 'replace'),
            proc.stderr.decode('utf-8', 'replace'))


def run_json(case, name, args, now=NOW, env=None, cwd=None):
    """То же, но с разбором JSON-вывода и проверкой, что скрипт отработал."""
    code, out, err = run(name, list(args) + ['--format', 'json'], now, env, cwd)
    case.assertEqual(code, 0, '%s завершился с кодом %d\n%s' % (name, code, err))
    case.assertNotIn('Traceback', err, '%s упал трассировкой:\n%s' % (name, err))
    try:
        return json.loads(out)
    except ValueError as exc:
        raise AssertionError('%s выдал не JSON: %s\n%s' % (name, exc, out[:2000]))


def parsed_to_file(case, tmpdir, name, sources, args=(), now=NOW):
    """Разбор логов, сохранённый в файл, — вход для остальных контуров."""
    payload = run_json(case, 'parse_logs.py', list(sources) + list(args), now=now)
    path = os.path.join(tmpdir, name)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return payload, path


# --------------------------------------------------------------------------
# Ожидаемый результат фикстуры
# --------------------------------------------------------------------------

# Ожидание — набор утверждений о значимых полях, а не снимок вывода: снимок
# ломается от любого нового поля, и его чинят перезаписью. Ключи перечислены
# явно, неизвестный ключ — ошибка: опечатка в утверждении не должна выглядеть
# как пройденная проверка.
ASSERTIONS = (
    'records', 'formats', 'levels', 'services', 'exceptions', 'statuses',
    'first_ts', 'last_ts', 'histogram', 'histogram_buckets',
    'signatures_contain', 'signatures_absent',
    'signature_kinds', 'signature_kinds_absent',
    'error_groups', 'top_group', 'groups_contain',
)


def load_expected(name):
    with open(os.path.join(EXPECTED, name), 'r', encoding='utf-8') as fh:
        return json.load(fh)


def _sig_values(parsed):
    return [str(s.get('value') or '') for s in parsed.get('signatures') or []]


def _error_groups(parsed):
    return [g for g in parsed.get('groups') or []
            if g.get('level') in ('ERROR', 'FATAL')]


def check_expected(case, parsed, expect, where):
    unknown = [k for k in expect if k not in ASSERTIONS]
    case.assertEqual(unknown, [], '%s: неизвестные утверждения %s' % (where, unknown))
    stats = parsed.get('stats') or {}

    for key in ('records', 'formats', 'levels', 'services', 'exceptions',
                'statuses', 'first_ts', 'last_ts', 'histogram'):
        if key in expect:
            case.assertEqual(stats.get(key, parsed.get(key)), expect[key],
                             '%s: %s разошёлся с ожидаемым' % (where, key))

    if 'histogram_buckets' in expect:
        case.assertEqual(len(parsed.get('histogram') or {}), expect['histogram_buckets'],
                         '%s: число интервалов гистограммы' % where)

    sigs = _sig_values(parsed)
    for needle in expect.get('signatures_contain', []):
        case.assertTrue(any(needle in s for s in sigs),
                        '%s: среди сигнатур нет %r\nесть: %s' % (where, needle, sigs))
    for needle in expect.get('signatures_absent', []):
        case.assertFalse(any(needle in s for s in sigs),
                         '%s: сигнатура %r не должна извлекаться' % (where, needle))

    kinds = {s.get('kind') for s in parsed.get('signatures') or []}
    for kind in expect.get('signature_kinds', []):
        case.assertIn(kind, kinds, '%s: нет сигнатур вида %s' % (where, kind))
    for kind in expect.get('signature_kinds_absent', []):
        case.assertNotIn(kind, kinds,
                         '%s: сигнатур вида %s быть не должно' % (where, kind))

    if 'error_groups' in expect:
        case.assertEqual(len(_error_groups(parsed)), expect['error_groups'],
                         '%s: число групп уровня ERROR/FATAL' % where)

    top = expect.get('top_group')
    if top:
        groups = parsed.get('groups') or []
        case.assertTrue(groups, '%s: групп нет вовсе' % where)
        first = groups[0]
        if 'level' in top:
            case.assertEqual(first.get('level'), top['level'],
                             '%s: уровень доминирующего шаблона' % where)
        if 'count' in top:
            case.assertEqual(first.get('count'), top['count'],
                             '%s: частота доминирующего шаблона' % where)
        if 'first' in top:
            case.assertEqual(first.get('first'), top['first'],
                             '%s: время первого появления шаблона' % where)
        if 'template_contains' in top:
            case.assertIn(top['template_contains'], first.get('template') or '',
                          '%s: шаблон доминирующей группы' % where)

    for want in expect.get('groups_contain', []):
        hits = [g for g in parsed.get('groups') or []
                if want['template'] in (g.get('template') or '')]
        case.assertTrue(hits, '%s: нет группы с шаблоном %r' % (where, want['template']))
        if 'count' in want:
            case.assertEqual(hits[0].get('count'), want['count'],
                             '%s: частота группы %r' % (where, want['template']))
        if 'level' in want:
            case.assertEqual(hits[0].get('level'), want['level'],
                             '%s: уровень группы %r' % (where, want['template']))


class ScriptCase(unittest.TestCase):
    """Общий предок: короткие имена для запуска скриптов."""

    maxDiff = 4000

    def run_script(self, name, args, **kw):
        return run(name, args, **kw)

    def json_of(self, name, args, **kw):
        return run_json(self, name, args, **kw)
