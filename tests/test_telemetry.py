# -*- coding: utf-8 -*-
"""Телеметрия вызовов: видно, какие контуры разбора реально прогнали.

Прогон фикстур проверяет, что скрипт на известном логе даёт известный ответ, и
ничего не говорит о том, был ли скрипт вызван вообще. Половина провалов разбора
именно такова: агент утонул в логах и не спросил базу знаний.
"""

import json
import os
import shutil
import tempfile
import unittest

import helpers
from helpers import ScriptCase

KNOWN = helpers.log('single_service.log')


class Telemetry(ScriptCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='triage-tests-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.file = os.path.join(self.tmp, 'telemetry.log')

    def _on(self, path=None):
        return {'INCIDENT_TRACE_FILE': path or self.file}

    def _lines(self):
        with open(self.file, 'r', encoding='utf-8') as fh:
            return [line for line in fh.read().split('\n') if line.strip()]

    def test_line_is_written_when_enabled(self):
        code, _out, err = self.run_script(
            'parse_logs.py', [KNOWN, '--format', 'json', '--level', 'ERROR'],
            env=self._on())
        self.assertEqual(code, 0, err)
        lines = self._lines()
        self.assertEqual(len(lines), 1)
        stamp, script, flags, rc = lines[0].split('\t')
        self.assertEqual(stamp, helpers.NOW)
        self.assertEqual(script, 'parse_logs.py')
        self.assertEqual(flags, '--format --level')
        self.assertEqual(rc, 'rc=0')

    def test_argument_values_do_not_leak(self):
        secret = 'фрагмент-боевого-лога-4711'
        self.run_script('parse_logs.py', [KNOWN, '--grep', secret], env=self._on())
        text = open(self.file, 'r', encoding='utf-8').read()
        self.assertIn('--grep', text)
        self.assertNotIn(secret, text, 'значение аргумента утекло в файл телеметрии')
        self.assertNotIn(KNOWN, text, 'путь к логу утёк в файл телеметрии')

    def test_file_is_not_created_when_disabled(self):
        code, _out, err = self.run_script('parse_logs.py', [KNOWN, '--format', 'json'])
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(self.file),
                         'без переменной файл телеметрии не должен появляться')

    def test_output_is_identical_with_and_without(self):
        _code, quiet, _err = self.run_script('parse_logs.py', [KNOWN, '--format', 'json'])
        _code, loud, _err = self.run_script('parse_logs.py', [KNOWN, '--format', 'json'],
                                            env=self._on())
        self.assertEqual(quiet, loud, 'телеметрия изменила стандартный вывод')

    def test_write_failure_does_not_break_the_run(self):
        broken = os.path.join(self.tmp, 'нет', 'такой', 'папки', 'telemetry.log')
        code, out, err = self.run_script('parse_logs.py', [KNOWN, '--format', 'json'],
                                         env=self._on(broken))
        self.assertEqual(code, 0, err)
        self.assertNotIn('Traceback', err)
        self.assertTrue(json.loads(out)['stats']['records'])

    def test_failed_run_is_recorded_with_its_code(self):
        code, _out, _err = self.run_script('parse_logs.py', ['/нет/такого.log'],
                                           env=self._on())
        self.assertNotEqual(code, 0)
        self.assertTrue(self._lines()[0].endswith('rc=%d' % code))

    def test_skipped_contour_is_visible(self):
        """По файлу видно порядок запусков и то, какой контур не прогнан."""
        parsed_path = os.path.join(self.tmp, 'parsed.json')
        code, out, err = self.run_script('parse_logs.py', [KNOWN, '--format', 'json'],
                                         env=self._on())
        self.assertEqual(code, 0, err)
        with open(parsed_path, 'w', encoding='utf-8') as fh:
            fh.write(out)
        self.run_script('kb_search.py', ['--from-parsed', parsed_path,
                                         '--kb', helpers.KB, '--format', 'json'],
                        env=self._on())
        self.run_script('confidence.py', ['--parsed', parsed_path, '--format', 'json'],
                        env=self._on())

        scripts = [line.split('\t')[1] for line in self._lines()]
        self.assertEqual(scripts, ['parse_logs.py', 'kb_search.py', 'confidence.py'])
        self.assertNotIn('code_hints.py', scripts,
                         'контур кода не запускался — это и должно быть видно')


if __name__ == '__main__':
    unittest.main()
