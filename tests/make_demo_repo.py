#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Собирает маленький git-репозиторий для контура кода в `docs/walkthrough.md`.

Фикстуры логов (`payment.log`, `gateway.log`) и записи базы знаний
(`tests/fixtures/kb/INC-2026-07-001-payment-pool-exhausted.md`) уже описывают
инцидент `ConnectionTimeoutError` в `src/payment/pool.py`. Настоящего репозитория
с таким файлом и историей коммитов среди фикстур не было — контуру кода
(`code_hints.py`) нечего было сопоставлять с логами. Этот скрипт строит такой
репозиторий по требованию, а не хранит его в git репозитория проекта: вложенный
`.git` в дереве основного репозитория ломает git-инструменты, которые ходят по
дереву рекурсивно.

    python3 tests/make_demo_repo.py /tmp/incident-demo-repo

Идемпотентен: если директория уже существует и это git-репозиторий с ожидаемым
файлом, ничего не делает.
"""

import os
import subprocess
import sys

FILE_REL = os.path.join('src', 'payment', 'pool.py')
COMMIT_DATE = '2026-07-27 20:00:00 +0000'

SOURCE = '''"""Пул соединений к платёжному провайдеру."""


class ConnectionTimeoutError(Exception):
    """Не дождались свободного соединения в пуле дольше отведённого таймаута."""


def acquire(pool, timeout_ms=5000):
    conn = pool.try_acquire(timeout_ms)
    if conn is None:
        raise ConnectionTimeoutError(
            'timed out waiting for connection from pool after %d ms' % timeout_ms)
    return conn
'''


def run_git(repo, args, when=None):
    env = os.environ.copy()
    env['GIT_AUTHOR_NAME'] = 'incident-detective-demo'
    env['GIT_AUTHOR_EMAIL'] = 'demo@example.invalid'
    env['GIT_COMMITTER_NAME'] = 'incident-detective-demo'
    env['GIT_COMMITTER_EMAIL'] = 'demo@example.invalid'
    if when:
        env['GIT_AUTHOR_DATE'] = when
        env['GIT_COMMITTER_DATE'] = when
    subprocess.run(['git', '-C', repo] + list(args), env=env, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def build(repo):
    file_path = os.path.join(repo, FILE_REL)
    if os.path.isdir(os.path.join(repo, '.git')) and os.path.isfile(file_path):
        return  # уже собран

    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, 'w', encoding='utf-8') as fh:
        fh.write(SOURCE)

    run_git(repo, ['init', '-q'])
    run_git(repo, ['add', '.'])
    run_git(repo, ['commit', '-q', '-m', 'add connection pool'], when=COMMIT_DATE)


def main(argv):
    if len(argv) != 1:
        sys.stderr.write('Использование: make_demo_repo.py ДИРЕКТОРИЯ\n')
        return 2
    repo = os.path.abspath(argv[0])
    os.makedirs(repo, exist_ok=True)
    build(repo)
    print('Демо-репозиторий готов: %s' % repo)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
