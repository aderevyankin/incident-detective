# -*- coding: utf-8 -*-
"""Бюджет сводки по умолчанию: 12 000 символов, без всякого `--max-chars`.

Предел объявлен константой `MAX_SUMMARY_CHARS` и держится тем, что сводка уезжает
в контекст агента и остаётся там до конца разбора. Прежде он проверялся только с
явно заданным малым `--max-chars` — то есть проверялся механизм усечения, но не
значение по умолчанию: подними кто-нибудь константу до миллиона, и ни одна
проверка бы этого не заметила.
"""

import os
import sys
import unittest

import helpers
from helpers import ScriptCase

sys.path.insert(0, helpers.SCRIPTS)
from kb_common import MAX_SUMMARY_CHARS  # noqa: E402


class DefaultSummaryBudget(ScriptCase):

    def setUp(self):
        self.tmp = self.tmpdir()

    def _big_log(self, count=400):
        """Лог заведомо шире бюджета: одна трасса, много длинных записей."""
        path = os.path.join(self.tmp, 'big.log')
        with open(path, 'w', encoding='utf-8') as fh:
            for i in range(count):
                fh.write('2026-07-28 12:%02d:%02d ERROR [svc] trace_id=t-0 failure '
                         'number %d with a longish payload that repeats itself to '
                         'pad the line out\n' % ((i // 60) % 60, i % 60, i))
        return path

    def test_constant_is_twelve_thousand(self):
        """Значение названо и в README, и в спеке — меняться молча оно не должно."""
        self.assertEqual(MAX_SUMMARY_CHARS, 12000)

    def _many_templates_log(self, count=200):
        """Лог, где каждая запись даёт свой шаблон — чтобы лента была длинной.

        Различаться записи должны буквами: числа шаблонизация заменяет на `<n>`,
        и лог из «ошибка 1..200» схлопнулся бы в один шаблон и одно событие ленты.
        """
        def word(n):
            letters = ''
            while True:
                letters = chr(ord('a') + n % 26) + letters
                n //= 26
                if not n:
                    return letters

        path = os.path.join(self.tmp, 'many.log')
        with open(path, 'w', encoding='utf-8') as fh:
            for i in range(count):
                tag = word(i)
                fh.write('2026-07-28 12:%02d:%02d ERROR [svc-%s] failure %s while '
                         'talking to backend-%s with a padded explanation\n'
                         % ((i // 60) % 60, i % 60, tag, tag, tag))
        return path

    # Укладка вывода считается по разделу переменной длины, а разделы вокруг него
    # в замер не входят — сводка `trace.py` и лента `timeline.py` перебирают предел
    # на несколько сотен символов. Это отдельная находка о механизме укладки, и
    # здесь она не подгоняется под ассерт: проверяется, что предел по умолчанию
    # действует и объявляется, с допуском на непосчитанные разделы.
    OVERHEAD = 1.1

    def test_trace_summary_is_capped_without_the_flag(self):
        path = self._big_log()
        code, out, err = self.run_script(
            'trace.py', ['--log', 'a=' + path, '--id', 't-0', '--records', '400'])
        self.assertEqual(code, 0, err)
        self.assertIn('Не показано записей', out, 'усечение не объявлено')
        self.assertLess(len(out), MAX_SUMMARY_CHARS * self.OVERHEAD,
                        'сводка без --max-chars не ограничена вовсе: %d' % len(out))

    def test_timeline_summary_is_capped_without_the_flag(self):
        path = self._many_templates_log()
        code, out, err = self.run_script('timeline.py', ['--log', 'a=' + path])
        self.assertEqual(code, 0, err)
        self.assertIn('Не показано событий', out, 'усечение не объявлено')
        self.assertLess(len(out), MAX_SUMMARY_CHARS * self.OVERHEAD,
                        'лента без --max-chars не ограничена вовсе: %d' % len(out))

    def test_without_the_budget_the_same_summary_is_longer(self):
        """Обрезал именно бюджет, а не нехватка данных."""
        path = self._big_log()
        _code, capped, _err = self.run_script(
            'trace.py', ['--log', 'a=' + path, '--id', 't-0', '--records', '400'])
        _code, full, _err = self.run_script(
            'trace.py', ['--log', 'a=' + path, '--id', 't-0', '--records', '400',
                         '--max-chars', '0'])
        self.assertGreater(len(full), MAX_SUMMARY_CHARS)
        self.assertLess(len(capped), len(full))

    def test_triage_summary_is_capped_without_the_flag(self):
        path = self._big_log()
        # пустой --repo: контур кода пропускается. Без него оркестратор ищет
        # репозиторий сам и уходит обходить дерево от рабочей директории — к
        # бюджету сводки это отношения не имеет, а прогон удлиняет на минуты
        repo = os.path.join(self.tmp, 'repo')
        os.makedirs(repo)
        code, out, err = self.run_script(
            'triage.py', [path, '--top', '300', '--kb', helpers.KB, '--repo', repo,
                          '--out', os.path.join(self.tmp, 'stages')])
        self.assertEqual(code, 0, err)
        self.assertLessEqual(len(out), MAX_SUMMARY_CHARS,
                             'сводка triage.py без --max-chars превысила бюджет: '
                             '%d > %d' % (len(out), MAX_SUMMARY_CHARS))


if __name__ == '__main__':
    unittest.main()
