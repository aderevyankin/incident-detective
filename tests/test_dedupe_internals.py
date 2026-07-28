# -*- coding: utf-8 -*-
"""Проверки для dedupe-script-internals: единый разбор времени, единый бюджет
объёма вывода, консервативный префильтр JSON-строк."""

import json
import os
import shutil
import tempfile
import unittest

import helpers
from helpers import ScriptCase

JSON_LEVEL_FIELD = helpers.log('json_level_field.log')
KNOWN = helpers.log('single_service.log')


class JsonPrefilter(ScriptCase):
    """Уровень JSON-строки берётся из поля, а не из первого слова сырого текста."""

    def test_debug_word_in_json_message_does_not_drop_the_record(self):
        parsed = self.json_of('parse_logs.py', [JSON_LEVEL_FIELD, '--level', 'ERROR'])
        # запись со словом «debug» в тексте, но полем "level":"error" должна
        # пройти фильтр целиком — раньше её отсеивал префильтр по сырому тексту
        self.assertEqual(parsed['stats']['filtered'], 1)
        templates = [g['template'] for g in parsed['groups']]
        self.assertTrue(any('retry failed' in t for t in templates),
                        'запись с полем level=error потерялась: %s' % templates)


class UnifiedTimeArg(ScriptCase):
    """--since/--until принимают один и тот же набор форматов во всех скриптах."""

    def test_relative_window_works_in_timeline(self):
        code, out, err = self.run_script(
            'timeline.py', ['--log', 'a=' + KNOWN, '--since', '8h'])
        self.assertEqual(code, 0, err)
        self.assertNotIn('Traceback', err)
        self.assertNotIn('Не разобрал время', out + err)

    def test_relative_window_works_in_trace(self):
        code, out, err = self.run_script(
            'trace.py', ['--log', 'a=' + KNOWN, '--since', '8h', '--top'])
        self.assertNotIn('Traceback', err)
        self.assertNotIn('Не разобрал время', out + err)

    def test_relative_window_works_in_code_hints(self):
        """Раньше второй `strptime` не был обёрнут в `try` — трейсбек на «1h»."""
        code, out, err = self.run_script(
            'code_hints.py', ['--text', 'boom', '--since', '1h', '--repo', helpers.REPO])
        self.assertNotIn('Traceback', err)

    def test_unparsed_value_is_a_clean_error_in_code_hints(self):
        code, out, err = self.run_script(
            'code_hints.py', ['--text', 'boom', '--since', 'вчера', '--repo', helpers.REPO])
        self.assertNotEqual(code, 0)
        self.assertNotIn('Traceback', err)


def _logfmt_line(minute, service, msg, trace_id='t-0'):
    return ('time=2026-07-28T12:%02d:00 level=error service=%s msg="%s" trace_id=%s\n'
           % (minute % 60, service, msg, trace_id))


class TraceBudget(ScriptCase):
    """`--records N` большой не должен уводить вывод за пределы бюджета."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='triage-tests-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _big_log(self, count=200):
        path = os.path.join(self.tmp, 'big.log')
        with open(path, 'w', encoding='utf-8') as fh:
            for i in range(count):
                fh.write(_logfmt_line(i, 'svc', 'failure number %d with a longish payload '
                                            'that repeats itself to pad the line out' % i))
        return path

    def test_output_stays_within_budget(self):
        path = self._big_log()
        code, out, err = self.run_script(
            'trace.py', ['--log', 'a=' + path, '--id', 't-0',
                        '--records', '200', '--max-chars', '800'])
        self.assertNotIn('Traceback', err)
        self.assertIn('Не показано записей', out)
        # раздел «Записи» показал заметно меньше, чем попросили --records: он и
        # был обрезан бюджетом, а не совпадением
        m = out.split('## Записи (', 1)[1].split(' из ', 1)[0]
        self.assertLess(int(m), 200)

    def test_no_budget_shows_everything(self):
        path = self._big_log(count=20)
        code, out, err = self.run_script(
            'trace.py', ['--log', 'a=' + path, '--id', 't-0',
                        '--records', '20', '--max-chars', '0'])
        self.assertNotIn('Не показано записей', out)
        self.assertIn('## Записи (20 из 20)', out)


class TimelineBudget(ScriptCase):
    """Лента `timeline.py` тоже укладывается в бюджет на большом входе."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='triage-tests-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # шаблонизация заменяет числа на `<n>`: слова должны различаться, иначе все
    # строки схлопнутся в один шаблон и «первое появление» будет только одно
    WORDS = ('timeout', 'refused', 'deadlock', 'overflow', 'corrupted', 'unreachable',
            'unauthorized', 'expired', 'exhausted', 'saturated', 'throttled', 'aborted')

    def _many_templates_log(self, count=60):
        path = os.path.join(self.tmp, 'many.log')
        with open(path, 'w', encoding='utf-8') as fh:
            for i in range(count):
                word = self.WORDS[i % len(self.WORDS)]
                # разные минуты и разный текст — чтобы шаблоны не схлопнулись
                # ни группировкой parse_logs, ни дедупликацией timeline.dedupe
                fh.write(_logfmt_line(i, 'svc-%d' % i,
                                      'kind-%d %s while talking to backend-%d' % (i, word, i),
                                      trace_id='t-%d' % i))
        return path

    def test_output_stays_within_budget(self):
        path = self._many_templates_log()
        code, out, err = self.run_script(
            'timeline.py', ['--log', 'a=' + path, '--max-chars', '900'])
        self.assertEqual(code, 0, err)
        self.assertNotIn('Traceback', err)
        self.assertIn('Не показано событий', out)
        self.assertLess(out.count('🆕'), 60,
                        'лента должна была обрезать хвост событий по бюджету')


class TriageBudget(ScriptCase):
    """`triage.py --max-chars` не превышается, даже если один раздел огромный."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='triage-tests-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_huge_logs_section_is_still_capped(self):
        path = os.path.join(self.tmp, 'huge.log')
        with open(path, 'w', encoding='utf-8') as fh:
            for i in range(300):
                fh.write('2026-07-28 12:%02d:%02d ERROR [svc-%d] distinct failure template '
                         'number %d with padding text to grow the line\n'
                         % ((i // 60) % 60, i % 60, i, i))
        budget = 1500
        code, out, err = self.run_script(
            'triage.py', [path, '--top', '300', '--max-chars', str(budget),
                         '--out', os.path.join(self.tmp, 'stages')],
            cwd=self.tmp)
        self.assertNotIn('Traceback', err)
        self.assertLessEqual(len(out), budget,
                             'triage.py --max-chars превышен: %d > %d' % (len(out), budget))


if __name__ == '__main__':
    unittest.main()
