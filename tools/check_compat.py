#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка, что скрипты скилла разбираются заявленной минимальной версией Python.

Скилл обещает работать всюду, где есть python3 3.8+, и на этом обещании держится
guard в начале каждого скрипта. Обещание легко нарушить незаметно: достаточно
написать `match`, walrus или `list[str]` в аннотации — на машине разработчика с
новым интерпретатором всё пройдёт, а на стенде скрипт упадёт синтаксической
ошибкой ещё до guard'а, и пользователь не увидит объяснения.

Проверка ловит только синтаксис. Использование методов, появившихся в поздних
версиях, ast не заметит — за этим следят при правках.

  python3 tools/check_compat.py
  python3 tools/check_compat.py --min 3.9
"""

import argparse
import ast
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO, 'incident-triage', 'scripts')
DEFAULT_MIN = (3, 8)


def parse_version(text):
    parts = text.split('.')
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise argparse.ArgumentTypeError('версия задаётся как MAJOR.MINOR, например 3.8')
    return (int(parts[0]), int(parts[1]))


def check(directory, minimum):
    """Возвращает список (файл, сообщение) для файлов, не прошедших разбор."""
    failures = []
    names = sorted(n for n in os.listdir(directory) if n.endswith('.py'))
    if not names:
        failures.append((directory, 'в директории нет ни одного .py — проверять нечего'))
        return names, failures
    for name in names:
        path = os.path.join(directory, name)
        with open(path, 'r', encoding='utf-8') as fh:
            source = fh.read()
        try:
            ast.parse(source, filename=path, feature_version=minimum)
        except SyntaxError as exc:
            failures.append((name, '%s (строка %s)' % (exc.msg, exc.lineno)))
    return names, failures


def main():
    parser = argparse.ArgumentParser(
        description='Проверяет, что скрипты скилла разбираются минимальной версией Python.')
    parser.add_argument('--dir', default=SCRIPTS_DIR,
                        help='директория со скриптами (по умолчанию incident-triage/scripts)')
    parser.add_argument('--min', type=parse_version, default=DEFAULT_MIN,
                        help='минимальная версия, MAJOR.MINOR (по умолчанию 3.8)')
    args = parser.parse_args()

    # feature_version умеет проверять только на версию не выше текущей.
    if args.min > sys.version_info[:2]:
        sys.stderr.write('Проверка требует интерпретатор не старше %d.%d, запущен %s\n'
                         % (args.min[0], args.min[1], sys.version.split()[0]))
        return 2

    if not os.path.isdir(args.dir):
        sys.stderr.write('Директория не найдена: %s\n' % args.dir)
        return 2

    names, failures = check(args.dir, args.min)
    target = '%d.%d' % args.min
    if failures:
        sys.stderr.write('Синтаксис несовместим с Python %s:\n' % target)
        for name, message in failures:
            sys.stderr.write('  %s: %s\n' % (name, message))
        sys.stderr.write('\nЛибо перепишите совместимо, либо поднимите минимальную версию '
                         'в guard скриптов, README и openspec/config.yaml.\n')
        return 1

    print('Все %d скриптов разбираются Python %s: %s' % (len(names), target, args.dir))
    return 0


if __name__ == '__main__':
    sys.exit(main())
