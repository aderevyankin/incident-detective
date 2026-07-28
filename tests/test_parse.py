# -*- coding: utf-8 -*-
"""Разбор фикстур с заранее известным ответом."""

import json
import os
import unittest

import helpers
from helpers import ScriptCase


class ExpectedFixtures(ScriptCase):
    """Каждая фикстура сверяется с утверждениями из tests/expected/."""

    def test_fixtures_match_expectations(self):
        names = sorted(n for n in os.listdir(helpers.EXPECTED) if n.endswith('.json'))
        self.assertTrue(names, 'в tests/expected/ нет ни одного ожидаемого результата')
        for name in names:
            expected = helpers.load_expected(name)
            fixture = expected['fixture']
            with self.subTest(fixture=fixture):
                self.assertTrue(expected.get('why'),
                                '%s: не объяснено, что проверяет фикстура' % name)
                parsed = self.json_of(
                    'parse_logs.py',
                    [helpers.log(fixture)] + list(expected.get('args') or []),
                    now=expected.get('now', helpers.NOW))
                helpers.check_expected(self, parsed, expected['expect'], fixture)

    def test_every_fixture_has_expectations(self):
        """Фикстура без зафиксированного ответа ничего не проверяет."""
        declared = {helpers.load_expected(n)['fixture']
                    for n in os.listdir(helpers.EXPECTED) if n.endswith('.json')}
        on_disk = {n for n in os.listdir(helpers.LOGS) if n.endswith('.log')}
        # краевые случаи лежат в logs/edge и проверяются устойчивостью, а не
        # утверждениями о разборе
        forgotten = sorted(on_disk - declared - {'gateway.log', 'payment.log'})
        self.assertEqual(forgotten, [],
                         'нет ожидаемого результата для фикстур: %s' % forgotten)


class OutputContract(ScriptCase):

    def test_new_field_does_not_break_check(self):
        """Утверждения о значимых полях переживают появление нового поля."""
        expected = helpers.load_expected('single_service.json')
        parsed = self.json_of('parse_logs.py', [helpers.log('single_service.log')])
        parsed['новое_поле'] = {'что-то': 1}
        parsed['stats']['новое_поле'] = 42
        for group in parsed['groups']:
            group['новое_поле'] = None
        helpers.check_expected(self, parsed, expected['expect'],
                               'single_service.log + новое поле')

    def test_repeated_run_gives_same_result(self):
        """Два прогона на одних данных дают один и тот же ответ."""
        args = [helpers.log('single_service.log'), helpers.log('mixed_formats.log')]
        first = self.json_of('parse_logs.py', args)
        second = self.json_of('parse_logs.py', args)
        self.assertEqual(json.dumps(first, sort_keys=True, ensure_ascii=False),
                         json.dumps(second, sort_keys=True, ensure_ascii=False))

    def test_runs_from_any_directory(self):
        """Результат не зависит от того, откуда запущен скрипт."""
        args = [helpers.log('single_service.log')]
        from_root = self.json_of('parse_logs.py', args, cwd=os.path.abspath(os.sep))
        from_repo = self.json_of('parse_logs.py', args, cwd=helpers.REPO)
        self.assertEqual(from_root, from_repo)

    def test_markdown_output_is_readable(self):
        """Сводка для человека строится и содержит разделы, на которые ссылается SKILL.md."""
        code, out, err = self.run_script('parse_logs.py', [helpers.log('single_service.log')])
        self.assertEqual(code, 0, err)
        self.assertIn('# Сводка логов', out)
        self.assertIn('Сигнатуры для базы знаний', out)
        self.assertIn('ConnectionTimeoutError', out)


if __name__ == '__main__':
    unittest.main()
