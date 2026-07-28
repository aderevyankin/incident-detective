#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Прогон проверок скилла — одной командой, на голом Python.

    python3 tests/run.py                 весь набор
    python3 tests/run.py test_parse      один файл проверок
    python3 tests/run.py -v              подробный вывод

Внешних зависимостей нет: unittest входит в стандартную библиотеку, а скилл не
тащит зависимостей по своей архитектуре — не тащит их и его проверка.

Проверка совместимости с минимальной версией Python (`tools/check_compat.py`)
входит сюда же: точка входа должна быть одна, иначе списки проверок у
разработчика и у CI расходятся.

Что прогоном НЕ проверяется: поведение модели — самозапуск скилла, порядок
шагов разбора, формулировки в ответе. Это инструкции в SKILL.md, и проверяются
они чтением, а не запуском.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# пути от расположения файла, а не от текущей директории: прогон запускается
# откуда угодно
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, 'tools'))


def check_compatibility():
    """Разбираются ли скрипты минимальной заявленной версией Python.

    Печать — целиком в check_compat.main(), здесь только код возврата: 0
    (совместимо), 1 (несовместимо) и 2 (интерпретатор старше заявленного
    минимума — проверка неполна, но это не провал прогона).
    """
    import check_compat

    code = check_compat.main([])
    if code == 2:
        sys.stderr.write('— совместимость: прогон неполный (см. сообщение выше)\n')
        return True
    return code == 0


def main(argv):
    verbosity = 2 if ('-v' in argv or '--verbose' in argv) else 1
    names = [a for a in argv if not a.startswith('-')]

    compatible = check_compatibility()

    loader = unittest.TestLoader()
    if names:
        suite = loader.loadTestsFromNames(names)
    else:
        suite = loader.discover(HERE, pattern='test_*.py', top_level_dir=HERE)

    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    if result.wasSuccessful() and compatible:
        return 0
    if not compatible:
        print('\nПрогон не пройден: проверка совместимости с минимальной версией Python.')
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
